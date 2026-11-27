"""set_spectrum() must expose every spectrum-mode TLV radiod accepts, and
SpectrumStream must use it instead of a private hand-rolled buffer
(audit finding F11)."""

import threading
import time
from unittest.mock import MagicMock

import pytest

from ka9q.control import RadiodControl, StatusType
from ka9q.exceptions import ValidationError
from ka9q.types import DemodType, WindowType


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


NEW_TAGS = (
    StatusType.WINDOW_TYPE, StatusType.SPECTRUM_AVG,
    StatusType.SPECTRUM_OVERLAP, StatusType.SPECTRUM_BASE,
    StatusType.SPECTRUM_STEP,
)


class TestSetSpectrumNewParams:
    def test_all_new_tags_emitted(self):
        c = _bare_control()
        sent = _capture_send(c)
        c.set_spectrum(
            ssrc=12345, bin_bw_hz=100.0, bin_count=512,
            window_type=WindowType.HANN_WINDOW, avg=4, overlap=0.5,
            base=-120.0, step=0.5,
            demod_type=DemodType.SPECT2_DEMOD, frequency_hz=14.1e6,
        )
        assert len(sent) == 1
        for tag in NEW_TAGS + (StatusType.DEMOD_TYPE,
                               StatusType.RADIO_FREQUENCY):
            assert _has_tag(sent[0], tag), f"missing tag {tag}"

    def test_omitted_params_send_no_tags(self):
        c = _bare_control()
        sent = _capture_send(c)
        c.set_spectrum(ssrc=12345, bin_bw_hz=100.0, bin_count=512)
        for tag in NEW_TAGS + (StatusType.DEMOD_TYPE,
                               StatusType.RADIO_FREQUENCY):
            assert not _has_tag(sent[0], tag), f"unexpected tag {tag}"

    def test_avg_must_be_at_least_one(self):
        c = _bare_control()
        c.send_command = MagicMock()
        with pytest.raises(ValidationError, match="avg"):
            c.set_spectrum(ssrc=12345, avg=0)

    def test_overlap_must_be_below_one(self):
        c = _bare_control()
        c.send_command = MagicMock()
        with pytest.raises(ValidationError, match="overlap"):
            c.set_spectrum(ssrc=12345, overlap=1.0)


def test_spectrum_stream_delegates_to_set_spectrum():
    from ka9q.spectrum_stream import SpectrumStream
    control = MagicMock()
    s = SpectrumStream(
        control=control, frequency_hz=14.1e6, bin_count=512,
        resolution_bw=100.0, window_type=WindowType.HANN_WINDOW,
        kaiser_beta=11.0, averaging=4, overlap=0.5,
    )
    s._ssrc = 777
    s._send_spectrum_command()
    control.set_spectrum.assert_called_once_with(
        777,
        bin_bw_hz=100.0,
        bin_count=512,
        kaiser_beta=11.0,
        window_type=WindowType.HANN_WINDOW,
        avg=4,
        overlap=0.5,
        demod_type=DemodType.SPECT2_DEMOD,
        frequency_hz=14.1e6,
        lifetime=1000,
    )
    assert s._polls_sent == 1


# radiod never reaps a 0 Hz spectrum channel that carries no lifetime: a
# client that died before stop() leaked its channel for good (chirp scout
# step 1 on SIGMA, 2026-11-19, four leaked channels).

class TestSpectrumLifetime:
    def test_set_spectrum_sends_lifetime(self):
        c = _bare_control()
        sent = _capture_send(c)
        c.set_spectrum(ssrc=12345, bin_count=512, lifetime=1000)
        assert _has_tag(sent[0], StatusType.LIFETIME)

    def test_set_spectrum_omits_lifetime_by_default(self):
        c = _bare_control()
        sent = _capture_send(c)
        c.set_spectrum(ssrc=12345, bin_count=512)
        assert not _has_tag(sent[0], StatusType.LIFETIME)

    @pytest.mark.parametrize("bad", [-1, 2.5, "1000"])
    def test_set_spectrum_rejects_bad_lifetime(self, bad):
        c = _bare_control()
        c.send_command = MagicMock()
        with pytest.raises(ValidationError, match="lifetime"):
            c.set_spectrum(ssrc=12345, lifetime=bad)

    def test_every_poll_refreshes_the_lifetime(self):
        from ka9q.spectrum_stream import SpectrumStream
        control = MagicMock()
        s = SpectrumStream(control=control, frequency_hz=14.1e6,
                           lifetime_frames=500)
        s._ssrc = 777
        s._send_spectrum_command()
        s._send_spectrum_command()
        for call in control.set_spectrum.call_args_list:
            assert call.kwargs["lifetime"] == 500

    def test_zero_lifetime_keeps_the_old_behaviour(self):
        from ka9q.spectrum_stream import SpectrumStream
        control = MagicMock()
        s = SpectrumStream(control=control, frequency_hz=14.1e6,
                           lifetime_frames=0)
        s._ssrc = 777
        s._send_spectrum_command()
        assert control.set_spectrum.call_args.kwargs["lifetime"] == 0

    @pytest.mark.parametrize("bad", [-1, 2.5])
    def test_bad_lifetime_frames_refused(self, bad):
        from ka9q.spectrum_stream import SpectrumStream
        with pytest.raises(ValueError, match="lifetime_frames"):
            SpectrumStream(control=MagicMock(), frequency_hz=14.1e6,
                           lifetime_frames=bad)

    def test_lifetime_must_outlast_two_polls(self):
        # 100 frames ~ 2 s at 50 frames/s; a 1.5 s poll would let it lapse.
        from ka9q.spectrum_stream import SpectrumStream
        with pytest.raises(ValueError, match="poll_interval_sec"):
            SpectrumStream(control=MagicMock(), frequency_hz=14.1e6,
                           lifetime_frames=100, poll_interval_sec=1.5)

    def test_stop_removes_and_shortens_the_lifetime(self):
        from ka9q.spectrum_stream import SpectrumStream, STOP_LIFETIME_FRAMES
        control = MagicMock()
        s = SpectrumStream(control=control, frequency_hz=14.1e6)
        s._ssrc = 777
        s._running = True
        s.stop()
        control.remove_channel.assert_called_once_with(777)
        control.set_channel_lifetime.assert_called_once_with(
            777, STOP_LIFETIME_FRAMES, encoding=0)

    def test_stop_with_infinite_lifetime_sends_no_lifetime(self):
        from ka9q.spectrum_stream import SpectrumStream
        control = MagicMock()
        s = SpectrumStream(control=control, frequency_hz=14.1e6,
                           lifetime_frames=0)
        s._ssrc = 777
        s._running = True
        s.stop()
        control.remove_channel.assert_called_once_with(777)
        control.set_channel_lifetime.assert_not_called()
