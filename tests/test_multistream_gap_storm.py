"""MultiStream gap-storm detection: a wedged subscription that keeps
receiving packets but resequences gaps at a pathological rate must be
treated like a drop (so the existing restore path re-subscribes).
"""
import unittest

from ka9q.multi_stream import MultiStream, _ChannelSlot
from ka9q.resequencer import PacketResequencer
from ka9q.stream_quality import StreamQuality


def _slot():
    return _ChannelSlot(
        channel_info=None, frequency_hz=14_074_000.0, preset="usb",
        sample_rate=12000, encoding=2, is_iq=False,
        resequencer=PacketResequencer(
            buffer_size=64, samples_per_packet=320, sample_rate=12000,
        ),
        quality=StreamQuality(), on_samples=None,
        on_stream_dropped=None, on_stream_restored=None,
    )


class GapStormTests(unittest.TestCase):
    def setUp(self):
        self.m = MultiStream(control=None, gap_storm_rate_per_sec=25.0,
                             gap_storm_window_sec=3.0)
        self.slot = _slot()

    def test_fires_after_sustained_storm(self):
        fired = []
        for _ in range(4):
            self.slot.resequencer.stats.gaps_detected += 100   # 100/s >> 25/s
            fired.append(self.m._gap_storm(self.slot, 1.0))
        self.assertEqual(fired[:2], [False, False])
        self.assertTrue(fired[2], "should fire once the storm spans the window")

    def test_low_rate_never_fires(self):
        for _ in range(10):
            self.slot.resequencer.stats.gaps_detected += 2     # 2/s << 25/s
            self.assertFalse(self.m._gap_storm(self.slot, 1.0))
        self.assertEqual(self.slot.gap_storm_secs, 0.0)

    def test_storm_resets_when_rate_drops(self):
        self.slot.resequencer.stats.gaps_detected += 100
        self.m._gap_storm(self.slot, 1.0)                      # one storm tick
        self.assertGreater(self.slot.gap_storm_secs, 0.0)
        self.assertFalse(self.m._gap_storm(self.slot, 1.0))    # no new gaps → reset
        self.assertEqual(self.slot.gap_storm_secs, 0.0)

    def test_window_defaults_to_drop_timeout(self):
        m = MultiStream(control=None, drop_timeout_sec=15.0)
        self.assertEqual(m._gap_storm_window, 15.0)


if __name__ == "__main__":
    unittest.main()
