"""Idempotency guardrails against a live radiod (b1/b2 only; never b3/b4).

Run:  uv run pytest tests/test_idempotency_integration.py --radiod-host=bee1-status.local
Skip: SKIP_INTEGRATION=1, or automatically when radiod is unreachable.
Test channels use SSRCs 3999900010-3999900019, destroyed unconditionally.

This file grew out of Task 13's brief, written before Task 7's live audit
(docs/audit/2026-08-12-alignment/idempotency.md, "Empirical results —
Round 2"; probes under docs/audit/2026-08-12-alignment/probes/). Several
things the brief assumed turned out not to hold against the real fleet
radiod, so the tests below are adapted from the brief with a comment at
each divergence point citing the empirical finding that forced it.
"""
import collections
import os
import socket
import time

import pytest

from ka9q import Encoding, RadiodControl, discover_channels

FREQ = 7_040_000.0
FIELDS = ("frequency", "sample_rate", "preset", "encoding")


def _snap(info):
    return {f: getattr(info, f, None) for f in FIELDS}


def _lan_ip():
    """Best-effort LAN IP for RadiodControl(interface=...).

    F8 (idempotency.md "Root cause" / "Empirical results — Round 2"): this
    class of sandbox routes 239.0.0.0/8 via `lo`, so an outbound control
    command sent without an explicit interface= silently never reaches
    radiod (no error -- it's just discarded by the kernel before it hits
    the wire). RadiodControl(interface=<NIC IP>) fixes this by forcing
    IP_MULTICAST_IF. Derived at runtime via the standard UDP-connect trick
    (a connected UDP socket sends no packet; connect() only makes the
    kernel pick a source route/address) instead of hardcoding the probes'
    literal 192.168.1.176, so this works on whatever host/NIC actually
    runs the test.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    finally:
        s.close()


@pytest.fixture(scope="session")
def lan_ip():
    if os.environ.get("SKIP_INTEGRATION"):
        pytest.skip("SKIP_INTEGRATION set")
    # A host with no default route (e.g. a sandboxed CI runner) raises
    # OSError out of connect() rather than yielding a source address; treat
    # that the same as "no usable network for this integration test" --
    # skip, don't error the fixture.
    try:
        return _lan_ip()
    except OSError as exc:
        pytest.skip(f"no LAN route available: {exc}")


@pytest.fixture(scope="session")
def reachable_destination(radiod_address, lan_ip):
    """Session-scoped reachability gate + working destination= group.

    Two adaptations bundled here:

    1. Skip-when-unreachable can't be a bare RadiodControl(...) try/except
       (as the brief's original `control` fixture did): construction
       succeeds even when the 239.0.0.0/8-via-lo problem (F8) is present,
       because setting IP_MULTICAST_IF doesn't itself contact the network.
       The failure only shows up as commands silently vanishing. A quick
       discover_channels() actually exercises the wire (read side) and
       gives a real reachability signal.

    2. create_channel's destination=None default ("uses radiod's
       config-file default" per its docstring) does not hold on
       bee1-status.local/bee2-status.local (idempotency.md Round 2,
       "Second requirement found"): omitting destination= silently never
       instantiates the channel, and only a multicast group radiod is
       already outputting on works. Discover the fleet's real channels
       once per session and reuse whichever destination group the most of
       them share (a stable, actively-used group), the same
       discover-then-reuse pattern the probes used.
    """
    if os.environ.get("SKIP_INTEGRATION"):
        pytest.skip("SKIP_INTEGRATION set")
    try:
        channels = discover_channels(radiod_address, listen_duration=5.0,
                                      interface=lan_ip)
    except Exception as exc:
        pytest.skip(f"radiod not reachable at {radiod_address}: {exc}")
    if not channels:
        pytest.skip(
            f"no channels discovered on {radiod_address}; radiod appears "
            "unreachable (or idle) from this host"
        )
    counts = collections.Counter(
        info.multicast_address for info in channels.values()
    )
    return counts.most_common(1)[0][0]


@pytest.fixture
def control(radiod_address, lan_ip, reachable_destination):
    if os.environ.get("SKIP_INTEGRATION"):
        pytest.skip("SKIP_INTEGRATION set")
    try:
        ctl = RadiodControl(radiod_address, interface=lan_ip)
    except Exception as exc:
        pytest.skip(f"radiod not reachable at {radiod_address}: {exc}")
    yield ctl
    ctl.close()


@pytest.fixture
def channel(control, reachable_destination):
    """Yields an SSRC allocator that guarantees teardown.

    destination= is defaulted to reachable_destination for every call --
    see reachable_destination's docstring (Round 2 finding: an omitted
    destination silently never creates the channel on this fleet).
    """
    created = []

    def make(ssrc, **kwargs):
        kwargs.setdefault("destination", reachable_destination)
        # Record the SSRC before calling create_channel: create_channel
        # issues two UDP sends, and an exception between them (or after
        # the first but before the second) must still be torn down below.
        # remove_channel on an SSRC that was never actually created is
        # already tolerated by the teardown loop's try/except.
        created.append(ssrc)
        control.create_channel(FREQ, ssrc=ssrc, **kwargs)
        return ssrc

    yield make
    for ssrc in created:
        try:
            control.remove_channel(ssrc)
        except Exception:
            pass


def test_create_twice_converges(control, channel):
    ssrc = channel(3999900010, preset="usb", sample_rate=12000)
    first = _snap(control.poll_channel(ssrc, expected_freq=FREQ, timeout=5.0))
    channel(3999900010, preset="usb", sample_rate=12000)
    second = _snap(control.poll_channel(ssrc, expected_freq=FREQ, timeout=5.0))
    assert first == second, "re-creating an existing channel must converge"


def test_keepalive_preserves_encoding(control, channel):
    # F7 (idempotency.md Round 2, Probe 2b Phase A): there is no automatic
    # client-side keepalive, and a bare poll_channel() does NOT extend a
    # channel's LIFETIME on this radiod build -- contradicting
    # set_channel_lifetime's own docstring ("...or any other poll..."). A
    # lifetime=1000-frame channel (~20s; LIFETIME is in radiod frames at
    # ~50/s, not seconds -- the brief's original lifetime=20 was ~0.4s)
    # left unattended for 30s would simply expire on schedule regardless
    # of any keepalive claim. So: explicitly call set_channel_lifetime()
    # every ~10s across ~30s, the code path 731ce5e actually guards (it
    # re-asserts OUTPUT_ENCODING alongside LIFETIME on every refresh), and
    # assert both survival and encoding preservation at each check -- this
    # is what the guardrail is actually meant to protect.
    ssrc = channel(3999900011, preset="usb", sample_rate=12000,
                   encoding=Encoding.F32LE, lifetime=1000)
    for _ in range(3):
        time.sleep(10)
        control.set_channel_lifetime(ssrc, 1000)
        info = control.poll_channel(ssrc, expected_freq=FREQ, timeout=5.0)
        assert info is not None, "channel vanished despite explicit keepalive"
        assert info.encoding == Encoding.F32LE, "keepalive dropped the encoding"


def test_remove_is_idempotent(control, channel):
    ssrc = channel(3999900012, preset="usb", sample_rate=12000)
    control.remove_channel(ssrc)
    control.remove_channel(ssrc)  # second remove must not raise
