"""Destination resolution for channel creation (audit F5, owner-ruled model).

Empirically (idempotency.md Round 2, "Second requirement found") an omitted
OUTPUT_DATA_DEST_SOCKET TLV silently creates nothing.  The supported model
is a per-client destination derived from client_id (same derivation in
create_channel and ensure_channel via _derive_client_destination());
explicit destination= is the legacy path and keeps working.  Creation
fails loudly only when NEITHER is available."""

import threading
import time
from unittest.mock import MagicMock

import pytest

from ka9q.addressing import generate_multicast_ip
from ka9q.control import RadiodControl, StatusType
from ka9q.exceptions import ValidationError


def _bare_control(client_id=None) -> RadiodControl:
    c = RadiodControl.__new__(RadiodControl)
    c.status_address = "test.local"
    c.socket = MagicMock()
    c.dest_addr = ("239.1.2.3", 5006)
    c._socket_lock = threading.RLock()
    c.max_commands_per_sec = 100
    c._command_count = 0
    c._command_window_start = time.time()
    c._rate_limit_lock = threading.Lock()
    c.metrics = MagicMock()
    c._requested_encoding = {}
    c.client_id = client_id
    return c


def _capture_send(control):
    sent = []
    control.send_command = MagicMock(
        side_effect=lambda buf: sent.append(bytes(buf)))
    return sent


def _has_tag(buf: bytes, tag: int) -> bool:
    """TLV walker (same as tests/test_filter_edges.py)."""
    cp = 1
    while cp < len(buf):
        t = buf[cp]
        cp += 1
        if t == StatusType.EOL:
            break
        if cp >= len(buf):
            break
        optlen = buf[cp]
        cp += 1
        if optlen & 0x80:
            n = optlen & 0x7F
            optlen = 0
            for _ in range(n):
                if cp >= len(buf):
                    return False
                optlen = (optlen << 8) | buf[cp]
                cp += 1
        if t == tag:
            return True
        cp += optlen
    return False


def test_create_channel_underivable_raises():
    c = _bare_control(client_id=None)
    c.send_command = MagicMock()
    with pytest.raises(ValidationError, match="destination"):
        c.create_channel(frequency_hz=14_074_000.0, ssrc=12345)
    c.send_command.assert_not_called()   # fail BEFORE any bytes hit the wire


def test_create_channel_derives_destination_from_client_id():
    c = _bare_control(client_id="unit-test")
    sent = _capture_send(c)
    c.create_channel(frequency_hz=14_074_000.0, ssrc=12345)
    assert sent, "create_channel sent nothing"
    assert _has_tag(sent[0], StatusType.OUTPUT_DATA_DEST_SOCKET), (
        "derived destination must be encoded — radiod silently creates "
        "nothing without the destination TLV")


def test_explicit_destination_still_works_legacy():
    c = _bare_control(client_id=None)
    sent = _capture_send(c)
    c.create_channel(frequency_hz=14_074_000.0, ssrc=12345,
                     destination="239.9.8.7")
    assert _has_tag(sent[0], StatusType.OUTPUT_DATA_DEST_SOCKET)


def test_create_and_ensure_derive_identical_destination():
    """Locks the shared-helper invariant: create_channel and ensure_channel
    must derive the SAME per-(client, radiod) group, so the two paths can
    never drift apart again."""
    expected = generate_multicast_ip(unique_id="unit-test",
                                     radiod_host="test.local")
    c = _bare_control(client_id="unit-test")
    assert c._derive_client_destination() == expected


def test_ensure_channel_underivable_raises():
    c = _bare_control(client_id=None)
    with pytest.raises(ValidationError, match="destination"):
        c.ensure_channel(frequency_hz=14_074_000.0, preset="iq",
                         sample_rate=16000)


def test_ensure_channel_derives_and_reuses_matching_channel():
    # The derived destination must reach the reuse comparison: an existing
    # channel on exactly the derived group is reused, create_channel is
    # never called, and the guard does not fire.
    from ka9q.discovery import ChannelInfo
    derived = generate_multicast_ip(unique_id="unit-test",
                                    radiod_host="test.local")
    c = _bare_control(client_id="unit-test")
    c.create_channel = MagicMock()
    ch = ChannelInfo(ssrc=1, preset="iq", sample_rate=16000,
                     frequency=14_074_000.0, snr=0.0,
                     multicast_address=derived, port=5004)

    def poll(ssrc, *a, **k):
        ch.ssrc = ssrc
        return ch

    c.poll_channel = MagicMock(side_effect=poll)
    c.ensure_channel(frequency_hz=14_074_000.0, preset="iq",
                     sample_rate=16000)  # must not raise
    c.create_channel.assert_not_called()
