"""Tests for the LIFETIME tag (radiod commit 0f8b622+).

Verifies that the encode side wires through `lifetime=` correctly:
  - `set_channel_lifetime()` builds a packet with LIFETIME present
  - `tune(lifetime=...)` includes LIFETIME on the command buffer
  - `create_channel(lifetime=...)` includes it on the creation buffer
  - omitting `lifetime` produces a packet WITHOUT a LIFETIME tag (so
    pre-0f8b622 radiod stays compatible)

We intercept `send_command` to capture the raw bytes and look for the
1-byte LIFETIME tag (= 117).  We don't try to decode the full TLV
stream — that's covered by the existing TLV encode/decode tests.
"""

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from ka9q.control import CMD, RadiodControl, StatusType
from ka9q.exceptions import ValidationError


def _bare_control() -> RadiodControl:
    """Construct a RadiodControl without touching the network."""
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
    return c


def _capture_send(control: RadiodControl) -> list[bytes]:
    """Replace `send_command` with a capture; return the buffer list."""
    sent: list[bytes] = []
    control.send_command = MagicMock(side_effect=lambda buf: sent.append(bytes(buf)))
    return sent


def _has_lifetime_tag(buf: bytes) -> bool:
    """Walk the TLV buffer (skipping the 1-byte CMD/STATUS prefix) and
    return True if any tag in the stream equals StatusType.LIFETIME (117).
    """
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
        # Extended-length: high bit set means follow-on length-of-length bytes
        if optlen & 0x80:
            n = optlen & 0x7F
            optlen = 0
            for _ in range(n):
                if cp >= len(buf):
                    return False
                optlen = (optlen << 8) | buf[cp]
                cp += 1
        if t == StatusType.LIFETIME:
            return True
        cp += optlen
    return False


class TestSetChannelLifetime:

    def test_sends_lifetime_tag(self):
        control = _bare_control()
        sent = _capture_send(control)
        control.set_channel_lifetime(ssrc=12345, lifetime=1000)
        assert len(sent) == 1
        assert sent[0][0] == CMD
        assert _has_lifetime_tag(sent[0])

    def test_zero_means_infinite(self):
        """lifetime=0 is the radiod sentinel for 'infinite'."""
        control = _bare_control()
        sent = _capture_send(control)
        control.set_channel_lifetime(ssrc=12345, lifetime=0)
        assert _has_lifetime_tag(sent[0])

    def test_rejects_negative_lifetime(self):
        control = _bare_control()
        with pytest.raises(ValidationError, match="lifetime"):
            control.set_channel_lifetime(ssrc=12345, lifetime=-1)

    def test_rejects_non_int_lifetime(self):
        control = _bare_control()
        with pytest.raises(ValidationError, match="lifetime"):
            control.set_channel_lifetime(ssrc=12345, lifetime=20.5)

    def test_validates_ssrc(self):
        control = _bare_control()
        with pytest.raises(ValidationError, match="Invalid SSRC"):
            control.set_channel_lifetime(ssrc=-1, lifetime=1000)


class TestTuneLifetime:

    def _capture_first_send_then_abort(self, control, sent_list):
        """Make send_command capture once, then raise to short-circuit tune()."""
        def side_effect(buf):
            sent_list.append(bytes(buf))
            # Aborting via TimeoutError is propagated out of tune() unchanged
            # (tune() only has try/finally around the listener, no except).
            raise TimeoutError("aborted after first send (unit test)")
        control.send_command = MagicMock(side_effect=side_effect)
        # Listener returns a MagicMock — tune() never gets to call select()
        # because send_command raises first.
        control._get_or_create_status_listener = MagicMock(return_value=MagicMock())

    def test_lifetime_appears_when_passed(self):
        """tune(lifetime=N) should include LIFETIME in the command buffer."""
        control = _bare_control()
        sent: list[bytes] = []
        self._capture_first_send_then_abort(control, sent)
        with pytest.raises(TimeoutError):
            control.tune(ssrc=12345, frequency_hz=14_074_000.0, lifetime=2000, timeout=0.05)
        assert sent, "tune() did not send any command"
        assert _has_lifetime_tag(sent[0])

    def test_lifetime_absent_when_omitted(self):
        """Omitting lifetime must produce a packet *without* the LIFETIME
        tag — preserves wire compatibility with pre-0f8b622 radiod.
        """
        control = _bare_control()
        sent: list[bytes] = []
        self._capture_first_send_then_abort(control, sent)
        with pytest.raises(TimeoutError):
            control.tune(ssrc=12345, frequency_hz=14_074_000.0, timeout=0.05)
        assert sent
        assert not _has_lifetime_tag(sent[0])

    def test_rejects_negative_lifetime(self):
        control = _bare_control()
        with pytest.raises(ValidationError, match="lifetime"):
            control.tune(ssrc=12345, lifetime=-5, timeout=0.05)


class TestCreateChannelLifetime:

    def test_lifetime_appears_when_passed(self):
        control = _bare_control()
        sent = _capture_send(control)
        control.create_channel(
            frequency_hz=14_074_000.0,
            preset="iq",
            ssrc=12345,
            lifetime=1500,
        )
        assert sent
        # First buffer is the main creation packet.
        assert _has_lifetime_tag(sent[0])

    def test_lifetime_absent_when_omitted(self):
        control = _bare_control()
        sent = _capture_send(control)
        control.create_channel(
            frequency_hz=14_074_000.0,
            preset="iq",
            ssrc=12345,
        )
        assert sent
        assert not _has_lifetime_tag(sent[0])


class TestMultiStreamLifetime:
    """MultiStream stores ``lifetime`` per-slot and forwards it to
    ensure_channel on both the initial add and the drop/restore path,
    plus exposes a set_channel_lifetime() keep-alive method.

    Asserted at the ``RadiodControl.ensure_channel`` boundary — these
    tests don't exercise the wire encoding (covered above), they verify
    that MultiStream wires the kwarg through correctly.
    """

    def _make_multi_with_mock_control(self, ssrc=12345):
        from ka9q.multi_stream import MultiStream
        from ka9q.discovery import ChannelInfo

        control = MagicMock()
        control.ensure_channel.return_value = ChannelInfo(
            ssrc=ssrc,
            preset="iq",
            sample_rate=12000,
            frequency=14_074_000.0,
            snr=0.0,
            multicast_address="239.1.2.3",
            port=5004,
        )
        multi = MultiStream(control=control)
        return multi, control

    def test_add_channel_forwards_lifetime(self):
        multi, control = self._make_multi_with_mock_control()
        multi.add_channel(
            frequency_hz=14_074_000.0,
            preset="iq",
            sample_rate=12000,
            lifetime=6000,
        )
        kwargs = control.ensure_channel.call_args.kwargs
        assert kwargs["lifetime"] == 6000

    def test_add_channel_lifetime_none_when_omitted(self):
        multi, control = self._make_multi_with_mock_control()
        multi.add_channel(
            frequency_hz=14_074_000.0,
            preset="iq",
            sample_rate=12000,
        )
        kwargs = control.ensure_channel.call_args.kwargs
        assert kwargs["lifetime"] is None

    def test_restore_reapplies_stored_lifetime(self):
        """A slot added with lifetime=N must re-pass N on _attempt_restore."""
        multi, control = self._make_multi_with_mock_control()
        multi.add_channel(
            frequency_hz=14_074_000.0,
            preset="iq",
            sample_rate=12000,
            lifetime=6000,
        )
        ssrc = next(iter(multi._slots))
        slot = multi._slots[ssrc]
        slot.dropped = True
        control.ensure_channel.reset_mock()

        multi._attempt_restore(ssrc, slot)

        kwargs = control.ensure_channel.call_args.kwargs
        assert kwargs["lifetime"] == 6000

    def test_set_channel_lifetime_updates_slot_and_wire(self):
        multi, control = self._make_multi_with_mock_control()
        multi.add_channel(
            frequency_hz=14_074_000.0,
            preset="iq",
            sample_rate=12000,
            lifetime=6000,
        )
        ssrc = next(iter(multi._slots))

        multi.set_channel_lifetime(ssrc, 9000)

        control.set_channel_lifetime.assert_called_once_with(ssrc, 9000)
        assert multi._slots[ssrc].lifetime == 9000

    def test_set_channel_lifetime_unknown_ssrc_is_noop(self):
        multi, control = self._make_multi_with_mock_control()
        multi.set_channel_lifetime(99999, 6000)
        control.set_channel_lifetime.assert_not_called()
