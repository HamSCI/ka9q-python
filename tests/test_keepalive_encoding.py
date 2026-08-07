"""The keepalive must not cost a channel its output encoding.

`set_channel_lifetime()` is the keepalive every long-lived client runs.  Some
radiod builds reset a channel's output encoding to the preset default when they
process a LIFETIME-only command, so the call that keeps the channel alive is
also what silently downgrades it -- F32 for one interval, S16 from then on, and
`verify_channel()` never noticed because it only checked frequency.
See HamSCI/ka9q-python#3.
"""

import threading
import time
from unittest.mock import MagicMock

import pytest

from ka9q.control import RadiodControl, StatusType, _encoding_name
from ka9q.types import Encoding


def _bare_control() -> RadiodControl:
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


def _capture(control):
    sent = []
    control.send_command = MagicMock(side_effect=lambda buf: sent.append(bytes(buf)))
    return sent


class TestKeepaliveCarriesEncoding:
    def test_reasserts_remembered_encoding(self):
        c = _bare_control()
        c._requested_encoding[1234] = Encoding.F32LE
        sent = _capture(c)
        c.set_channel_lifetime(1234, 3000)
        assert bytes([StatusType.OUTPUT_ENCODING, 1, Encoding.F32LE]) in sent[0]

    def test_explicit_encoding_wins_over_remembered(self):
        c = _bare_control()
        c._requested_encoding[1234] = Encoding.F32LE
        sent = _capture(c)
        c.set_channel_lifetime(1234, 3000, encoding=Encoding.S16LE)
        assert bytes([StatusType.OUTPUT_ENCODING, 1, Encoding.S16LE]) in sent[0]

    def test_no_encoding_tag_when_none_requested(self):
        """Back-compat: a client that never asked for an encoding sends none."""
        c = _bare_control()
        sent = _capture(c)
        c.set_channel_lifetime(1234, 3000)
        assert bytes([StatusType.OUTPUT_ENCODING]) not in sent[0]

    def test_explicit_zero_suppresses_the_tag(self):
        c = _bare_control()
        c._requested_encoding[1234] = Encoding.F32LE
        sent = _capture(c)
        c.set_channel_lifetime(1234, 3000, encoding=0)
        assert bytes([StatusType.OUTPUT_ENCODING]) not in sent[0]

    def test_lifetime_tag_still_present(self):
        """Re-asserting encoding must not disturb what this call is for."""
        c = _bare_control()
        c._requested_encoding[1234] = Encoding.F32LE
        sent = _capture(c)
        c.set_channel_lifetime(1234, 3000)
        assert bytes([StatusType.LIFETIME]) in sent[0]

    def test_set_output_encoding_is_remembered(self):
        c = _bare_control()
        _capture(c)
        c.set_output_encoding(4321, Encoding.F32LE)
        assert c._requested_encoding[4321] == Encoding.F32LE


class TestVerifyChannelChecksEncoding:
    @pytest.fixture(autouse=True)
    def _patch_discovery(self, monkeypatch):
        """Patch via monkeypatch so the module global is restored per test."""
        self._monkeypatch = monkeypatch

    def _control_with_channel(self, granted):
        c = _bare_control()
        ch = MagicMock()
        ch.frequency = 10_000_000.0
        ch.preset = "iq"
        ch.encoding = granted
        import ka9q.control as ctl
        self._monkeypatch.setattr(ctl, "discover_channels", lambda addr: {77: ch})
        return c

    def test_mismatch_is_reported(self):
        c = self._control_with_channel(granted=Encoding.S16BE)
        c._requested_encoding[77] = Encoding.F32LE
        assert c.verify_channel(77) is False

    def test_match_passes(self):
        c = self._control_with_channel(granted=Encoding.F32LE)
        c._requested_encoding[77] = Encoding.F32LE
        assert c.verify_channel(77) is True

    def test_explicit_expectation_overrides(self):
        c = self._control_with_channel(granted=Encoding.S16BE)
        c._requested_encoding[77] = Encoding.F32LE
        assert c.verify_channel(77, expected_encoding=Encoding.S16BE) is True

    def test_zero_skips_the_check(self):
        c = self._control_with_channel(granted=Encoding.S16BE)
        c._requested_encoding[77] = Encoding.F32LE
        assert c.verify_channel(77, expected_encoding=0) is True

    def test_no_expectation_means_no_check(self):
        """Callers that never requested an encoding keep the old behaviour."""
        c = self._control_with_channel(granted=Encoding.S16BE)
        assert c.verify_channel(77) is True


class TestEncodingName:
    def test_known(self):
        assert _encoding_name(Encoding.F32LE) == "F32LE(4)"

    def test_unknown(self):
        assert _encoding_name(99) == "UNKNOWN(99)"

    def test_none(self):
        assert _encoding_name(None) == "unknown"
