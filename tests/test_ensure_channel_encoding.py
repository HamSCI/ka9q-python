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
                encoding=Encoding.F32
            )
            
            # Should call create_channel with correct encoding
            self.control.create_channel.assert_called_once()
            _, kwargs = self.control.create_channel.call_args
            self.assertEqual(kwargs['encoding'], Encoding.F32)

if __name__ == '__main__':
    unittest.main()
