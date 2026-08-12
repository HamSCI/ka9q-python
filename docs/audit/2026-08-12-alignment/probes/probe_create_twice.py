"""Does create_channel converge when the channel already exists? (b1 only)

Task 6 verdict under test (idempotency.md Q1): DEFECT — an existing-SSRC
create is a radiod-side delta-update (only TLVs present in the *new*
packet are applied), not an atomic template reset. For two calls with
*identical* params (this probe), every relevant TLV is re-sent both
times, so the expected empirical outcome is CONVERGES — the delta-update
mechanism is invisible when nothing differs between calls. See
probe_create_twice_variant.py for the case that is expected to surface
the DEFECT (differing params on the second call).

Round 2 adaptations (see idempotency.md "Empirical results — Round 2"):
- `interface=LAN_IP` is required on this sandbox: the default route for
  239.0.0.0/8 is `dev lo`, which silently discards outbound control
  commands unless RadiodControl is told which NIC to bind
  IP_MULTICAST_IF to.
- `destination=` must be set explicitly to an address radiod already
  uses for RTP output (confirmed via a controlled A/B test — omitting
  destination, or using a not-already-in-use multicast address, causes
  radiod to silently accept the packet on the wire but never instantiate
  the channel; a destination matching an existing channel's group
  succeeds reproducibly). WORKING_DESTINATION below was read from a real
  channel's `multicast_address` via `discover_channels()` immediately
  before this session's probes ran.
"""
import sys
from ka9q import RadiodControl

HOST, SSRC, FREQ = "bee1-status.local", 3999900001, 7_040_000.0
LAN_IP = "192.168.1.176"  # this sandbox's ens18 IP; overrides the 239/8->lo route
WORKING_DESTINATION = "239.139.172.41"  # existing b1 RTP output group (see note above)
FIELDS = ("frequency", "sample_rate", "preset", "encoding")


def snap(info):
    return {f: getattr(info, f, None) for f in FIELDS}


c = RadiodControl(HOST, interface=LAN_IP)
try:
    c.create_channel(FREQ, preset="usb", sample_rate=12000, ssrc=SSRC,
                      destination=WORKING_DESTINATION)
    first = snap(c.poll_channel(SSRC, expected_freq=FREQ, timeout=5.0))
    c.create_channel(FREQ, preset="usb", sample_rate=12000, ssrc=SSRC,
                      destination=WORKING_DESTINATION)
    second = snap(c.poll_channel(SSRC, expected_freq=FREQ, timeout=5.0))
    print("first: ", first)
    print("second:", second)
    print("CONVERGES" if first == second else "DIVERGES")
    sys.exit(0 if first == second else 1)
finally:
    try:
        c.remove_channel(SSRC)
    finally:
        c.close()
