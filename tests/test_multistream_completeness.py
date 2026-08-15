"""MultiStream's completeness must be able to report loss.

`completeness_pct` is the fleet's primary "are we losing data" metric.
Its formula is right --

    actual = total_samples_delivered - total_gaps_filled
    return min(100.0, actual / total_samples_expected * 100)

-- but MultiStream only ever set `total_samples_delivered`.  With
`total_samples_expected` left at 0 the property takes its early return
and yields a hardcoded **100.0**, whatever was lost.

MultiStream is the substrate for hf-timestd, wspr-recorder,
psk-recorder, meteor-scatter and hfdl-recorder (see CLAUDE.md), so every
multiplexing recorder in the fleet has been reporting perfect
completeness by construction.  Observed on AC0G-B4 2026-08-15: 42 gap
events totalling 93,600 samples logged in 25 minutes, with all 126
completeness lines reading `completeness=100.0%`.

Gap fills keep the sample count whole, so `expected` tracks `delivered`
and the loss is carried entirely by `total_gaps_filled`.
"""
import numpy as np

from ka9q.multi_stream import MultiStream, _ChannelSlot
from ka9q.resequencer import PacketResequencer, RTPPacket
from ka9q.stream_quality import StreamQuality

SR = 96_000
N = 180


def _slot():
    return _ChannelSlot(
        channel_info=None,
        resequencer=PacketResequencer(buffer_size=64, samples_per_packet=N,
                                      sample_rate=SR),
        quality=StreamQuality(), on_samples=lambda *a: None, sample_rate=SR,
        deliver_interval=1, frequency_hz=0.0, preset="iq", encoding=0,
        is_iq=True, on_stream_dropped=None, on_stream_restored=None,
    )


def _pkt(seq, ts, n=N):
    return RTPPacket(sequence=seq & 0xFFFF, timestamp=ts & 0xFFFFFFFF, ssrc=1,
                     samples=np.full(n, 0.5 + 0.5j, dtype=np.complex64),
                     wallclock=None)


def test_a_clean_stream_reports_full_completeness():
    m = MultiStream(control=None)
    slot = _slot()
    for k in range(4):
        m._account_delivery(slot, _pkt(k, 1000 + k * N).samples, [])

    assert slot.quality.completeness_pct == 100.0


def test_gap_filled_samples_are_subtracted_from_completeness():
    """The whole point: loss must be able to move this number."""
    from ka9q.stream_quality import GapEvent, GapSource
    m = MultiStream(control=None)
    slot = _slot()
    gap = GapEvent(source=GapSource.NETWORK_LOSS, position_samples=0,
                   duration_samples=N, timestamp_utc="", packets_affected=1)

    # 3 real packets delivered, plus one packet's worth of gap fill.
    for k in range(3):
        m._account_delivery(slot, _pkt(k, 1000 + k * N).samples, [])
    m._account_delivery(slot, _pkt(3, 1000 + 3 * N).samples, [gap])

    # 4 packets' worth delivered, one of them synthetic => 75%.
    assert slot.quality.completeness_pct == 75.0


def test_completeness_cannot_silently_stay_at_100_when_everything_is_filled():
    from ka9q.stream_quality import GapEvent, GapSource
    m = MultiStream(control=None)
    slot = _slot()
    gap = GapEvent(source=GapSource.RADIOD_BLOCK_DROP, position_samples=0,
                   duration_samples=N, timestamp_utc="", packets_affected=1)

    m._account_delivery(slot, _pkt(0, 1000).samples, [gap])

    assert slot.quality.completeness_pct == 0.0
