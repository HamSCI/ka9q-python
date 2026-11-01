import unittest
from unittest.mock import MagicMock, patch
from ka9q.control import RadiodControl
from ka9q.discovery import ChannelInfo
from ka9q.types import Encoding

class TestEnsureChannelEncoding(unittest.TestCase):
    @patch('ka9q.control.RadiodControl._connect')
    def setUp(self, mock_connect):
        self.control = RadiodControl("radiod.local")
        self.control.create_channel = MagicMock()
        self.control.verify_channel = MagicMock(return_value=True)

    def test_ensure_channel_encoding_match(self):
        """Verify that matching encoding reuses channel"""
        # Mock existing channel with F32 encoding
        ssrc = 12345
        existing = ChannelInfo(
            ssrc=ssrc,
            preset="iq",
            sample_rate=16000,
            frequency=14074000.0,
            snr=0.0,
            multicast_address="239.1.1.1",
            port=5004,
            encoding=Encoding.F32
        )
        # ensure_channel probes for an existing channel via poll_channel;
        # return the matching F32 channel so the reuse branch runs.
        self.control.poll_channel = MagicMock(return_value=existing)

        # Patch allocate_ssrc to return our mock SSRC
        with patch('ka9q.control.allocate_ssrc', return_value=ssrc):
            # Call ensure_channel asking for F32
            result = self.control.ensure_channel(
                frequency_hz=14.074e6,
                destination="239.1.1.1",
                encoding=Encoding.F32
            )

            # Should return existing channel
            self.assertEqual(result, existing)
            # Should NOT call create_channel
            self.control.create_channel.assert_not_called()

    def test_ensure_channel_encoding_mismatch(self):
        """Verify that mismatching encoding triggers recreation"""
        # Mock existing channel with S16 encoding
        ssrc = 12345
        existing = ChannelInfo(
            ssrc=ssrc,
            preset="iq",
            sample_rate=16000,
            frequency=14074000.0,
            snr=0.0,
            multicast_address="239.1.1.1",
            port=5004,
            encoding=Encoding.S16LE # Different encoding
        )
        # poll_channel finds the existing S16 channel; the encoding
        # mismatch makes ensure_channel fall through to create_channel.
        self.control.poll_channel = MagicMock(return_value=existing)

        # Patch allocate_ssrc to return our mock SSRC
        with patch('ka9q.control.allocate_ssrc', return_value=ssrc):
            # Call ensure_channel asking for F32
            result = self.control.ensure_channel(
                frequency_hz=14.074e6,
                destination="239.1.1.1",
                encoding=Encoding.F32
            )

            # Should call create_channel with correct encoding
            self.control.create_channel.assert_called_once()
            _, kwargs = self.control.create_channel.call_args
            self.assertEqual(kwargs['encoding'], Encoding.F32)


def _info(ssrc, encoding):
    return ChannelInfo(ssrc=ssrc, preset="iq", sample_rate=16000,
                       frequency=14074000.0, snr=0.0,
                       multicast_address="239.1.1.1", port=5004,
                       encoding=encoding)


class TestEnsureChannelEncodingSettles(unittest.TestCase):
    """The first status after create can predate OUTPUT_ENCODING.

    Found 2026-11-01: codar-sounder on AC0G-ND and UACNJ got encoding S16
    back for an F32 channel and decoded 4-byte floats as 2-byte ints for a
    whole day.  ensure_channel must wait for the requested encoding.
    """

    SSRC = 12345

    @patch('ka9q.control.RadiodControl._connect')
    def setUp(self, mock_connect):
        self.control = RadiodControl("radiod.local")
        self.control.create_channel = MagicMock()
        self.control._ENCODING_SETTLE_POLL_S = 0.0

    def _ensure(self, encoding=Encoding.F32, timeout=0.5):
        with patch('ka9q.control.allocate_ssrc', return_value=self.SSRC):
            return self.control.ensure_channel(
                frequency_hz=14.074e6, destination="239.1.1.1",
                encoding=encoding, timeout=timeout)

    def test_stale_first_status_is_replaced_by_the_settled_one(self):
        # probe (no channel yet), first verify (stale S16), then F32
        self.control.poll_channel = MagicMock(side_effect=[
            None, _info(self.SSRC, Encoding.S16BE),
            _info(self.SSRC, Encoding.S16BE), _info(self.SSRC, Encoding.F32)])
        result = self._ensure()
        self.assertEqual(result.encoding, Encoding.F32)
        self.assertEqual(self.control.poll_channel.call_count, 4)

    def test_encoding_that_never_settles_returns_radiods_value_and_logs(self):
        def poll(*a, **kw):
            return None if poll.n == 0 else _info(self.SSRC, Encoding.S16BE)
        poll.n = 0
        def counting(*a, **kw):
            r = poll(*a, **kw); poll.n += 1; return r
        self.control.poll_channel = MagicMock(side_effect=counting)
        with self.assertLogs('ka9q.control', level='ERROR') as logs:
            result = self._ensure(timeout=0.2)
        self.assertEqual(result.encoding, Encoding.S16BE)
        self.assertTrue(any("still reports encoding" in m for m in logs.output))

    def test_matching_first_status_polls_no_more(self):
        self.control.poll_channel = MagicMock(side_effect=[
            None, _info(self.SSRC, Encoding.F32)])
        self.assertEqual(self._ensure().encoding, Encoding.F32)
        self.assertEqual(self.control.poll_channel.call_count, 2)

    def test_no_encoding_requested_takes_the_first_status(self):
        self.control.poll_channel = MagicMock(side_effect=[
            None, _info(self.SSRC, Encoding.S16BE)])
        self.assertEqual(self._ensure(encoding=0).encoding, Encoding.S16BE)
        self.assertEqual(self.control.poll_channel.call_count, 2)

if __name__ == '__main__':
    unittest.main()
