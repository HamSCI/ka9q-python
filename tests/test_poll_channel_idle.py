"""poll_channel() and radiod's 0 Hz ("idle") replies -- ka9q-python#6.

radiod answers a poll for an SSRC it holds at 0 Hz -- a channel being reaped
after ``remove_channel()``, one parked at 0 Hz, or (on builds that create
channels dynamically) one freshly minted by the poll itself -- with a STATUS
reply naming that SSRC and ``frequency = 0``.  By default ``poll_channel()``
discards such replies as "no such channel", which makes a purging SSRC look
free.  ``allow_idle=True`` returns the reply so the caller can see that radiod
currently holds the SSRC.
"""

import time
from types import SimpleNamespace
from unittest import mock

import pytest

from ka9q.control import (RadiodControl, encode_double, encode_eol, encode_int,
                          encode_string)
from ka9q.types import StatusType

SSRC = 0x12345678
MCAST = "239.1.2.3"


def status_packet(ssrc: int, frequency: float, preset: str = "iq",
                  sample_rate: int = 16000) -> bytes:
    pkt = bytearray([0])                       # STATUS type byte
    encode_int(pkt, StatusType.OUTPUT_SSRC, ssrc)
    encode_double(pkt, StatusType.RADIO_FREQUENCY, frequency)
    encode_string(pkt, StatusType.PRESET, preset)
    encode_int(pkt, StatusType.OUTPUT_SAMPRATE, sample_rate)
    encode_eol(pkt)
    return bytes(pkt)


class FakeSocket:
    """Hands out the scripted replies, then reports nothing more."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.sent = []

    def setsockopt(self, *args):
        pass

    def sendto(self, data, addr):
        self.sent.append(bytes(data))

    def recvfrom(self, bufsize):
        if self.replies:
            return self.replies.pop(0), (MCAST, 5006)
        time.sleep(0.01)
        raise OSError("nothing more")

    def close(self):
        pass


@pytest.fixture
def control():
    c = RadiodControl.__new__(RadiodControl)
    c.status_address = "radio.local"
    c.metrics = SimpleNamespace(status_received=0)
    return c


def poll(control, replies, **kwargs):
    sock = FakeSocket(replies)
    with mock.patch("ka9q.control.resolve_multicast_address", return_value=MCAST), \
         mock.patch("ka9q.discovery._create_status_listener_socket", return_value=sock), \
         mock.patch("ka9q.control.select.select",
                    side_effect=lambda r, w, x, t: (list(r), [], [])):
        return control.poll_channel(SSRC, timeout=0.3, **kwargs)


class TestIdleReplies:

    def test_default_drops_idle_reply(self, control):
        """Unchanged contract: an idle (0 Hz) reply is 'no such channel'."""
        assert poll(control, [status_packet(SSRC, 0.0)]) is None

    def test_allow_idle_returns_idle_reply(self, control):
        info = poll(control, [status_packet(SSRC, 0.0, preset="wfm")],
                    allow_idle=True)
        assert info is not None
        assert info.ssrc == SSRC
        assert info.frequency == 0.0
        assert info.preset == "wfm"

    def test_allow_idle_returns_live_reply_unchanged(self, control):
        info = poll(control, [status_packet(SSRC, 14.074e6)], allow_idle=True)
        assert info is not None
        assert info.frequency == pytest.approx(14.074e6)

    def test_expected_freq_still_rejects_idle_reply(self, control):
        """expected_freq is the establishment probe; 0 Hz never matches it."""
        assert poll(control, [status_packet(SSRC, 0.0)],
                    expected_freq=14.074e6, allow_idle=True) is None

    def test_idle_reply_for_another_ssrc_is_ignored(self, control):
        assert poll(control, [status_packet(SSRC + 1, 0.0)],
                    allow_idle=True) is None
