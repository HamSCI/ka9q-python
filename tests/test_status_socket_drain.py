"""The cached poll socket must not accumulate radiod's status broadcast.

`RadiodControl` caches one socket joined to the status group but reads it
only during a poll. Between polls the kernel queues the entire continuous
status stream into it. On B4 that socket sat permanently full and had
discarded 1,254,234 packets at ~24/s, while the same process's
continuously-drained StatusListener socket showed zero.

Correctness never depended on draining -- poll_status matches on
command_tag -- but a poll had to recvfrom() and decode its way through
thousands of stale packets inside a 2 s timeout to reach its reply.
"""
import socket

import pytest

from ka9q.control import RadiodControl


@pytest.fixture
def pair():
    """A real UDP socket with real queued datagrams. No mock can show the
    behaviour under test, which is entirely about kernel queue state."""
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.bind(("127.0.0.1", 0))
    rx.settimeout(0.1)
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    yield rx, tx, rx.getsockname()
    rx.close()
    tx.close()


def test_drain_discards_everything_queued(pair):
    rx, tx, addr = pair
    for i in range(50):
        tx.sendto(b"stale-%d" % i, addr)
    assert RadiodControl._drain_status_socket(rx) == 50
    with pytest.raises((socket.timeout, BlockingIOError, OSError)):
        rx.recv(4096)


def test_drain_on_empty_socket_is_a_no_op(pair):
    rx, _, _ = pair
    assert RadiodControl._drain_status_socket(rx) == 0


def test_socket_is_usable_after_draining(pair):
    """The drain flips the socket non-blocking; if it fails to restore the
    timeout, every subsequent poll turns into a busy-spin that never blocks."""
    rx, tx, addr = pair
    tx.sendto(b"stale", addr)
    RadiodControl._drain_status_socket(rx)
    tx.sendto(b"fresh", addr)
    assert rx.recv(4096) == b"fresh"
    assert rx.gettimeout() == pytest.approx(0.1)
    assert not rx.getblocking() is False or rx.gettimeout() is not None


def test_only_post_drain_traffic_survives(pair):
    """The property poll_status relies on: after draining, the next read is
    guaranteed to be traffic that arrived after the command was sent."""
    rx, tx, addr = pair
    for i in range(20):
        tx.sendto(b"before-%d" % i, addr)
    RadiodControl._drain_status_socket(rx)
    tx.sendto(b"reply", addr)
    assert rx.recv(4096) == b"reply"


def test_drain_terminates_on_a_socket_that_never_raises():
    """The bound, tested directly.

    The first version of this drain was `while True: recv()` exiting only on
    an exception. That is not a drain, it is a hostage to the sender: on a
    group refilled as fast as it is read it never returns. It hung the
    tune() suite immediately, because a mocked socket returns a value
    forever, and the same shape would stall a real poll behind a busy
    group. Termination must not depend on the peer.
    """
    from unittest.mock import MagicMock
    from ka9q.control import _DRAIN_MAX_PACKETS

    import time
    from ka9q.control import _DRAIN_MAX_SEC

    sock = MagicMock()
    sock.recv.return_value = b"x" * 200          # never raises, never ends
    t0 = time.monotonic()
    n = RadiodControl._drain_status_socket(sock)
    elapsed = time.monotonic() - t0

    # Either bound may trip first depending on machine speed -- the contract
    # is that it stops, promptly, and leaves the socket usable. Asserting a
    # specific bound would just re-encode today's hardware.
    assert 0 < n <= _DRAIN_MAX_PACKETS
    assert elapsed < _DRAIN_MAX_SEC * 4
    sock.setblocking.assert_any_call(True)


def test_drain_restores_blocking_state_even_when_recv_raises():
    """`finally` must run: a socket left non-blocking turns every later
    poll into a busy-spin that never waits."""
    from unittest.mock import MagicMock

    sock = MagicMock()
    sock.recv.side_effect = OSError("boom")
    RadiodControl._drain_status_socket(sock)
    sock.setblocking.assert_any_call(True)
    sock.settimeout.assert_any_call(0.1)
