"""
Tests for channel removal functionality
"""
import pytest
from unittest.mock import MagicMock, patch
from ka9q.control import RadiodControl, encode_double, StatusType, CMD
from ka9q.exceptions import ValidationError


class TestRemoveChannel:
    """Tests for remove_channel method"""
    
    def test_remove_channel_encodes_zero_frequency(self):
        """Test that remove_channel sends frequency=0"""
        # Create mock control
        control = RadiodControl.__new__(RadiodControl)
        control.status_address = "test.local"
        control.socket = MagicMock()
        control.dest_addr = ("239.1.2.3", 5006)
        control._socket_lock = __import__('threading').RLock()
        control.max_commands_per_sec = 100
        control._command_count = 0
        control._command_window_start = __import__('time').time()
        control._rate_limit_lock = __import__('threading').Lock()
        control.metrics = MagicMock()
        
        ssrc = 14074000
        
        # Mock send_command to capture the buffer
        sent_buffer = None
        def capture_send(buffer):
            nonlocal sent_buffer
            sent_buffer = buffer
        
        with patch.object(control, 'send_command', side_effect=capture_send):
            control.remove_channel(ssrc)
        
        # Verify buffer was sent
        assert sent_buffer is not None
        assert len(sent_buffer) > 0
        
        # Verify buffer starts with CMD byte
        assert sent_buffer[0] == CMD
        
        # Verify frequency=0.0 is encoded in the buffer
        # We can check by looking for the RADIO_FREQUENCY type
        assert StatusType.RADIO_FREQUENCY in sent_buffer
    
    def test_remove_channel_validates_ssrc(self):
        """Test that remove_channel validates SSRC"""
        control = RadiodControl.__new__(RadiodControl)
        control.status_address = "test.local"
        
        # Invalid SSRCs should raise ValidationError
        with pytest.raises(ValidationError, match="Invalid SSRC"):
            control.remove_channel(-1)
        
        with pytest.raises(ValidationError, match="Invalid SSRC"):
            control.remove_channel(2**32)  # Too large
        
        with pytest.raises(ValidationError, match="must be an integer"):
            control.remove_channel("12345")  # Wrong type
    
    def test_remove_channel_logs_correctly(self):
        """Test that remove_channel logs the action"""
        control = RadiodControl.__new__(RadiodControl)
        control.status_address = "test.local"
        control.socket = MagicMock()
        control.dest_addr = ("239.1.2.3", 5006)
        control._socket_lock = __import__('threading').RLock()
        control.max_commands_per_sec = 100
        control._command_count = 0
        control._command_window_start = __import__('time').time()
        control._rate_limit_lock = __import__('threading').Lock()
        control.metrics = MagicMock()
        
        ssrc = 14074000
        
        with patch.object(control, 'send_command'):
            with patch('ka9q.control.logger') as mock_logger:
                control.remove_channel(ssrc)
                
                # Verify logging
                mock_logger.info.assert_called_once()
                log_message = mock_logger.info.call_args[0][0]
                assert "Removing channel" in log_message
                assert str(ssrc) in log_message
    
    def test_remove_channel_with_valid_ssrcs(self):
        """Test remove_channel with various valid SSRCs"""
        control = RadiodControl.__new__(RadiodControl)
        control.status_address = "test.local"
        control.socket = MagicMock()
        control.dest_addr = ("239.1.2.3", 5006)
        control._socket_lock = __import__('threading').RLock()
        control.max_commands_per_sec = 100
        control._command_count = 0
        control._command_window_start = __import__('time').time()
        control._rate_limit_lock = __import__('threading').Lock()
        control.metrics = MagicMock()
        
        valid_ssrcs = [
            0,           # Minimum
            12345678,    # Typical
            14074000,    # Frequency-based
            0xFFFFFFFF,  # Maximum
        ]
        
        for ssrc in valid_ssrcs:
            with patch.object(control, 'send_command') as mock_send:
                control.remove_channel(ssrc)
                # Verify command was sent
                mock_send.assert_called_once()


class TestChannelLifecycle:
    """Integration-style tests for channel lifecycle"""
    
    def test_create_and_remove_pattern(self):
        """Test typical create-use-remove pattern"""
        control = RadiodControl.__new__(RadiodControl)
        control.status_address = "test.local"
        control.socket = MagicMock()
        control.dest_addr = ("239.1.2.3", 5006)
        control._socket_lock = __import__('threading').RLock()
        control.max_commands_per_sec = 100
        control._command_count = 0
        control._command_window_start = __import__('time').time()
        control._rate_limit_lock = __import__('threading').Lock()
        control.metrics = MagicMock()
        control.client_id = "unit-test"

        ssrc = 14074000

        # Track commands sent
        commands = []
        def track_command(buffer):
            commands.append(buffer)
        
        with patch.object(control, 'send_command', side_effect=track_command):
            # Create channel
            control.create_channel(
                ssrc=ssrc,
                frequency_hz=14.074e6,
                preset="usb"
            )
            
            # Remove channel
            control.remove_channel(ssrc)
        
        # Verify both commands were sent
        assert len(commands) == 2
        
        # First command: create (non-zero frequency)
        # Second command: remove (zero frequency)
        # We can verify the second one is the remove by checking it's shorter
        # (remove only sets frequency=0 and SSRC, create sets many parameters)
        assert len(commands[1]) < len(commands[0])


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
