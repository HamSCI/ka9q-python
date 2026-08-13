import unittest
from unittest.mock import MagicMock
from ka9q.control import RadiodControl
from ka9q.types import Encoding

class TestCreateChannelSplit(unittest.TestCase):
    @unittest.mock.patch('ka9q.control.RadiodControl._connect')
    def test_create_channel_splits_encoding(self, mock_connect):
        """Verify that create_channel sends two packets when encoding is specified"""
        control = RadiodControl("radiod.local", client_id="unit-test")
        control.send_command = MagicMock()
        
        # 1. Create channel WITH encoding
        control.create_channel(
            frequency_hz=14074000.0,
            encoding=Encoding.F32
        )
        
        # Should call send_command twice
        self.assertEqual(control.send_command.call_count, 2, "Should call send_command twice")
        
        # 2. Create channel WITHOUT encoding
        control.send_command.reset_mock()
        control.create_channel(
            frequency_hz=14074000.0,
            encoding=Encoding.NO_ENCODING # or 0
        )
        
        # Should call send_command once
        self.assertEqual(control.send_command.call_count, 1, "Should call send_command once")

if __name__ == '__main__':
    unittest.main()
