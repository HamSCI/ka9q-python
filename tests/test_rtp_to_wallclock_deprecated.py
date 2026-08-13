"""audit F16 (ka9q side): rtp_to_wallclock must emit DeprecationWarning
while staying signature- and behavior-identical to rtp_to_utc.  The two
lagging clients (hf-timestd, wspr-recorder) migrated first — this warning
is the migration signal for any remaining out-of-tree callers."""

import inspect
import warnings

import pytest

from ka9q.discovery import ChannelInfo
from ka9q.rtp_recorder import rtp_to_utc, rtp_to_wallclock

GPS_UTC_OFFSET = 315964800
BILLION = 1_000_000_000
GPS_TIME_NS = 1234567890000000000


def _channel(sample_rate=48000, gps_time_ns=GPS_TIME_NS, rtp_timesnap=1000):
    return ChannelInfo(
        ssrc=1234, preset="test", sample_rate=sample_rate,
        frequency=100.0, snr=0.0, multicast_address="239.1.2.3",
        port=5004, gps_time=gps_time_ns, rtp_timesnap=rtp_timesnap,
    )


def _hint():
    # The channel's own wall-clock instant — pins wrap epoch k=0.
    return (GPS_TIME_NS + BILLION * (GPS_UTC_OFFSET - 18)) / BILLION


def test_emits_deprecation_warning():
    with pytest.warns(DeprecationWarning, match="rtp_to_utc"):
        rtp_to_wallclock(2000, _channel(), wallclock_hint_sec=_hint())


def test_result_identical_to_rtp_to_utc():
    ch = _channel()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        old = rtp_to_wallclock(2000, ch, wallclock_hint_sec=_hint())
    new = rtp_to_utc(2000, ch, wallclock_hint_sec=_hint())
    assert old is not None
    assert old == new


def test_none_path_preserved():
    # Same construction as tests/test_rtp_recorder.py::
    # test_rtp_to_wallclock_returns_none_when_timing_missing.
    ch = _channel(gps_time_ns=None, rtp_timesnap=None)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        assert rtp_to_wallclock(2000, ch) is None


def test_signature_renders_identically():
    # tests/client_usage_manifest.json snapshots str(inspect.signature) for
    # ka9q:rtp_to_wallclock and ka9q.rtp_recorder:rtp_to_wallclock — the
    # wrapper must render exactly like the old plain alias did.
    assert str(inspect.signature(rtp_to_wallclock)) == str(
        inspect.signature(rtp_to_utc))


def test_rtp_to_utc_does_not_warn():
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        rtp_to_utc(2000, _channel(), wallclock_hint_sec=_hint())


def test_internal_hot_paths_do_not_use_the_alias():
    """The library must not deprecation-warn about itself: the per-packet
    receive paths in stream.py / multi_stream.py / rtp_recorder.py call
    rtp_to_utc directly.  (hasattr checks: those two modules used to
    *import* the alias, so the module attribute existing at all proves
    the old wiring.  ka9q/__init__.py's public re-export is exempt and
    untouched.)"""
    import ka9q.multi_stream
    import ka9q.rtp_recorder
    import ka9q.stream
    assert not hasattr(ka9q.stream, "rtp_to_wallclock")
    assert not hasattr(ka9q.multi_stream, "rtp_to_wallclock")
    src = inspect.getsource(ka9q.rtp_recorder.RTPRecorder)
    assert "rtp_to_wallclock(" not in src
