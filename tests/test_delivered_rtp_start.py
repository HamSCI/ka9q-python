"""delivered_rtp_start: the truthful first-sample RTP label for
resequenced/zero-filled delivery.

Motivation (hf-timestd T6 origin slips, 2026-08-11): consumers labelled
delivered batches with quality.last_rtp_timestamp — the last RECEIVED
packet's header, stamped pre-resequencer — which desynchronizes from the
delivered samples under loss.  These tests pin the contract: the
published label equals the true RTP of the first delivered sample,
through contiguity, gaps (fills included), and timestamp wrap.
"""
import numpy as np
import unittest

from ka9q.resequencer import PacketResequencer, RTPPacket
from ka9q.multi_stream import MultiStream, _ChannelSlot


def _pkt(seq, ts, n=60, ssrc=1):
    return RTPPacket(sequence=seq & 0xFFFF, timestamp=ts & 0xFFFFFFFF,
                     ssrc=ssrc, samples=np.ones(n, dtype=np.complex64),
                     wallclock=None)


class TestResequencerChunkStart(unittest.TestCase):
    def setUp(self):
        self.r = PacketResequencer(buffer_size=16, samples_per_packet=60,
                                   sample_rate=96000)

    def test_contiguous_chunk_starts_track_stream(self):
        # First emission includes the initializing packet, so the first
        # chunk's truthful label is pkt0's timestamp.
        self.r.process_packet(_pkt(0, 1000))
        out, _ = self.r.process_packet(_pkt(1, 1060))
        self.assertIsNotNone(out)
        self.assertEqual(self.r.last_chunk_rtp_start, 1000)
        out, _ = self.r.process_packet(_pkt(2, 1120))
        self.assertIsNotNone(out)
        self.assertEqual(self.r.last_chunk_rtp_start, 1120)

    def test_gap_fill_included_in_chunk_start(self):
        self.r.process_packet(_pkt(0, 1000))
        self.r.process_packet(_pkt(1, 1060))
        # Lose seq 2 and 3 (120 samples); seq 4 arrives at ts 1300.
        out, gaps = self.r.process_packet(_pkt(4, 1300))
        # Resequencer waits for the missing seq until buffer pressure or
        # skip logic; feed more so it flushes.
        seq, ts = 5, 1360
        while out is None:
            out, gaps = self.r.process_packet(_pkt(seq, ts))
            seq += 1
            ts += 60
        # The emitted chunk begins with the ZERO FILL at the first lost
        # sample's true RTP: 1120.
        self.assertEqual(self.r.last_chunk_rtp_start, 1120)
        # Skip-ahead may coalesce the loss differently; the contract here
        # is the label, and that the loss was accounted as gap fill.
        self.assertGreaterEqual(sum(g.duration_samples for g in gaps), 120)

    def test_wrap_around(self):
        base = 0xFFFFFFC4          # 60 before wrap
        self.r.process_packet(_pkt(0, base))
        out, _ = self.r.process_packet(_pkt(1, (base + 60) & 0xFFFFFFFF))
        self.assertIsNotNone(out)
        # Chunk includes the initializing pre-wrap packet: label = base.
        self.assertEqual(self.r.last_chunk_rtp_start, base)
        # Next chunk starts exactly at the wrap.
        out, _ = self.r.process_packet(_pkt(2, (base + 120) & 0xFFFFFFFF))
        self.assertIsNotNone(out)
        # pkt1 (ts wraps to 0) went out in the first chunk; this chunk is
        # pkt2 alone, post-wrap: label 60.
        self.assertEqual(self.r.last_chunk_rtp_start, (base + 120) & 0xFFFFFFFF)


class TestMultiStreamDeliveredLabel(unittest.TestCase):
    def test_slot_pending_flows_to_quality(self):
        got = {}
        def cb(samples, quality):
            got['label'] = quality.delivered_rtp_start
            got['n'] = len(samples)
        from ka9q.stream_quality import StreamQuality
        slot = _ChannelSlot(
            channel_info=None, resequencer=PacketResequencer(
                buffer_size=16, samples_per_packet=60, sample_rate=96000),
            quality=StreamQuality(), on_samples=cb, sample_rate=96000,
            deliver_interval=1, frequency_hz=0.0, preset="iq", encoding=0,
            is_iq=True, on_stream_dropped=None, on_stream_restored=None,
        )
        m = MultiStream(control=None)
        # Drive the slot the way process-packet does.
        slot.resequencer.process_packet(_pkt(0, 5000))
        out, gaps = slot.resequencer.process_packet(_pkt(1, 5060))
        assert out is not None
        slot.pending_rtp_start = slot.resequencer.last_chunk_rtp_start
        slot.sample_buffer.append(out)
        m._deliver(slot)
        # First emission includes the initializing packet (ts 5000).
        self.assertEqual(got['label'], 5000)
        self.assertIsNone(slot.pending_rtp_start)


if __name__ == "__main__":
    unittest.main()
