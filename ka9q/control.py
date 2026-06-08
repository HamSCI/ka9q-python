"""
General-purpose radiod control interface via TLV command protocol

This module provides a complete interface to radiod's control protocol using
TLV (Type-Length-Value) encoding, based on ka9q-radio's status.c/status.h.

ARCHITECTURE:
- This module exposes ALL radiod capabilities and parameters
- Application-specific defaults should be implemented in higher-level modules
  or configuration files
- Reusable for any application needing radiod channel control

USAGE:
1. Create RadiodControl instance with status address
2. Use granular setters (set_frequency, set_preset, etc.) OR
3. Use create_and_configure_channel() for common channel creation patterns

PARAMETERS:
- All StatusType enum values from ka9q-radio/status.h are supported
- See individual methods for parameter descriptions and valid ranges
"""

import socket
import select
import struct
import secrets
import logging
import threading
import hashlib
import re
import time
from dataclasses import dataclass, field
from typing import Optional, Dict, Union
from .types import StatusType, CMD
from .discovery import discover_channels
from .exceptions import ConnectionError, CommandError, ValidationError
from .utils import resolve_multicast_address

logger = logging.getLogger(__name__)


@dataclass
class Metrics:
    """Metrics for monitoring RadiodControl operations"""
    commands_sent: int = 0
    commands_failed: int = 0
    status_received: int = 0
    last_error: str = ""
    last_error_time: float = 0.0
    errors_by_type: Dict[str, int] = field(default_factory=dict)
    
    def to_dict(self) -> dict:
        """Convert metrics to dictionary for easy inspection"""
        total = max(1, self.commands_sent)  # Avoid division by zero
        return {
            'commands_sent': self.commands_sent,
            'commands_failed': self.commands_failed,
            'commands_succeeded': self.commands_sent - self.commands_failed,
            'success_rate': (self.commands_sent - self.commands_failed) / total,
            'status_received': self.status_received,
            'last_error': self.last_error,
            'last_error_time': self.last_error_time,
            'errors_by_type': dict(self.errors_by_type),
        }


# SSRC allocation function - compatible with signal-recorder
def allocate_ssrc(
    frequency_hz: float,
    preset: str = "iq",
    sample_rate: int = 16000,
    agc: bool = False,
    gain: float = 0.0,
    destination: Optional[str] = None,
    encoding: int = 0,
    radiod_host: Optional[str] = None
) -> int:
    """
    Allocate a deterministic SSRC from channel parameters.

    This function generates a consistent SSRC for a given set of channel
    parameters. The same parameters will always produce the same SSRC,
    enabling stream sharing and coordination between applications.

    The algorithm matches signal-recorder's StreamSpec.ssrc_hash() for
    cross-library compatibility.

    Args:
        frequency_hz: Center frequency in Hz
        preset: Demodulation mode ("iq", "usb", "lsb", "am", "fm", "cw")
        sample_rate: Output sample rate in Hz
        agc: Automatic gain control enabled
        gain: Manual gain in dB
        destination: RTP destination multicast address (optional)
        encoding: Output encoding type
        radiod_host: Optional radiod hostname or address. When provided the
            hash includes the radiod identity so the same client parameters
            talking to different radiod instances produce distinct SSRCs.

    Returns:
        Deterministic 31-bit positive SSRC value

    Example:
        >>> ssrc = allocate_ssrc(10.0e6, "iq", 16000)
        >>> print(f"SSRC for 10 MHz IQ @ 16kHz: {ssrc}")

        # Same parameters always give same SSRC
        >>> ssrc2 = allocate_ssrc(10.0e6, "iq", 16000)
        >>> assert ssrc == ssrc2
    """
    # Create a stable string key from parameters
    # The format and rounding match signal-recorder's StreamSpec hash logic.
    key_str = (
        f"{round(frequency_hz)}|"      # Frequency rounded to nearest Hz
        f"{preset.lower()}|"           # Preset normalized to lowercase
        f"{sample_rate}|"              # Sample rate
        f"{'1' if agc else '0'}|"      # AGC as 1/0
        f"{round(gain, 1)}|"           # Gain rounded to 0.1 dB
        f"{destination or ''}|"        # Destination (empty string if None)
        f"{encoding}|"                 # Encoding type
        f"{radiod_host or ''}"         # Radiod instance identity
    )
    
    # Use SHA-256 for a stable, platform-independent hash
    h = hashlib.sha256(key_str.encode()).digest()
    
    # Convert first 4 bytes to integer (big-endian)
    # Keep positive and 31 bits to match signal-recorder's SSRC range
    ssrc_full = int.from_bytes(h[:4], byteorder='big')
    return ssrc_full & 0x7FFFFFFF


# Input validation functions
def _validate_ssrc(ssrc: int) -> None:
    """Validate SSRC fits in 32-bit unsigned integer"""
    if not isinstance(ssrc, int):
        raise ValidationError(f"SSRC must be an integer, got {type(ssrc).__name__}")
    if not (0 <= ssrc <= 0xFFFFFFFF):
        raise ValidationError(f"Invalid SSRC: {ssrc} (must be 0-4294967295)")


def _validate_frequency(freq_hz: float) -> None:
    """Validate frequency is within reasonable SDR range"""
    if not isinstance(freq_hz, (int, float)):
        raise ValidationError(f"Frequency must be a number, got {type(freq_hz).__name__}")
    if not (0 < freq_hz < 10e12):  # 10 THz max
        raise ValidationError(f"Invalid frequency: {freq_hz} Hz (must be 0 < freq < 10 THz)")


def _validate_sample_rate(rate: int) -> None:
    """Validate sample rate is positive and reasonable"""
    if not isinstance(rate, int):
        raise ValidationError(f"Sample rate must be an integer, got {type(rate).__name__}")
    if not (1 <= rate <= 100e6):  # 100 MHz max
        raise ValidationError(f"Invalid sample rate: {rate} Hz (must be 1-100000000)")


def _validate_timeout(timeout: float) -> None:
    """Validate timeout is positive"""
    if not isinstance(timeout, (int, float)):
        raise ValidationError(f"Timeout must be a number, got {type(timeout).__name__}")
    if timeout <= 0:
        raise ValidationError(f"Timeout must be positive, got {timeout}")


def _validate_gain(gain_db: float) -> None:
    """Validate gain is within reasonable range"""
    if not isinstance(gain_db, (int, float)):
        raise ValidationError(f"Gain must be a number, got {type(gain_db).__name__}")
    if not (-100 <= gain_db <= 100):  # Reasonable range for most SDRs
        raise ValidationError(f"Invalid gain: {gain_db} dB (must be -100 to +100)")


def _validate_positive(value: float, name: str) -> None:
    """Validate that a value is positive"""
    if not isinstance(value, (int, float)):
        raise ValidationError(f"{name} must be a number, got {type(value).__name__}")
    if value <= 0:
        raise ValidationError(f"{name} must be positive, got {value}")


def _validate_preset(preset: str) -> None:
    """
    Validate preset name is safe and within reasonable bounds
    
    Args:
        preset: Preset name to validate
        
    Raises:
        ValidationError: If preset name is invalid
    """
    if not isinstance(preset, str):
        raise ValidationError(f"Preset must be a string, got {type(preset).__name__}")
    if not preset:
        raise ValidationError("Preset name cannot be empty")
    if len(preset) > 32:
        raise ValidationError(f"Preset name too long: {len(preset)} chars (max 32)")
    # Check control characters FIRST (before regex)
    if any(ord(c) < 32 or ord(c) == 127 for c in preset):
        raise ValidationError(f"Preset name contains control characters")
    # Allow alphanumeric, dash, underscore only
    if not re.match(r'^[a-zA-Z0-9_-]+$', preset):
        raise ValidationError(f"Invalid preset name '{preset}': only alphanumeric, dash, and underscore allowed")


def _validate_string_param(value: str, param_name: str, max_length: int = 256) -> None:
    """
    Validate a generic string parameter
    
    Args:
        value: String value to validate
        param_name: Name of parameter (for error messages)
        max_length: Maximum allowed length
        
    Raises:
        ValidationError: If string is invalid
    """
    if not isinstance(value, str):
        raise ValidationError(f"{param_name} must be a string, got {type(value).__name__}")
    if not value:
        raise ValidationError(f"{param_name} cannot be empty")
    if len(value) > max_length:
        raise ValidationError(f"{param_name} too long: {len(value)} chars (max {max_length})")
    # Check null bytes FIRST
    if '\x00' in value:
        raise ValidationError(f"{param_name} contains null bytes")
    # Then check other control characters (except newline/tab if needed)
    if any(ord(c) < 32 and c not in '\n\t' for c in value):
        raise ValidationError(f"{param_name} contains control characters")


def _validate_multicast_address(address: str) -> None:
    """
    Validate multicast address format
    
    Args:
        address: Multicast address (IP or hostname)
        
    Raises:
        ValidationError: If address format is invalid
    """
    if not isinstance(address, str):
        raise ValidationError(f"Multicast address must be a string, got {type(address).__name__}")
    if not address:
        raise ValidationError("Multicast address cannot be empty")
    
    # Try to parse as IP address
    try:
        # Simple validation - socket.inet_aton will catch malformed addresses
        socket.inet_aton(address)
    except OSError:
        # Not a valid IP, could be hostname - allow it
        # radiod will resolve it or hash it to generate multicast address
        if len(address) > 255:
            raise ValidationError(f"Hostname too long: {len(address)} chars (max 255)")
        if '\x00' in address:
            raise ValidationError("Multicast address contains null bytes")


def encode_int64(buf: bytearray, type_val: int, x: int) -> int:
    """
    Encode a 64-bit integer in TLV format
    
    Format: [type:1][length:1][value:variable]
    Value is big-endian, with leading zeros compressed
    
    Args:
        buf: Buffer to write to
        type_val: TLV type identifier
        x: Integer value (must be 0 <= x <= 2^64-1)
        
    Raises:
        ValidationError: If x is negative or too large
    """
    if x < 0:
        raise ValidationError(f"Cannot encode negative integer: {x}")
    if x >= 2**64:
        raise ValidationError(f"Integer too large for 64-bit encoding: {x}")
    
    buf.append(type_val)
    
    if x == 0:
        # Compress zero to zero length
        buf.append(0)
        return 2
    
    # Convert to bytes and remove leading zeros
    x_bytes = x.to_bytes(8, byteorder='big')
    # Find first non-zero byte
    start = 0
    while start < len(x_bytes) and x_bytes[start] == 0:
        start += 1
    
    value_bytes = x_bytes[start:]
    length = len(value_bytes)
    
    buf.append(length)
    buf.extend(value_bytes)
    
    return 2 + length


def encode_int(buf: bytearray, type_val: int, x: int) -> int:
    """
    Encode an integer in TLV format (alias for encode_int64)
    
    Args:
        buf: Buffer to write to
        type_val: TLV type identifier
        x: Integer value (must be 0 <= x <= 2^64-1)
        
    Returns:
        Number of bytes written (2 + value length)
        
    Raises:
        ValidationError: If x is negative or too large
    """
    return encode_int64(buf, type_val, x)


def encode_double(buf: bytearray, type_val: int, x: float) -> int:
    """
    Encode a double-precision float (float64) in TLV format
    
    Converts the float to IEEE 754 double-precision format, then encodes
    it as a 64-bit integer in TLV format.
    
    Args:
        buf: Buffer to write to
        type_val: TLV type identifier
        x: Float value to encode
        
    Returns:
        Number of bytes written (2 + value length)
        
    Example:
        >>> buf = bytearray()
        >>> encode_double(buf, StatusType.RADIO_FREQUENCY, 14.074e6)
        10
    """
    # Pack as double, unpack as uint64
    packed = struct.pack('>d', x)  # big-endian double
    value = struct.unpack('>Q', packed)[0]  # big-endian uint64
    return encode_int64(buf, type_val, value)


def encode_float(buf: bytearray, type_val: int, x: float) -> int:
    """
    Encode a single-precision float (float32) in TLV format
    
    Converts the float to IEEE 754 single-precision format, then encodes
    it as a 32-bit integer in TLV format.
    
    Args:
        buf: Buffer to write to
        type_val: TLV type identifier
        x: Float value to encode
        
    Returns:
        Number of bytes written (2 + value length)
        
    Note:
        Single-precision floats have less precision than doubles.
        Use encode_double() for better precision when needed.
    """
    # Pack as float, unpack as uint32
    packed = struct.pack('>f', x)  # big-endian float
    value = struct.unpack('>I', packed)[0]  # big-endian uint32
    return encode_int64(buf, type_val, value)


def encode_string(buf: bytearray, type_val: int, s: str) -> int:
    """
    Encode a UTF-8 string in TLV format
    
    Strings are encoded with variable-length encoding:
    - Length < 128: single-byte length
    - Length >= 128: multi-byte length (0x80 | high_byte, low_byte)
    
    Args:
        buf: Buffer to write to
        type_val: TLV type identifier
        s: String to encode (will be converted to UTF-8)
        
    Returns:
        Number of bytes written (2 + string length)
        
    Raises:
        ValueError: If string is longer than 65535 bytes
        
    Example:
        >>> buf = bytearray()
        >>> encode_string(buf, StatusType.PRESET, "usb")
        5
    """
    buf.append(type_val)
    
    s_bytes = s.encode('utf-8')
    length = len(s_bytes)
    
    if length < 128:
        buf.append(length)
    elif length < 65536:
        # Multi-byte length encoding
        buf.append(0x80 | (length >> 8))
        buf.append(length & 0xff)
    else:
        raise ValueError(f"String too long: {length} bytes")
    
    buf.extend(s_bytes)
    return 2 + length


def encode_socket(buf: bytearray, type_val: int, address: str, port: int = 5004) -> int:
    """
    Encode a socket address (IPv4) in TLV format
    
    Format matches radiod's decode_socket expectations:
    - Address: 4 bytes (IPv4 address in network order)
    - Port: 2 bytes (big-endian)
    Total: 6 bytes for IPv4
    
    Note: Family (AF_INET/AF_INET6) is inferred from length by radiod:
    - length 6 = IPv4
    - length 10 = IPv6 (not currently supported)
    
    Args:
        buf: Buffer to write to
        type_val: TLV type identifier
        address: IP address as string (e.g., "239.1.2.3")
        port: Port number (default: 5004 for RTP)
        
    Returns:
        Number of bytes written (8 = type + length + 6 bytes data)
        
    Raises:
        ValidationError: If address format is invalid
        
    Example:
        >>> buf = bytearray()
        >>> encode_socket(buf, StatusType.OUTPUT_DATA_DEST_SOCKET, "239.1.2.3", 5004)
        8
    """
    buf.append(type_val)
    
    # Encode the socket address
    try:
        # Convert string IP to 4 bytes (already in network order)
        addr_bytes = socket.inet_aton(address)
    except OSError as e:
        raise ValidationError(f"Invalid IP address '{address}': {e}")
    
    # Validate port range
    if not (0 <= port <= 65535):
        raise ValidationError(f"Invalid port {port} (must be 0-65535)")
    
    # Format: address(4 bytes) + port(2 bytes, network order)
    # This matches radiod's decode_socket() expectations
    buf.append(6)  # length for IPv4
    buf.extend(addr_bytes)  # 4 bytes address (already in network order from inet_aton)
    buf.extend(struct.pack('>H', port))  # 2 bytes port (big-endian)
    
    return 2 + 6  # type + length + data


def encode_eol(buf: bytearray) -> int:
    """
    Encode end-of-list marker
    
    Every TLV command must end with an EOL marker to signal
    the end of the parameter list.
    
    Args:
        buf: Buffer to write to
        
    Returns:
        Number of bytes written (always 1)
    """
    buf.append(StatusType.EOL)
    return 1


# Decode functions for parsing TLV responses

def decode_int(data: bytes, length: int) -> int:
    """
    Decode an integer from TLV response
    
    Args:
        data: Bytes to decode (variable length, big-endian)
        length: Number of bytes
        
    Returns:
        Integer value
        
    Raises:
        ValidationError: If length is negative or data is insufficient
    """
    # Validate length
    if length < 0:
        raise ValidationError(f"Negative length in decode_int: {length}")
    if length == 0:
        return 0
    if length > 8:
        logger.warning(f"Integer length {length} exceeds 8 bytes, truncating")
        length = 8
    if len(data) < length:
        raise ValidationError(f"Insufficient data: need {length} bytes, have {len(data)}")
    
    value = 0
    for i in range(length):
        value = (value << 8) | data[i]
    return value


def decode_int32(data: bytes, length: int) -> int:
    """
    Decode a 32-bit integer from TLV response (alias for decode_int)
    
    Args:
        data: Bytes to decode (variable length, big-endian)
        length: Number of bytes
        
    Returns:
        Integer value
    """
    return decode_int(data, length)


def decode_int64(data: bytes, length: int) -> int:
    """
    Decode a 64-bit integer from TLV response (alias for decode_int)
    
    Args:
        data: Bytes to decode (variable length, big-endian)
        length: Number of bytes
        
    Returns:
        Integer value (up to 64-bit)
    """
    return decode_int(data, length)


def decode_float(data: bytes, length: int) -> float:
    """
    Decode a float (float32) from TLV response
    
    Args:
        data: Bytes to decode (big-endian IEEE 754)
        length: Number of bytes (should be 4 or less with leading zeros stripped)
        
    Returns:
        Float value
        
    Raises:
        ValidationError: If length is negative or data is insufficient
    """
    # Validate length
    if length < 0:
        raise ValidationError(f"Negative length in decode_float: {length}")
    if length > 4:
        logger.warning(f"Float length {length} exceeds 4 bytes, truncating")
        length = 4
    if len(data) < length:
        raise ValidationError(f"Insufficient data: need {length} bytes, have {len(data)}")
    
    # Reconstruct 4-byte big-endian representation
    value_bytes = b'\x00' * (4 - length) + data[:length]
    return struct.unpack('>f', value_bytes)[0]


def decode_double(data: bytes, length: int) -> float:
    """
    Decode a double (float64) from TLV response
    
    Args:
        data: Bytes to decode (big-endian IEEE 754)
        length: Number of bytes (should be 8 or less with leading zeros stripped)
        
    Returns:
        Float value
        
    Raises:
        ValidationError: If length is negative or data is insufficient
    """
    # Validate length
    if length < 0:
        raise ValidationError(f"Negative length in decode_double: {length}")
    if length > 8:
        logger.warning(f"Double length {length} exceeds 8 bytes, truncating")
        length = 8
    if len(data) < length:
        raise ValidationError(f"Insufficient data: need {length} bytes, have {len(data)}")
    
    # Reconstruct 8-byte big-endian representation
    value_bytes = b'\x00' * (8 - length) + data[:length]
    return struct.unpack('>d', value_bytes)[0]


def decode_bool(data: bytes, length: int) -> bool:
    """
    Decode a boolean value from TLV response
    
    Args:
        data: Bytes to decode
        length: Number of bytes
        
    Returns:
        True if non-zero, False if zero
    """
    return decode_int(data, length) != 0


def decode_string(data: bytes, length: int) -> str:
    """
    Decode a UTF-8 string from TLV response
    
    Args:
        data: Bytes to decode
        length: String length in bytes
        
    Returns:
        Decoded string
        
    Raises:
        ValidationError: If length is negative or data is insufficient
    """
    # Validate length
    if length < 0:
        raise ValidationError(f"Negative length in decode_string: {length}")
    if length > 65535:
        logger.warning(f"String length {length} exceeds maximum, truncating to 65535")
        length = 65535
    if len(data) < length:
        logger.warning(f"String data truncated: expected {length} bytes, have {len(data)}")
        length = len(data)
    
    return data[:length].decode('utf-8', errors='replace')


def decode_socket(data: bytes, length: int) -> dict:
    """
    Decode a socket address from TLV response
    
    Args:
        data: Bytes containing socket address
        length: Length of socket data
        
    Returns:
        Dictionary with 'family', 'address', and 'port' keys
    
    Note:
        Handles two formats:
        - With family field: 2 bytes family + 2 bytes port + N bytes address (length 8 for IPv4, 18 for IPv6)
        - Without family field: N bytes address + 2 bytes port (length 6 for IPv4, 10 for IPv6)
    """
    if length == 8:
        # Format WITH family field: family(2) + port(2) + address(4) for IPv4
        family = struct.unpack('>H', data[0:2])[0]
        port = struct.unpack('>H', data[2:4])[0]
        # Check if it's actually IPv4 (AF_INET = 2)
        if family == 2:
            address = socket.inet_ntoa(data[4:8])
            return {'family': 'IPv4', 'address': address, 'port': port}
        else:
            # Unknown family
            return {'family': f'unknown (family={family})', 'address': '', 'port': port}
    elif length == 6:
        # Format WITHOUT family field: address(4) + port(2) for IPv4
        address = socket.inet_ntoa(data[0:4])
        port = struct.unpack('>H', data[4:6])[0]
        return {'family': 'IPv4', 'address': address, 'port': port}
    elif length == 10:
        # Format WITHOUT family field: address(8) + port(2) for IPv6 (truncated)
        # Note: This is a truncated IPv6 address (only 8 bytes instead of 16)
        address_bytes = data[0:8]
        port = struct.unpack('>H', data[8:10])[0]
        # Format as hex string since it's truncated
        address = ':'.join(f'{address_bytes[i]:02x}{address_bytes[i+1]:02x}' 
                          for i in range(0, 8, 2))
        return {'family': 'IPv6', 'address': address, 'port': port}
    else:
        return {'family': 'unknown', 'address': '', 'port': 0}


def decode_status_dict(buffer: bytes) -> dict:
    """Decode a radiod STATUS packet into a flat dictionary.

    Standalone module-level decoder — does NOT require a RadiodControl
    instance, so it's safe to call from contexts that should not open
    a control socket (notably :class:`ka9q.status_listener.StatusListener`,
    which would otherwise drag in a full control session just to parse
    bytes).

    Returns an empty dict for non-STATUS packets (first byte != 0).
    Unknown TLV tags are silently skipped.

    Used by :meth:`RadiodControl._decode_status_response` (which adds a
    metric bump on top of the same parse) and by the status listener.
    """
    status: dict = {}

    if len(buffer) == 0 or buffer[0] != 0:
        return status  # Not a status response

    cp = 1  # Skip packet type byte

    while cp < len(buffer):
        if cp >= len(buffer):
            break

        type_val = buffer[cp]
        cp += 1

        if type_val == StatusType.EOL:
            break

        if cp >= len(buffer):
            break

        optlen = buffer[cp]
        cp += 1

        # Handle extended length encoding
        if optlen & 0x80:
            length_of_length = optlen & 0x7f
            optlen = 0
            for _ in range(length_of_length):
                if cp >= len(buffer):
                    break
                optlen = (optlen << 8) | buffer[cp]
                cp += 1

        if cp + optlen > len(buffer):
            break

        data = buffer[cp:cp + optlen]

        # Decode based on type
        if type_val == StatusType.COMMAND_TAG:
            status['command_tag'] = decode_int32(data, optlen)
        elif type_val == StatusType.GPS_TIME:
            status['gps_time'] = decode_int64(data, optlen)
        elif type_val == StatusType.RTP_TIMESNAP:
            status['rtp_timesnap'] = decode_int32(data, optlen)
        elif type_val == StatusType.RADIO_FREQUENCY:
            status['frequency'] = decode_double(data, optlen)
        elif type_val == StatusType.OUTPUT_SSRC:
            status['ssrc'] = decode_int32(data, optlen)
        elif type_val == StatusType.AGC_ENABLE:
            status['agc_enable'] = decode_bool(data, optlen)
        elif type_val == StatusType.GAIN:
            status['gain'] = decode_float(data, optlen)
        elif type_val == StatusType.RF_GAIN:
            status['rf_gain'] = decode_float(data, optlen)
        elif type_val == StatusType.RF_ATTEN:
            status['rf_atten'] = decode_float(data, optlen)
        elif type_val == StatusType.RF_AGC:
            status['rf_agc'] = decode_int(data, optlen)
        elif type_val == StatusType.PRESET:
            status['preset'] = decode_string(data, optlen)
        elif type_val == StatusType.LOW_EDGE:
            status['low_edge'] = decode_float(data, optlen)
        elif type_val == StatusType.HIGH_EDGE:
            status['high_edge'] = decode_float(data, optlen)
        elif type_val == StatusType.NOISE_DENSITY:
            status['noise_density'] = decode_float(data, optlen)
        elif type_val == StatusType.BASEBAND_POWER:
            status['baseband_power'] = decode_float(data, optlen)
        elif type_val == StatusType.OUTPUT_SAMPRATE:
            status['sample_rate'] = decode_int(data, optlen)
        elif type_val == StatusType.OUTPUT_ENCODING:
            status['encoding'] = decode_int(data, optlen)
        elif type_val == StatusType.OUTPUT_DATA_DEST_SOCKET:
            status['destination'] = decode_socket(data, optlen)
        elif type_val == StatusType.OUTPUT_TTL:
            status['ttl'] = decode_int(data, optlen)
            if status['ttl'] == 0:
                logger.warning(
                    f"Radiod reporting TTL=0 for SSRC "
                    f"{status.get('ssrc', 'unknown')}: Multicast data "
                    f"restricted to localhost loopback only!"
                )
        elif type_val == StatusType.LIFETIME:
            status['lifetime'] = decode_int(data, optlen)
        elif type_val == StatusType.DESCRIPTION:
            status['description'] = decode_string(data, optlen)

        cp += optlen

    # Calculate SNR if we have the necessary data
    if all(k in status for k in
           ['baseband_power', 'low_edge', 'high_edge', 'noise_density']):
        import math
        bandwidth = abs(status['high_edge'] - status['low_edge'])

        if bandwidth > 0:
            try:
                noise_power_db = (
                    status['noise_density'] + 10 * math.log10(bandwidth)
                )
                signal_plus_noise_db = status['baseband_power']
                noise_power = 10 ** (noise_power_db / 10)
                signal_plus_noise = 10 ** (signal_plus_noise_db / 10)

                if noise_power > 0:
                    snr_linear = signal_plus_noise / noise_power - 1
                    if snr_linear > 0:
                        status['snr'] = 10 * math.log10(snr_linear)
            except (ValueError, ZeroDivisionError, OverflowError):
                pass

    return status


class RadiodControl:
    """
    Control interface for radiod
    
    Sends TLV-encoded commands to radiod's control socket to create
    and configure channels.
    """
    
    def __init__(self, status_address: str, max_commands_per_sec: int = 100,
                 interface: Optional[str] = None,
                 client_id: Optional[str] = None):
        """
        Initialize radiod control

        Args:
            status_address: mDNS name or IP:port of radiod status stream
            max_commands_per_sec: Maximum commands per second (rate limiting)
            interface: IP address of network interface for multicast (e.g., '192.168.1.100').
                      Required on multi-homed systems. If None, uses INADDR_ANY (0.0.0.0).
            client_id: Identity string for this client application
                      (e.g. ``"psk-recorder"``, ``"hf-timestd"``).  When set,
                      :py:meth:`ensure_channel` derives a per-(client, radiod)
                      multicast destination via
                      :py:func:`ka9q.generate_multicast_ip` whenever the
                      caller does not supply an explicit ``destination=``.
                      Same client_id + same radiod -> same destination
                      (all of that client's channels share one multicast
                      group); different client_id OR different radiod ->
                      distinct destinations (peer clients on one station
                      never collide; one client targeting two radiods
                      sees one destination per radiod).  ``None`` (default)
                      preserves pre-3.14 behavior: when no ``destination=``
                      is passed, radiod uses its config-file default and
                      every client lands on the same group.
        """
        self.status_address = status_address
        self.interface = interface
        self.client_id = client_id
        self.socket = None
        self._status_sock = None  # Cached status listener socket for tune()
        self._status_sock_lock = None  # Will be initialized when needed
        self._socket_lock = threading.RLock()  # Protect control socket operations

        # Optional continuous STATUS listener (live timing anchor refresh).
        # Lazily created by start_status_listener(); see ka9q.status_listener.
        self._status_listener = None
        
        # Rate limiting
        self.max_commands_per_sec = max_commands_per_sec
        self._command_count = 0
        self._command_window_start = time.time()
        self._rate_limit_lock = threading.Lock()
        
        # Metrics tracking
        self.metrics = Metrics()
        
        self._connect()
    
    def __enter__(self):
        """Context manager entry"""
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit - ensure cleanup"""
        try:
            self.close()
        except Exception as e:
            logger.warning(f"Error during cleanup in context manager: {e}")
        return False  # Don't suppress exceptions
    
    def _connect(self):
        """Connect to radiod control socket"""
        try:
            # Resolve the status address using shared utility
            mcast_addr = resolve_multicast_address(self.status_address, timeout=5.0)
            
            # Create UDP socket
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            
            # Allow multiple sockets to bind to the same port
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            
            # Set socket options for multicast
            import struct
            
            # Determine interface address for multicast operations
            # Use specified interface for multi-homed systems, or INADDR_ANY otherwise
            interface_addr = self.interface if self.interface else '0.0.0.0'
            
            # Set multicast interface for sending
            self.socket.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, 
                                 socket.inet_aton(interface_addr))
            logger.debug(f"Set IP_MULTICAST_IF to {interface_addr}")
            
            # Join the multicast group on specified interface
            mreq = struct.pack('=4s4s', 
                              socket.inet_aton(mcast_addr),  # multicast group address
                              socket.inet_aton(interface_addr))  # interface to use
            try:
                self.socket.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
                logger.debug(f"Joined multicast group {mcast_addr} on interface {interface_addr}")
            except OSError as e:
                # EADDRINUSE is not fatal - group already joined
                if e.errno != 48:  # EADDRINUSE on macOS
                    logger.warning(f"Failed to join multicast group: {e}")
            
            # Enable multicast loopback
            self.socket.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
            # Set TTL for multicast packets
            self.socket.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
            
            # Store both status and control addresses
            # Status address is where we listen for status multicast
            # Control address is where we send commands (same as status for now)
            self.status_mcast_addr = mcast_addr
            self.dest_addr = (mcast_addr, 5006)  # Standard radiod control port
            
            logger.info(f"Connected to radiod at {mcast_addr}:5006")
            logger.debug(f"Status multicast: {self.status_mcast_addr}, Control: {self.dest_addr}")
            logger.debug(f"Socket options: REUSEADDR=1, MULTICAST_IF=INADDR_ANY, MULTICAST_LOOP=1, MULTICAST_TTL=2")
            
        except socket.error as e:
            logger.error(f"Socket error connecting to radiod: {e}")
            raise ConnectionError(f"Failed to create socket: {e}") from e
        except Exception as e:
            logger.error(f"Unexpected error connecting to radiod: {e}", exc_info=True)
            raise ConnectionError(f"Failed to connect to radiod: {e}") from e
    
    def _check_rate_limit(self):
        """
        Check and enforce rate limiting (thread-safe)
        
        Implements a sliding window rate limiter to prevent DoS attacks
        and network flooding.
        """
        with self._rate_limit_lock:
            now = time.time()
            
            # Reset window every second
            if now - self._command_window_start >= 1.0:
                self._command_count = 0
                self._command_window_start = now
            
            # Check limit
            if self._command_count >= self.max_commands_per_sec:
                sleep_time = 1.0 - (now - self._command_window_start)
                if sleep_time > 0:
                    logger.warning(
                        f"Rate limit reached ({self.max_commands_per_sec}/sec), "
                        f"sleeping {sleep_time:.3f}s"
                    )
                    time.sleep(sleep_time)
                    # Reset after sleeping
                    self._command_count = 0
                    self._command_window_start = time.time()
            
            self._command_count += 1
    
    def send_command(self, cmdbuffer: bytearray, max_retries: int = 3, retry_delay: float = 0.1):
        """
        Send a command packet to radiod with retry logic (thread-safe)
        
        Args:
            cmdbuffer: Command buffer to send
            max_retries: Maximum number of retry attempts (default: 3)
            retry_delay: Initial delay between retries in seconds (default: 0.1)
                        Uses exponential backoff: 0.1s, 0.2s, 0.4s, etc.
        
        Raises:
            RuntimeError: If not connected to radiod
            CommandError: If sending fails after all retries
        """
        import time
        
        with self._socket_lock:
            if not self.socket:
                raise RuntimeError("Not connected to radiod")
        
        # Apply rate limiting
        self._check_rate_limit()
        
        with self._socket_lock:
            attempt = 0
            last_error = None
            for attempt in range(max_retries):
                try:
                    # Log hex dump of the command
                    hex_dump = ' '.join(f'{b:02x}' for b in cmdbuffer)
                    logger.debug(f"Sending {len(cmdbuffer)} bytes to {self.dest_addr} (attempt {attempt+1}/{max_retries}): {hex_dump}")
                    
                    sent = self.socket.sendto(bytes(cmdbuffer), self.dest_addr)
                    logger.debug(f"Command sent successfully (attempt {attempt + 1})")
                    self.metrics.commands_sent += 1
                    return sent
                    
                except socket.error as e:
                    last_error = e
                    if attempt < max_retries - 1:
                        # Exponential backoff
                        delay = retry_delay * (2 ** attempt)
                        logger.warning(f"Socket error on attempt {attempt+1}/{max_retries}: {e}. Retrying in {delay:.2f}s...")
                        time.sleep(delay)
                    else:
                        logger.error(f"Failed to send command after {max_retries} attempts: {last_error}")
                        self.metrics.commands_sent += 1
                        self.metrics.commands_failed += 1
                        self.metrics.last_error = str(last_error)
                        self.metrics.last_error_time = time.time()
                        error_type = type(last_error).__name__
                        self.metrics.errors_by_type[error_type] = self.metrics.errors_by_type.get(error_type, 0) + 1
                        raise CommandError(f"Command failed after {max_retries} attempts") from last_error
                        
                except Exception as e:
                    logger.error(f"Unexpected error sending command: {e}", exc_info=True)
                    raise CommandError(f"Failed to send command: {e}") from e
            
            # Should not reach here, but just in case
            if last_error:
                raise CommandError(f"Failed to send command after {max_retries} attempts: {last_error}") from last_error
    
    def set_frequency(self, ssrc: int, frequency_hz: float):
        """
        Set the frequency of a channel
        
        Args:
            ssrc: SSRC of the channel
            frequency_hz: Frequency in Hz
        
        Raises:
            ValidationError: If parameters are invalid
        """
        _validate_ssrc(ssrc)
        _validate_frequency(frequency_hz)
        
        cmdbuffer = bytearray()
        cmdbuffer.append(CMD)  # Command packet type
        
        encode_double(cmdbuffer, StatusType.RADIO_FREQUENCY, frequency_hz)
        encode_int(cmdbuffer, StatusType.OUTPUT_SSRC, ssrc)
        encode_int(cmdbuffer, StatusType.COMMAND_TAG, secrets.randbits(31))
        encode_eol(cmdbuffer)
        
        logger.info(f"Setting frequency for SSRC {ssrc} to {frequency_hz/1e6:.3f} MHz")
        self.send_command(cmdbuffer)
    
    def set_preset(self, ssrc: int, preset: str):
        """
        Set the preset (mode) of a channel
        
        Args:
            ssrc: SSRC of the channel
            preset: Preset name (e.g., "iq", "usb", "lsb")
            
        Raises:
            ValidationError: If preset name is invalid
        """
        _validate_ssrc(ssrc)
        _validate_preset(preset)
        
        cmdbuffer = bytearray()
        cmdbuffer.append(CMD)
        
        encode_string(cmdbuffer, StatusType.PRESET, preset)
        encode_int(cmdbuffer, StatusType.OUTPUT_SSRC, ssrc)
        encode_int(cmdbuffer, StatusType.COMMAND_TAG, secrets.randbits(31))
        encode_eol(cmdbuffer)
        
        logger.info(f"Setting preset for SSRC {ssrc} to {preset}")
        self.send_command(cmdbuffer)
    
    def set_sample_rate(self, ssrc: int, sample_rate: int):
        """
        Set the sample rate of a channel
        
        Args:
            ssrc: SSRC of the channel
            sample_rate: Sample rate in Hz
        
        Raises:
            ValidationError: If parameters are invalid
        """
        _validate_ssrc(ssrc)
        _validate_sample_rate(sample_rate)
        
        cmdbuffer = bytearray()
        cmdbuffer.append(CMD)
        
        encode_int(cmdbuffer, StatusType.OUTPUT_SAMPRATE, sample_rate)
        encode_int(cmdbuffer, StatusType.OUTPUT_SSRC, ssrc)
        encode_int(cmdbuffer, StatusType.COMMAND_TAG, secrets.randbits(31))
        encode_eol(cmdbuffer)
        
        logger.info(f"Setting sample rate for SSRC {ssrc} to {sample_rate} Hz")
        self.send_command(cmdbuffer)
    
    def set_agc(self, ssrc: int, enable: bool, hangtime: Optional[float] = None,
                headroom: Optional[float] = None, recovery_rate: Optional[float] = None,
                threshold: Optional[float] = None):
        """
        Configure AGC (Automatic Gain Control) for a channel.

        radiod exposes four AGC knobs: enable, hangtime, headroom, recovery_rate,
        and threshold. There is no separate "attack rate" — see radio_status.c
        decode_radio_commands().

        Args:
            ssrc: SSRC of the channel
            enable: Enable/disable AGC (True=enabled, False=manual gain)
            hangtime: AGC hang time in seconds
            headroom: Target headroom in dB (positive; radiod negates internally)
            recovery_rate: AGC recovery rate in dB/sec
            threshold: AGC threshold in dB
        """
        cmdbuffer = bytearray()
        cmdbuffer.append(CMD)

        encode_int(cmdbuffer, StatusType.AGC_ENABLE, 1 if enable else 0)
        if hangtime is not None:
            encode_float(cmdbuffer, StatusType.AGC_HANGTIME, hangtime)
        if headroom is not None:
            encode_float(cmdbuffer, StatusType.HEADROOM, headroom)
        if recovery_rate is not None:
            encode_float(cmdbuffer, StatusType.AGC_RECOVERY_RATE, recovery_rate)
        if threshold is not None:
            encode_float(cmdbuffer, StatusType.AGC_THRESHOLD, threshold)

        encode_int(cmdbuffer, StatusType.OUTPUT_SSRC, ssrc)
        encode_int(cmdbuffer, StatusType.COMMAND_TAG, secrets.randbits(31))
        encode_eol(cmdbuffer)

        logger.info(f"Setting AGC for SSRC {ssrc}: enable={enable}")
        self.send_command(cmdbuffer)
    
    def set_gain(self, ssrc: int, gain_db: float):
        """
        Set manual gain for a channel (linear modes only)
        
        Args:
            ssrc: SSRC of the channel
            gain_db: Gain in dB
        
        Raises:
            ValidationError: If parameters are invalid
        """
        _validate_ssrc(ssrc)
        _validate_gain(gain_db)
        
        cmdbuffer = bytearray()
        cmdbuffer.append(CMD)
        
        encode_double(cmdbuffer, StatusType.GAIN, gain_db)
        encode_int(cmdbuffer, StatusType.OUTPUT_SSRC, ssrc)
        encode_int(cmdbuffer, StatusType.COMMAND_TAG, secrets.randbits(31))
        encode_eol(cmdbuffer)
        
        logger.info(f"Setting gain for SSRC {ssrc} to {gain_db} dB")
        self.send_command(cmdbuffer)
    
    def set_filter(self, ssrc: int, low_edge: Optional[float] = None, 
                   high_edge: Optional[float] = None, kaiser_beta: Optional[float] = None):
        """
        Configure filter parameters for a channel
        
        Args:
            ssrc: SSRC of the channel
            low_edge: Low frequency edge in Hz (optional)
            high_edge: High frequency edge in Hz (optional)
            kaiser_beta: Kaiser window beta parameter (optional)
        """
        cmdbuffer = bytearray()
        cmdbuffer.append(CMD)
        
        if low_edge is not None:
            encode_double(cmdbuffer, StatusType.LOW_EDGE, low_edge)
        if high_edge is not None:
            encode_double(cmdbuffer, StatusType.HIGH_EDGE, high_edge)
        if kaiser_beta is not None:
            encode_float(cmdbuffer, StatusType.KAISER_BETA, kaiser_beta)
        
        encode_int(cmdbuffer, StatusType.OUTPUT_SSRC, ssrc)
        encode_int(cmdbuffer, StatusType.COMMAND_TAG, secrets.randbits(31))
        encode_eol(cmdbuffer)
        
        logger.info(f"Setting filter for SSRC {ssrc}: low={low_edge}, high={high_edge}, beta={kaiser_beta}")
        self.send_command(cmdbuffer)
    
    def set_shift_frequency(self, ssrc: int, shift_hz: float):
        """
        Set post-detection frequency shift (for CW offset, etc.)
        
        Args:
            ssrc: SSRC of the channel
            shift_hz: Frequency shift in Hz
        """
        cmdbuffer = bytearray()
        cmdbuffer.append(CMD)
        
        encode_double(cmdbuffer, StatusType.SHIFT_FREQUENCY, shift_hz)
        encode_int(cmdbuffer, StatusType.OUTPUT_SSRC, ssrc)
        encode_int(cmdbuffer, StatusType.COMMAND_TAG, secrets.randbits(31))
        encode_eol(cmdbuffer)
        
        logger.info(f"Setting frequency shift for SSRC {ssrc} to {shift_hz} Hz")
        self.send_command(cmdbuffer)
    
    def set_output_level(self, ssrc: int, level: float):
        """
        Set output level for a channel
        
        Args:
            ssrc: SSRC of the channel
            level: Output level (range depends on mode)
        """
        cmdbuffer = bytearray()
        cmdbuffer.append(CMD)
        
        encode_float(cmdbuffer, StatusType.OUTPUT_LEVEL, level)
        encode_int(cmdbuffer, StatusType.OUTPUT_SSRC, ssrc)
        encode_int(cmdbuffer, StatusType.COMMAND_TAG, secrets.randbits(31))
        encode_eol(cmdbuffer)
        
        logger.info(f"Setting output level for SSRC {ssrc} to {level}")
        self.send_command(cmdbuffer)
    
    def create_channel(self, frequency_hz: float,
                       preset: str = "iq", sample_rate: Optional[int] = None,
                       agc_enable: int = 0, gain: float = 0.0,
                       destination: Optional[str] = None,
                       encoding: int = 0,
                       ssrc: Optional[int] = None,
                       lifetime: Optional[int] = None,
                       low_edge: Optional[float] = None,
                       high_edge: Optional[float] = None,
                       kaiser_beta: Optional[float] = None) -> int:
        """
        Create a new channel with specified configuration
        
        Creates and configures a radiod channel with a single command packet,
        ensuring all parameters are set together atomically.
        
        SSRC can be auto-allocated by omitting the ssrc parameter. The allocation
        uses a deterministic hash of channel parameters, enabling stream sharing
        between applications using the same parameters.
        
        Args:
            frequency_hz: Tuning frequency in Hz
            preset: Mode/preset name (default: "iq"). Common values:
                   - "iq": Raw IQ output (no demodulation)
                   - "usb": Upper sideband
                   - "lsb": Lower sideband
                   - "am": Amplitude modulation
                   - "fm": Frequency modulation
                   - "cw": Morse code
            sample_rate: Output sample rate in Hz (optional, uses radiod default if not set)
            agc_enable: Enable automatic gain control (0=off, 1=on, default: 0)
            gain: Manual gain in dB (default: 0.0). Only used when agc_enable=0
            destination: RTP destination multicast address (optional). Format: "address" or "address:port"
                        Examples: "239.1.2.3", "wspr.local", "239.1.2.3:5004"
                        If not specified, uses radiod's default from config file.
            encoding: Output encoding (0=none, 4=F32, etc.) - see Encoding class
            ssrc: SSRC (channel identifier). If None, auto-allocated from parameters.
                  Auto-allocation uses allocate_ssrc() for deterministic, shareable SSRCs.
            lifetime: Optional channel auto-destruct timer (radiod commit 0f8b622+).
                  None (default) = don't send; the channel inherits radiod's
                  Template default (infinite). Integer value = sent verbatim as
                  the LIFETIME tag, in radiod main-loop frames; 0 = infinite,
                  >0 = decremented at the radiod frame rate (~50 Hz at default
                  20 ms blocktime, so ~1000 frames ≈ 20 s) and the channel
                  self-destructs at zero. Each subsequent poll auto-extends
                  the lifetime to at least Channel_idle_timeout (~20 s) — so
                  callers using a finite lifetime as a crash-safe cleanup
                  must keep polling (e.g. via tune() or set_channel_lifetime).
            low_edge: Channel filter low edge in Hz, relative to channel
                  center (typically negative for symmetric IQ filters).
                  None = use the preset's default. Useful for clients that
                  need a narrower or wider passband than the preset provides
                  (e.g. BPSK PPS calibration with a near-CW signal needs a
                  much narrower filter than the iq preset's default).
            high_edge: Channel filter high edge in Hz, relative to channel
                  center. None = use the preset's default. See low_edge.
            kaiser_beta: Filter window Kaiser beta. None = use radiod default.
                  Higher beta = sharper transition + more sidelobe rejection.
        
        Returns:
            The SSRC of the created channel (useful when auto-allocated)
        
        Raises:
            CommandError: If command fails to send
            ValidationError: If parameters are invalid
            RuntimeError: If not connected to radiod
        
        Example:
            >>> control = RadiodControl("radiod.local")
            >>> # SSRC-free API (recommended) - SSRC auto-allocated
            >>> ssrc = control.create_channel(
            ...     frequency_hz=14.074e6,
            ...     preset="usb",
            ...     sample_rate=12000
            ... )
            >>> print(f"Channel created with SSRC: {ssrc}")
            
            >>> # Explicit SSRC (backward compatible)
            >>> control.create_channel(
            ...     frequency_hz=10.0e6,
            ...     preset="iq",
            ...     ssrc=10000000
            ... )
        """
        # Auto-allocate SSRC if not provided
        if ssrc is None:
            ssrc = allocate_ssrc(
                frequency_hz=frequency_hz,
                preset=preset,
                sample_rate=sample_rate or 16000,  # Default for allocation
                agc=bool(agc_enable),
                gain=gain,
                destination=destination,
                encoding=encoding,
                radiod_host=self.status_address
            )
            logger.info(f"Auto-allocated SSRC: {ssrc}")
        
        # Validate inputs
        _validate_ssrc(ssrc)
        _validate_frequency(frequency_hz)
        if sample_rate is not None:
            _validate_sample_rate(sample_rate)
        _validate_gain(gain)
        
        filter_desc = ""
        if low_edge is not None or high_edge is not None or kaiser_beta is not None:
            filter_desc = (f", filter=[{low_edge},{high_edge}]"
                           f"{f' beta={kaiser_beta}' if kaiser_beta is not None else ''}")
        logger.info(f"Creating channel: SSRC={ssrc}, freq={frequency_hz/1e6:.3f} MHz, "
                   f"demod={preset}, rate={sample_rate}Hz, agc={agc_enable}, gain={gain}dB, "
                   f"enc={encoding}{filter_desc}")
        
        # Build a single command packet with ALL parameters
        # This ensures radiod creates the channel with the correct settings
        cmdbuffer = bytearray()
        cmdbuffer.append(CMD)
        
        # PRESET: Mode name (e.g., "iq", "usb", "lsb")
        # This MUST come first - radiod uses it to set up the channel
        _validate_preset(preset)
        encode_string(cmdbuffer, StatusType.PRESET, preset)
        logger.info(f"Setting preset for SSRC {ssrc} to {preset}")
        
        # DEMOD_TYPE: 0=linear (IQ/USB/LSB/etc), 1=FM
        demod_type = 0 if preset.lower() in ['iq', 'usb', 'lsb', 'cw', 'am'] else 1
        encode_int(cmdbuffer, StatusType.DEMOD_TYPE, demod_type)
        logger.info(f"Setting DEMOD_TYPE for SSRC {ssrc} to {demod_type}")
        
        # Frequency
        encode_double(cmdbuffer, StatusType.RADIO_FREQUENCY, frequency_hz)
        logger.info(f"Setting frequency for SSRC {ssrc} to {frequency_hz/1e6:.3f} MHz")
        
        # Sample rate
        if sample_rate:
            encode_int(cmdbuffer, StatusType.OUTPUT_SAMPRATE, sample_rate)
            logger.info(f"Setting sample rate for SSRC {ssrc} to {sample_rate} Hz")

        # Filter edges + Kaiser beta — override the preset defaults inline
        # with the create command so the channel goes live with the requested
        # passband from frame zero (no transient at preset BW). Same TLV path
        # as set_filter().
        if low_edge is not None:
            encode_double(cmdbuffer, StatusType.LOW_EDGE, low_edge)
            logger.info(f"Setting LOW_EDGE for SSRC {ssrc} to {low_edge} Hz")
        if high_edge is not None:
            encode_double(cmdbuffer, StatusType.HIGH_EDGE, high_edge)
            logger.info(f"Setting HIGH_EDGE for SSRC {ssrc} to {high_edge} Hz")
        if kaiser_beta is not None:
            encode_float(cmdbuffer, StatusType.KAISER_BETA, kaiser_beta)
            logger.info(f"Setting KAISER_BETA for SSRC {ssrc} to {kaiser_beta}")

        # AGC setting
        encode_int(cmdbuffer, StatusType.AGC_ENABLE, agc_enable)
        logger.info(f"Setting AGC_ENABLE for SSRC {ssrc} to {agc_enable}")
        
        # Gain setting
        encode_double(cmdbuffer, StatusType.GAIN, gain)
        logger.info(f"Setting GAIN for SSRC {ssrc} to {gain} dB")
        
        # Encoding setting - NOT sending in main buffer anymore
        # Radiod requires OUTPUT_ENCODING to be sent in a separate command after creation
        # if encoding > 0:
        #    encode_int(cmdbuffer, StatusType.OUTPUT_ENCODING, encoding)
        #    logger.info(f"Setting OUTPUT_ENCODING for SSRC {ssrc} to {encoding}")
        
        # Destination address (if specified)
        if destination is not None:
            _validate_multicast_address(destination)
            dest_addr = destination
            dest_port = 5004  # Default RTP port
            
            # Check if port is specified in format "address:port"
            if ':' in destination:
                parts = destination.rsplit(':', 1)
                dest_addr = parts[0]
                try:
                    dest_port = int(parts[1])
                except ValueError:
                    raise ValidationError(f"Invalid port in destination '{destination}'")
            
            encode_socket(cmdbuffer, StatusType.OUTPUT_DATA_DEST_SOCKET, dest_addr, dest_port)
            logger.info(f"Setting destination for SSRC {ssrc} to {dest_addr}:{dest_port}")
        
        # SSRC and command tag
        encode_int(cmdbuffer, StatusType.OUTPUT_SSRC, ssrc)
        encode_int(cmdbuffer, StatusType.COMMAND_TAG, secrets.randbits(31))
        if lifetime is not None:
            encode_int(cmdbuffer, StatusType.LIFETIME, int(lifetime))
            logger.info(f"Setting LIFETIME for SSRC {ssrc} to {lifetime} frames")
        encode_eol(cmdbuffer)

        # Send the main creation packet
        self.send_command(cmdbuffer)
        
        # Send separate encoding command if requested (radiod requirement)
        if encoding > 0:
            encbuffer = bytearray()
            encbuffer.append(CMD)
            
            # Target the SSRC we just created
            encode_int(encbuffer, StatusType.OUTPUT_SSRC, ssrc)
            encode_int(encbuffer, StatusType.COMMAND_TAG, secrets.randbits(31))
            
            # Set the encoding
            encode_int(encbuffer, StatusType.OUTPUT_ENCODING, encoding)
            encode_eol(encbuffer)
            
            self.send_command(encbuffer)
            logger.info(f"Sent separate OUTPUT_ENCODING command for SSRC {ssrc}: {encoding}")
        
        logger.info(f"Channel {ssrc} created and configured")
        return ssrc
    
    def verify_channel(self, ssrc: int, expected_freq: Optional[float] = None) -> bool:
        """
        Verify that a channel exists and is configured correctly
        
        Args:
            ssrc: SSRC to verify
            expected_freq: Expected frequency in Hz (optional)
        
        Returns:
            True if channel exists and matches expectations
        """
        # Discover current channels
        channels = discover_channels(self.status_address)
        
        if ssrc not in channels:
            logger.warning(f"Channel {ssrc} not found")
            return False
        
        channel = channels[ssrc]
        
        if expected_freq and abs(channel.frequency - expected_freq) > 1:  # 1 Hz tolerance
            logger.warning(
                f"Channel {ssrc} frequency mismatch: "
                f"expected {expected_freq/1e6:.3f} MHz, "
                f"got {channel.frequency/1e6:.3f} MHz"
            )
            return False
        
        logger.info(f"Channel {ssrc} verified: {channel.frequency/1e6:.3f} MHz, {channel.preset}")
        return True

    def poll_channel(self, ssrc: int, expected_freq: Optional[float] = None,
                     timeout: float = 2.0, frequency_tolerance: float = 1.0):
        """Targeted status poll for a single channel — O(1), not O(channels).

        Sends a poll naming exactly this ``ssrc`` and returns the ChannelInfo
        from the first *matching* STATUS reply, or ``None`` on timeout.  Unlike
        :func:`discover_channels` (a fixed-window listen that parses *every*
        channel), this asks radiod for one channel and returns the moment its
        status lands, so cost is independent of how many channels radiod runs.

        radiod answers a poll for an unknown SSRC with an *empty* status
        (frequency 0) rather than silence, so "a reply arrived" does not prove
        the channel exists.  Pass ``expected_freq`` and a reply is accepted
        only when its frequency matches within ``frequency_tolerance``; empty
        or stale replies are skipped and the poll is re-sent until the real
        status appears or the timeout elapses.  With no ``expected_freq`` an
        obviously-empty reply (frequency 0) is rejected.

        This is the establishment/verify probe: after ``create_channel`` sends
        the create, one ``poll_channel(ssrc, expected_freq=f)`` confirms radiod
        accepted it and yields the *granted* encoding + RTP_TIMESNAP/GPS_TIME
        timing anchor the data path needs.  Liveness thereafter is judged from
        the RTP data plane, not from repeated polling.
        """
        _validate_ssrc(ssrc)
        from .discovery import ChannelInfo, _create_status_listener_socket

        multicast_addr = resolve_multicast_address(self.status_address, timeout=2.0)

        cmd = bytearray()
        cmd.append(CMD)
        encode_int(cmd, StatusType.COMMAND_TAG, secrets.randbits(31))
        encode_int(cmd, StatusType.OUTPUT_SSRC, ssrc)
        encode_eol(cmd)

        sock = _create_status_listener_socket(multicast_addr, None)
        # A status group with many channels (and many concurrent pollers
        # during a provisioning burst) can overrun the default ~208 KB socket
        # buffer and drop the one reply we are waiting for, forcing a spurious
        # timeout.  Give the listener generous headroom (honored up to
        # net.core.rmem_max; silently capped otherwise).
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 * 1024 * 1024)
        except OSError:
            pass
        try:
            deadline = time.time() + timeout
            next_poll = 0.0
            while True:
                now = time.time()
                if now >= deadline:
                    return None
                # (Re)send the poll periodically so a just-created channel is
                # caught promptly even if radiod's first broadcast was missed.
                if now >= next_poll:
                    try:
                        sock.sendto(bytes(cmd), (multicast_addr, 5006))
                    except OSError as exc:
                        logger.debug("poll_channel: send failed for SSRC %s: %s", ssrc, exc)
                        return None
                    next_poll = now + 0.5
                ready = select.select([sock], [], [], min(0.25, max(0.0, deadline - now)))
                if not ready[0]:
                    continue
                try:
                    buffer, _ = sock.recvfrom(8192)
                except OSError:
                    continue
                if not buffer or buffer[0] != 0:   # STATUS packets carry type byte 0
                    continue
                try:
                    status = self._decode_status_response(buffer)
                except Exception:
                    continue
                if status.get("ssrc") != ssrc:
                    continue
                freq = status.get("frequency", 0.0) or 0.0
                if expected_freq is not None:
                    if abs(freq - expected_freq) > frequency_tolerance:
                        continue   # empty or stale reply — keep waiting
                elif not freq:
                    continue       # radiod's "no such channel" empty status
                dest = status.get("destination", {})
                mcast = dest.get("address", "") if isinstance(dest, dict) else ""
                port = dest.get("port", 0) if isinstance(dest, dict) else 0
                return ChannelInfo(
                    ssrc=ssrc,
                    preset=status.get("preset", "unknown"),
                    sample_rate=status.get("sample_rate", 0),
                    frequency=freq,
                    snr=status.get("snr", float("-inf")),
                    multicast_address=mcast,
                    port=port,
                    gps_time=status.get("gps_time"),
                    rtp_timesnap=status.get("rtp_timesnap"),
                    encoding=status.get("encoding", 0),
                )
        finally:
            try:
                sock.close()
            except Exception:
                pass

    def ensure_channel(
        self,
        frequency_hz: float,
        preset: str = "iq",
        sample_rate: int = 16000,
        agc_enable: int = 0,
        gain: float = 0.0,
        destination: Optional[str] = None,
        encoding: int = 0,
        timeout: float = 5.0,
        frequency_tolerance: float = 1.0,
        lifetime: Optional[int] = None,
        low_edge: Optional[float] = None,
        high_edge: Optional[float] = None,
        kaiser_beta: Optional[float] = None,
    ):
        """
        Ensure a channel exists with the requested characteristics and return it.
        
        This is the recommended high-level API for client applications. It provides
        a simple contract: "deliver me a channel with these characteristics" and
        returns a verified ChannelInfo ready for use with RadiodStream.
        
        The method:
        1. Computes a deterministic SSRC from the channel parameters (including destination)
        2. Checks if a matching channel already exists (enables stream sharing)
        3. Creates the channel if it doesn't exist
        4. Verifies the channel meets the requested specifications
        5. Returns a ChannelInfo object ready for RTP stream consumption
        
        Args:
            frequency_hz: Center frequency in Hz
            preset: Demodulation mode ("iq", "usb", "lsb", "am", "fm", "cw")
            sample_rate: Output sample rate in Hz (default: 16000)
            agc_enable: Enable automatic gain control (0=off, 1=on, default: 0)
            gain: Manual gain in dB (default: 0.0). Only used when agc_enable=0
            destination: RTP destination multicast address (optional).
                        Precedence: (1) explicit ``destination=`` wins;
                        (2) otherwise if the RadiodControl was constructed
                        with ``client_id=``, this method derives a
                        deterministic ``239.x.y.z`` address from the
                        (client_id, status_address) pair so each peer
                        client on a station gets its own multicast group;
                        (3) otherwise (``destination=None`` and no
                        ``client_id``), radiod's config-file default is
                        used.  The resolved address becomes part of the
                        channel identity (SSRC).
            encoding: Output encoding (0=none, 4=F32, etc.) - see Encoding class
            timeout: Maximum time to wait for channel verification (default: 5.0s)
            frequency_tolerance: Acceptable frequency deviation in Hz (default: 1.0)
            lifetime: Optional channel auto-destruct timer in radiod frames
                     (radiod commit 0f8b622+). When set, the LIFETIME tag is
                     included on the creation command and on the reuse-poll
                     so the channel's lifetime is refreshed regardless of its
                     prior state. None (default) = don't touch lifetime.
                     See create_channel() for the full unit semantics.
            low_edge: Optional channel filter low edge in Hz (relative to
                     channel center). When specified, the requested filter is
                     applied both to newly-created channels (inline with the
                     create command) and to reused channels (via set_filter
                     after the reuse decision), so ensure_channel always
                     returns a channel with the requested passband.
                     None (default) = use the preset's filter edges.
                     Note: filter edges are not part of the channel identity
                     (they don't participate in SSRC allocation), so a second
                     caller requesting the same channel with different edges
                     will reconfigure the filter — last-writer-wins, same
                     model as gain/AGC.
            high_edge: See low_edge.
            kaiser_beta: Optional filter window Kaiser beta. None = radiod default.

        Returns:
            ChannelInfo object with verified channel details, ready for RadiodStream
        
        Raises:
            TimeoutError: If channel cannot be verified within timeout
            ValidationError: If parameters are invalid
            CommandError: If radiod communication fails
        
        Example:
            >>> from ka9q import RadiodControl, RadiodStream
            >>> 
            >>> with RadiodControl("radiod.local") as control:
            ...     # Request a channel - ka9q-python handles all the details
            ...     channel = control.ensure_channel(
            ...         frequency_hz=14.074e6,
            ...         preset="usb",
            ...         sample_rate=12000
            ...     )
            ...     print(f"Channel ready: {channel.frequency/1e6:.3f} MHz")
            ...     
            ...     # Channel is verified and ready for streaming
            ...     stream = RadiodStream(channel, on_samples=my_callback)
            ...     stream.start()
        
        Note:
            The deterministic SSRC allocation enables stream sharing: multiple
            applications requesting the same parameters will share the same
            channel, reducing radiod resource usage.
        """
        from .addressing import generate_multicast_ip
        from .discovery import ChannelInfo, discover_channels

        # Validate inputs
        _validate_frequency(frequency_hz)
        _validate_sample_rate(sample_rate)
        _validate_gain(gain)
        _validate_preset(preset)
        _validate_timeout(timeout)

        # CONTRACT v0.3 §7: when the RadiodControl was constructed with a
        # client_id and the caller didn't pass an explicit destination,
        # derive a deterministic per-(client, radiod) multicast address.
        # This makes peer clients on the same host land on distinct
        # multicast groups without per-client derivation code.  An
        # explicit destination= still wins (e.g. operator override).
        if destination is None and self.client_id:
            destination = generate_multicast_ip(
                unique_id=self.client_id,
                radiod_host=self.status_address,
            )
            logger.debug(
                "ensure_channel: derived destination=%s for client_id=%r "
                "radiod=%r", destination, self.client_id, self.status_address,
            )

        # Compute deterministic SSRC from parameters (including radiod identity)
        ssrc = allocate_ssrc(
            frequency_hz=frequency_hz,
            preset=preset,
            sample_rate=sample_rate,
            agc=bool(agc_enable),
            gain=gain,
            destination=destination,
            encoding=encoding,
            radiod_host=self.status_address
        )
        logger.info(f"ensure_channel: computed SSRC {ssrc} for {frequency_hz/1e6:.3f} MHz {preset} dest={destination} enc={encoding} radiod={self.status_address}")
        
        # Check if channel already exists with matching parameters.  Targeted
        # O(1) poll instead of a full discover_channels() sweep: poll_channel
        # returns the channel only when it is present at our frequency.
        existing = self.poll_channel(
            ssrc, expected_freq=frequency_hz,
            timeout=1.0, frequency_tolerance=frequency_tolerance,
        )
        if existing is not None:
            # Verify existing channel meets specs
            if abs(existing.frequency - frequency_hz) <= frequency_tolerance:
                if existing.sample_rate == sample_rate:
                    # Check destination if requested
                    dest_ok = True
                    if destination:
                        # Existing destination might be "ip" or "ip:port" or "hostname"
                        # Simple check: if requested is in existing string
                        # Ideally we'd parse both, but string containment is a safe basic check
                        if destination not in (existing.multicast_address or ""):
                            dest_ok = False
                    
                    # Check encoding if requested
                    if dest_ok and encoding != 0:
                        if existing.encoding != encoding:
                            dest_ok = False
                            logger.info(
                                f"ensure_channel: existing channel encoding mismatch "
                                f"({existing.encoding} vs {encoding}), will reconfigure"
                            )

                    if dest_ok:
                        logger.info(
                            f"ensure_channel: reusing existing channel SSRC {ssrc} "
                            f"at {existing.frequency/1e6:.3f} MHz"
                        )
                        # Refresh lifetime on reuse if requested — the channel
                        # may have been created without a finite lifetime (or
                        # may be drifting toward expiry); a poll with LIFETIME
                        # set forces it to the requested value.
                        if lifetime is not None:
                            self.set_channel_lifetime(ssrc, int(lifetime))
                        # Apply filter edges on reuse so the requested
                        # passband is authoritative regardless of the prior
                        # channel state. ChannelInfo doesn't expose current
                        # filter edges (radiod's discovery payload doesn't
                        # carry them reliably), so we re-send rather than
                        # compare.
                        if (low_edge is not None
                                or high_edge is not None
                                or kaiser_beta is not None):
                            self.set_filter(
                                ssrc,
                                low_edge=low_edge,
                                high_edge=high_edge,
                                kaiser_beta=kaiser_beta,
                            )
                        return existing
                    else:
                        logger.info(
                            f"ensure_channel: existing channel configuration mismatch, will reconfigure"
                        )
                else:
                    logger.info(
                        f"ensure_channel: existing channel has different sample rate "
                        f"({existing.sample_rate} vs {sample_rate}), will reconfigure"
                    )
            else:
                logger.info(
                    f"ensure_channel: existing channel frequency mismatch "
                    f"({existing.frequency/1e6:.3f} vs {frequency_hz/1e6:.3f} MHz), will reconfigure"
                )
        
        # Create or reconfigure the channel
        logger.info(f"ensure_channel: creating/configuring channel SSRC {ssrc}")
        self.create_channel(
            frequency_hz=frequency_hz,
            preset=preset,
            sample_rate=sample_rate,
            agc_enable=agc_enable,
            gain=gain,
            destination=destination,
            encoding=encoding,
            ssrc=ssrc,
            lifetime=lifetime,
            low_edge=low_edge,
            high_edge=high_edge,
            kaiser_beta=kaiser_beta,
        )
        
        # Confirm the channel landed via one targeted O(1) poll.  This was a
        # looped discover_channels() sweep (O(total channels), re-listing every
        # channel each iteration) which stalled badly on a busy shared radiod —
        # a per-channel verify couldn't catch its one status in the 1 s window
        # and timed out even though radiod had created the channel.  Liveness
        # after establishment is judged from the RTP data plane by the stream
        # classes, not by more polling.
        channel = self.poll_channel(
            ssrc, expected_freq=frequency_hz,
            timeout=timeout, frequency_tolerance=frequency_tolerance,
        )
        if channel is None:
            raise TimeoutError(
                f"Channel SSRC {ssrc} not verified within {timeout}s. "
                f"Requested: {frequency_hz/1e6:.3f} MHz, {preset}, {sample_rate} Hz"
            )
        # radiod may grant a different encoding (e.g. F32->S16 for some IQ
        # configs); the returned ChannelInfo carries the granted value, which
        # consumers use authoritatively.  A rate/preset divergence is logged
        # but not fatal — the channel exists at the requested frequency.
        if (channel.sample_rate != sample_rate
                or channel.preset.lower() != preset.lower()):
            logger.warning(
                "ensure_channel: SSRC %s granted rate=%s preset=%s "
                "(requested rate=%s preset=%s)",
                ssrc, channel.sample_rate, channel.preset, sample_rate, preset,
            )
        logger.info(
            f"ensure_channel: verified channel SSRC {ssrc} - "
            f"{channel.frequency/1e6:.3f} MHz, {channel.preset}, "
            f"{channel.sample_rate} Hz"
        )
        return channel
    
    def remove_channel(self, ssrc: int):
        """
        Remove a channel from radiod
        
        In radiod, setting a channel's frequency to 0 marks it for removal.
        Radiod periodically polls for channels with frequency=0 and removes them,
        so the removal is not instantaneous but happens within the next polling cycle.
        
        This is the proper way to clean up unused channels and prevent radiod
        from accumulating orphaned channel instances.
        
        Args:
            ssrc: SSRC of the channel to remove
            
        Raises:
            ValidationError: If SSRC is invalid
            
        Example:
            >>> control = RadiodControl("radiod.local")
            >>> control.create_channel(ssrc=14074000, frequency_hz=14.074e6)
            >>> # ... use channel ...
            >>> control.remove_channel(ssrc=14074000)  # Mark for removal
            >>> # Channel will be removed by radiod in next polling cycle
        
        Note:
            - Removal is NOT instantaneous - radiod polls periodically for channels to remove
            - Always call this when your application is done with a channel
            - Especially important for long-running applications that create temporary channels
            - The channel may still appear in discovery for a brief time after calling this
        """
        _validate_ssrc(ssrc)
        
        cmdbuffer = bytearray()
        cmdbuffer.append(CMD)
        
        # Setting frequency to 0 removes the channel in radiod
        encode_double(cmdbuffer, StatusType.RADIO_FREQUENCY, 0.0)
        encode_int(cmdbuffer, StatusType.OUTPUT_SSRC, ssrc)
        encode_int(cmdbuffer, StatusType.COMMAND_TAG, secrets.randbits(31))
        encode_eol(cmdbuffer)
        
        logger.info(f"Removing channel SSRC {ssrc}")
        self.send_command(cmdbuffer)

    def set_channel_lifetime(self, ssrc: int, lifetime: int):
        """
        Set / refresh a channel's auto-destruct timer.

        ka9q-radio commit 0f8b622+ added a per-channel ``lifetime`` field
        that decrements every radiod main-loop frame (~50 Hz at the
        default 20 ms blocktime) and destroys the channel when it
        reaches zero.  Polling auto-extends a non-zero lifetime to at
        least ~20 s, so a client using this for crash-safe cleanup
        should call this method (or any other poll) periodically as a
        keep-alive.

        Args:
            ssrc: SSRC of the channel.
            lifetime: New lifetime value, sent verbatim as the LIFETIME
                tag (units: radiod frames).  ``0`` = infinite (channel
                lives until explicitly destroyed).  ``>0`` = self-
                destruct after that many frames; radiod will bump it up
                to the configured idle-timeout floor (typically 1000
                frames ≈ 20 s) when this poll is processed.

        Note:
            Older radiod builds without LIFETIME silently ignore the tag,
            so calling this against a pre-0f8b622 radiod is a no-op
            rather than an error.
        """
        _validate_ssrc(ssrc)
        if not isinstance(lifetime, int):
            raise ValidationError(
                f"lifetime must be int (frames); got {type(lifetime).__name__}"
            )
        if lifetime < 0:
            raise ValidationError(f"lifetime must be >= 0; got {lifetime}")

        cmdbuffer = bytearray()
        cmdbuffer.append(CMD)
        encode_int(cmdbuffer, StatusType.OUTPUT_SSRC, ssrc)
        encode_int(cmdbuffer, StatusType.COMMAND_TAG, secrets.randbits(31))
        encode_int(cmdbuffer, StatusType.LIFETIME, lifetime)
        encode_eol(cmdbuffer)

        logger.info(f"Setting LIFETIME for SSRC {ssrc} to {lifetime} frames")
        self.send_command(cmdbuffer)

    def _setup_status_listener(self):
        """Set up socket to listen for status responses"""
        # Create a separate socket for receiving status messages
        status_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        status_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        
        # Set SO_REUSEPORT to allow multiple processes to bind (if available)
        if hasattr(socket, 'SO_REUSEPORT'):
            try:
                status_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                logger.debug("SO_REUSEPORT enabled")
            except OSError as e:
                logger.warning(f"Could not set SO_REUSEPORT: {e}")
        
        # CRITICAL: Must bind to the multicast port (5006) to receive multicast packets
        # Multicast packets are addressed to specific port, not just IP
        # Use 0.0.0.0 and SO_REUSEADDR to allow multiple processes
        try:
            status_sock.bind(('0.0.0.0', 5006))  # Bind to radiod status port on all interfaces
            bound_port = status_sock.getsockname()[1]
            logger.debug(f"Bound to port {bound_port} for multicast reception")
        except OSError as e:
            logger.error(f"Failed to bind socket: {e}")
            raise
        
        # Join the multicast group on specified interface
        # Use the status multicast address (where status packets are sent)
        interface_addr = self.interface if self.interface else '0.0.0.0'
        mreq = struct.pack('=4s4s', 
                          socket.inet_aton(self.status_mcast_addr),  # status multicast group
                          socket.inet_aton(interface_addr))  # interface to use
        status_sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        logger.debug(f"Joined status multicast group {self.status_mcast_addr} on interface {interface_addr}")
        
        # Set timeout for polling
        status_sock.settimeout(0.1)  # 100ms timeout
        
        return status_sock
    
    def _get_or_create_status_listener(self):
        """
        Get cached status listener socket or create new one if needed.
        
        This method implements socket reuse to avoid creating/destroying sockets
        on every tune() call, which saves 20-30ms per operation and prevents
        socket exhaustion.
        
        Returns:
            Cached or newly created status listener socket
        """
        import threading
        
        # Lazy initialization of lock (avoid threading overhead if never used)
        if self._status_sock_lock is None:
            self._status_sock_lock = threading.Lock()
        
        with self._status_sock_lock:
            if self._status_sock is None:
                logger.debug("Creating cached status listener socket")
                self._status_sock = self._setup_status_listener()
            else:
                logger.debug("Reusing cached status listener socket")
            return self._status_sock
    
    def tune(self, ssrc: int, frequency_hz: Optional[float] = None,
             preset: Optional[str] = None, sample_rate: Optional[int] = None,
             low_edge: Optional[float] = None, high_edge: Optional[float] = None,
             gain: Optional[float] = None, agc_enable: Optional[bool] = None,
             rf_gain: Optional[float] = None, rf_atten: Optional[float] = None,
             encoding: Optional[int] = None, destination: Optional[str] = None,
             lifetime: Optional[int] = None,
             timeout: float = 5.0) -> dict:
        """
        Tune a channel and retrieve its status (like tune.c in ka9q-radio)
        
        This method sends tuning commands to radiod and waits for a status response,
        replicating the functionality of the tune utility in ka9q-radio.
        
        Args:
            ssrc: SSRC of the channel to tune
            frequency_hz: Frequency in Hz (optional)
            preset: Preset/mode name (optional, e.g., "iq", "usb", "lsb")
            sample_rate: Sample rate in Hz (optional)
            low_edge: Low filter edge in Hz (optional)
            high_edge: High filter edge in Hz (optional)
            gain: Manual gain in dB (optional, disables AGC)
            agc_enable: Enable AGC (optional)
            rf_gain: RF front-end gain in dB (optional)
            rf_atten: RF front-end attenuation in dB (optional)
            encoding: Output encoding type (optional, use Encoding constants)
            destination: Destination multicast address (optional)
            lifetime: Set/refresh the channel auto-destruct timer, in radiod
                     frames. See ``set_channel_lifetime``. None = don't touch.
            timeout: Maximum time to wait for response in seconds (default: 5.0)
            
        Returns:
            Dictionary containing channel status with keys:
            - ssrc: Channel SSRC
            - frequency: Radio frequency in Hz
            - preset: Mode/preset name
            - sample_rate: Sample rate in Hz
            - agc_enable: AGC enabled status
            - gain: Current gain in dB
            - rf_gain: RF gain in dB
            - rf_atten: RF attenuation in dB
            - rf_agc: RF AGC status
            - low_edge: Low filter edge in Hz
            - high_edge: High filter edge in Hz
            - noise_density: Noise density in dB/Hz
            - baseband_power: Baseband power in dB
            - encoding: Output encoding type
            - destination: Destination socket address
            - snr: Signal-to-noise ratio in dB (calculated)
        
        Raises:
            TimeoutError: If no matching response received within timeout
        """
        # Validate inputs
        _validate_ssrc(ssrc)
        if frequency_hz is not None:
            _validate_frequency(frequency_hz)
        if sample_rate is not None:
            _validate_sample_rate(sample_rate)
        if gain is not None:
            _validate_gain(gain)
        _validate_timeout(timeout)
        
        import time
        import select
        
        # Build command packet with all specified parameters
        cmdbuffer = bytearray()
        cmdbuffer.append(CMD)
        
        # Generate command tag for matching response
        command_tag = secrets.randbits(31)
        encode_int(cmdbuffer, StatusType.COMMAND_TAG, command_tag)
        encode_int(cmdbuffer, StatusType.OUTPUT_SSRC, ssrc)
        
        if preset is not None:
            _validate_preset(preset)
            encode_string(cmdbuffer, StatusType.PRESET, preset)
        
        if sample_rate is not None:
            encode_int(cmdbuffer, StatusType.OUTPUT_SAMPRATE, sample_rate)
        
        if low_edge is not None:
            encode_float(cmdbuffer, StatusType.LOW_EDGE, low_edge)
        
        if high_edge is not None:
            encode_float(cmdbuffer, StatusType.HIGH_EDGE, high_edge)
        
        if frequency_hz is not None:
            encode_double(cmdbuffer, StatusType.RADIO_FREQUENCY, frequency_hz)
        
        if gain is not None:
            encode_float(cmdbuffer, StatusType.GAIN, gain)
            encode_int(cmdbuffer, StatusType.AGC_ENABLE, 0)  # Turn off AGC for manual gain
        elif agc_enable is not None:
            encode_int(cmdbuffer, StatusType.AGC_ENABLE, 1 if agc_enable else 0)
        
        if encoding is not None:
            encode_int(cmdbuffer, StatusType.OUTPUT_ENCODING, encoding)
        
        if rf_gain is not None:
            encode_float(cmdbuffer, StatusType.RF_GAIN, rf_gain)
        
        if rf_atten is not None:
            encode_float(cmdbuffer, StatusType.RF_ATTEN, rf_atten)
        
        if destination is not None:
            _validate_multicast_address(destination)
            # Parse destination - could be IP address or hostname
            # If it's a hostname, try to resolve it; if not, use it as-is (radiod will handle it)
            dest_addr = destination
            dest_port = 5004  # Default RTP port
            
            # Check if port is specified in format "address:port"
            if ':' in destination:
                parts = destination.rsplit(':', 1)
                dest_addr = parts[0]
                try:
                    dest_port = int(parts[1])
                except ValueError:
                    raise ValidationError(f"Invalid port in destination '{destination}'")
            
            encode_socket(cmdbuffer, StatusType.OUTPUT_DATA_DEST_SOCKET, dest_addr, dest_port)
            logger.info(f"Setting destination for SSRC {ssrc} to {dest_addr}:{dest_port}")

        if lifetime is not None:
            if not isinstance(lifetime, int) or lifetime < 0:
                raise ValidationError(
                    f"lifetime must be a non-negative int (frames); got {lifetime!r}"
                )
            encode_int(cmdbuffer, StatusType.LIFETIME, lifetime)
            logger.info(f"Setting LIFETIME for SSRC {ssrc} to {lifetime} frames")

        encode_eol(cmdbuffer)
        
        # Get cached status listener (or create if first use)
        # Socket is reused across tune() calls to avoid creation/destruction overhead
        status_sock = self._get_or_create_status_listener()
        
        try:
            start_time = time.time()
            last_send_time = 0
            retry_interval = 0.1  # Start at 100ms
            max_retry_interval = 1.0  # Cap at 1 second
            attempts = 0
            
            while time.time() - start_time < timeout:
                # Send command with exponential backoff
                current_time = time.time()
                if current_time - last_send_time >= retry_interval:
                    self.send_command(cmdbuffer)
                    last_send_time = current_time
                    attempts += 1
                    logger.debug(f"Sent tune command with tag {command_tag} (attempt {attempts})")
                    
                    # Exponential backoff: 100ms, 200ms, 400ms, 800ms, 1000ms (capped)
                    # This reduces network spam and CPU usage significantly
                    retry_interval = min(retry_interval * 2, max_retry_interval)
                
                # Check for incoming status messages with adaptive timeout
                try:
                    # Use remaining time or retry interval, whichever is smaller
                    remaining = timeout - (time.time() - start_time)
                    select_timeout = min(retry_interval, remaining, 0.5)
                    
                    ready = select.select([status_sock], [], [], select_timeout)
                    if not ready[0]:
                        logger.debug(f"select() timed out after {select_timeout:.3f}s, no packets received")
                        continue
                    
                    response_buffer, addr = status_sock.recvfrom(8192)
                    logger.debug(f"Received {len(response_buffer)} bytes from {addr}")
                    logger.debug(f"First 16 bytes: {' '.join(f'{b:02x}' for b in response_buffer[:16])}")
                    
                    # Parse response
                    if len(response_buffer) == 0 or response_buffer[0] != 0:
                        continue  # Not a status response
                    
                    # Decode the response
                    status = self._decode_status_response(response_buffer)
                    
                    # Check if this response is for our command
                    if status.get('ssrc') == ssrc and status.get('command_tag') == command_tag:
                        logger.info(f"Received matching status response for SSRC {ssrc}")
                        return status
                    else:
                        logger.debug(f"Response not for us: ssrc={status.get('ssrc')}, tag={status.get('command_tag')}")
                
                except socket.timeout:
                    continue
            
            raise TimeoutError(f"No status response received for SSRC {ssrc} within {timeout}s")
        
        finally:
            # NOTE: Do NOT close status_sock here - it's cached for reuse
            # Socket will be closed in close() method
            pass
    
    def _decode_status_response(self, buffer: bytes) -> dict:
        """
        Decode a status response packet from radiod (thin wrapper).

        Delegates the actual parse to the module-level
        :func:`decode_status_dict` and bumps the per-instance metric.
        Kept as a method for backward compatibility with callers that
        already use ``self._decode_status_response`` (e.g.
        ``listen_status``, ``tune``).

        Args:
            buffer: Raw response bytes

        Returns:
            Dictionary containing decoded status fields
        """
        status = decode_status_dict(buffer)
        if status:
            self.metrics.status_received += 1
        return status

    def get_metrics(self) -> dict:
        """
        Get current metrics as a dictionary
        
        Returns:
            Dictionary containing performance and error metrics
            
        Example:
            >>> control = RadiodControl("radiod.local")
            >>> control.create_channel(ssrc=12345, frequency_hz=14.074e6)
            >>> metrics = control.get_metrics()
            >>> print(f"Success rate: {metrics['success_rate']:.1%}")
            >>> print(f"Commands sent: {metrics['commands_sent']}")
        """
        return self.metrics.to_dict()
    
    def reset_metrics(self):
        """Reset all metrics to zero"""
        self.metrics = Metrics()
        logger.info("Metrics reset")
    
    def set_doppler(self, ssrc: int, doppler_hz: float = 0.0, doppler_rate_hz_per_sec: float = 0.0):
        """
        Set Doppler frequency shift and rate for satellite tracking
        
        Args:
            ssrc: SSRC of the channel
            doppler_hz: Doppler frequency shift in Hz (default: 0.0)
            doppler_rate_hz_per_sec: Doppler rate in Hz/sec (default: 0.0)
        
        Example:
            >>> # Track satellite with 5 kHz Doppler shift, changing at 100 Hz/sec
            >>> control.set_doppler(ssrc=12345, doppler_hz=5000, doppler_rate_hz_per_sec=100)
        """
        _validate_ssrc(ssrc)
        
        cmdbuffer = bytearray()
        cmdbuffer.append(CMD)
        
        encode_double(cmdbuffer, StatusType.DOPPLER_FREQUENCY, doppler_hz)
        encode_double(cmdbuffer, StatusType.DOPPLER_FREQUENCY_RATE, doppler_rate_hz_per_sec)
        encode_int(cmdbuffer, StatusType.OUTPUT_SSRC, ssrc)
        encode_int(cmdbuffer, StatusType.COMMAND_TAG, secrets.randbits(31))
        encode_eol(cmdbuffer)
        
        logger.info(f"Setting Doppler for SSRC {ssrc}: freq={doppler_hz} Hz, rate={doppler_rate_hz_per_sec} Hz/s")
        self.send_command(cmdbuffer)
    
    def set_pll(self, ssrc: int, enable: bool, bandwidth_hz: Optional[float] = None, square: bool = False):
        """
        Configure PLL (Phase-Locked Loop) for carrier tracking in linear modes
        
        Args:
            ssrc: SSRC of the channel
            enable: Enable PLL carrier tracking
            bandwidth_hz: PLL loop bandwidth in Hz (optional, default depends on mode)
            square: Enable squaring loop for suppressed carrier reception (default: False)
        
        Example:
            >>> # Enable PLL for coherent AM reception
            >>> control.set_pll(ssrc=12345, enable=True, bandwidth_hz=50)
            >>> # Enable squaring PLL for DSB-SC (suppressed carrier)
            >>> control.set_pll(ssrc=12345, enable=True, square=True, bandwidth_hz=20)
        """
        _validate_ssrc(ssrc)
        
        cmdbuffer = bytearray()
        cmdbuffer.append(CMD)
        
        encode_int(cmdbuffer, StatusType.PLL_ENABLE, 1 if enable else 0)
        if bandwidth_hz is not None:
            _validate_positive(bandwidth_hz, "PLL bandwidth")
            encode_float(cmdbuffer, StatusType.PLL_BW, bandwidth_hz)
        if square:
            encode_int(cmdbuffer, StatusType.PLL_SQUARE, 1)
        
        encode_int(cmdbuffer, StatusType.OUTPUT_SSRC, ssrc)
        encode_int(cmdbuffer, StatusType.COMMAND_TAG, secrets.randbits(31))
        encode_eol(cmdbuffer)
        
        logger.info(f"Setting PLL for SSRC {ssrc}: enable={enable}, bw={bandwidth_hz} Hz, square={square}")
        self.send_command(cmdbuffer)
    
    def set_squelch(self, ssrc: int, enable: bool = True, open_snr_db: Optional[float] = None, 
                    close_snr_db: Optional[float] = None):
        """
        Configure SNR-based squelch
        
        Args:
            ssrc: SSRC of the channel
            enable: Enable SNR squelch (default: True)
            open_snr_db: SNR threshold in dB to open squelch (optional)
            close_snr_db: SNR threshold in dB to close squelch (optional, should be < open_snr_db)
        
        Example:
            >>> # Open squelch at 10 dB SNR, close at 8 dB
            >>> control.set_squelch(ssrc=12345, enable=True, open_snr_db=10, close_snr_db=8)
        """
        _validate_ssrc(ssrc)
        
        cmdbuffer = bytearray()
        cmdbuffer.append(CMD)
        
        encode_int(cmdbuffer, StatusType.SNR_SQUELCH, 1 if enable else 0)
        if open_snr_db is not None:
            encode_float(cmdbuffer, StatusType.SQUELCH_OPEN, open_snr_db)
        if close_snr_db is not None:
            encode_float(cmdbuffer, StatusType.SQUELCH_CLOSE, close_snr_db)
        
        encode_int(cmdbuffer, StatusType.OUTPUT_SSRC, ssrc)
        encode_int(cmdbuffer, StatusType.COMMAND_TAG, secrets.randbits(31))
        encode_eol(cmdbuffer)
        
        logger.info(f"Setting squelch for SSRC {ssrc}: enable={enable}, open={open_snr_db} dB, close={close_snr_db} dB")
        self.send_command(cmdbuffer)
    
    def set_output_channels(self, ssrc: int, channels: int):
        """
        Set output channel count (mono/stereo)
        
        Args:
            ssrc: SSRC of the channel
            channels: 1 for mono, 2 for stereo
                     For WFM mode: 2 enables FM stereo decoding, 1 disables it
        
        Raises:
            ValidationError: If channels is not 1 or 2
        
        Example:
            >>> control.set_output_channels(ssrc=12345, channels=2)  # Stereo
        """
        _validate_ssrc(ssrc)
        if channels not in [1, 2]:
            raise ValidationError(f"Invalid channel count: {channels} (must be 1 or 2)")
        
        cmdbuffer = bytearray()
        cmdbuffer.append(CMD)
        
        encode_int(cmdbuffer, StatusType.OUTPUT_CHANNELS, channels)
        encode_int(cmdbuffer, StatusType.OUTPUT_SSRC, ssrc)
        encode_int(cmdbuffer, StatusType.COMMAND_TAG, secrets.randbits(31))
        encode_eol(cmdbuffer)
        
        logger.info(f"Setting output channels for SSRC {ssrc} to {channels}")
        self.send_command(cmdbuffer)
    
    def set_envelope_detection(self, ssrc: int, enable: bool):
        """
        Enable/disable envelope detection in linear modes (for AM)
        
        Args:
            ssrc: SSRC of the channel
            enable: True for envelope detection (AM), False for synchronous detection
        
        Example:
            >>> control.set_envelope_detection(ssrc=12345, enable=True)  # AM mode
        """
        _validate_ssrc(ssrc)
        
        cmdbuffer = bytearray()
        cmdbuffer.append(CMD)
        
        encode_int(cmdbuffer, StatusType.ENVELOPE, 1 if enable else 0)
        encode_int(cmdbuffer, StatusType.OUTPUT_SSRC, ssrc)
        encode_int(cmdbuffer, StatusType.COMMAND_TAG, secrets.randbits(31))
        encode_eol(cmdbuffer)
        
        logger.info(f"Setting envelope detection for SSRC {ssrc}: {enable}")
        self.send_command(cmdbuffer)
    
    def set_independent_sideband(self, ssrc: int, enable: bool):
        """
        Enable/disable Independent Sideband (ISB) mode
        
        In ISB mode, USB and LSB are demodulated separately and output to left/right channels.
        
        Args:
            ssrc: SSRC of the channel
            enable: True to enable ISB mode, False for normal operation
        
        Example:
            >>> control.set_independent_sideband(ssrc=12345, enable=True)
        """
        _validate_ssrc(ssrc)
        
        cmdbuffer = bytearray()
        cmdbuffer.append(CMD)
        
        encode_int(cmdbuffer, StatusType.INDEPENDENT_SIDEBAND, 1 if enable else 0)
        encode_int(cmdbuffer, StatusType.OUTPUT_SSRC, ssrc)
        encode_int(cmdbuffer, StatusType.COMMAND_TAG, secrets.randbits(31))
        encode_eol(cmdbuffer)
        
        logger.info(f"Setting ISB mode for SSRC {ssrc}: {enable}")
        self.send_command(cmdbuffer)
    
    def set_fm_threshold_extension(self, ssrc: int, enable: bool):
        """
        Enable/disable FM threshold extension (for weak signals)
        
        Args:
            ssrc: SSRC of the channel
            enable: True to enable threshold extension
        
        Example:
            >>> control.set_fm_threshold_extension(ssrc=12345, enable=True)
        """
        _validate_ssrc(ssrc)
        
        cmdbuffer = bytearray()
        cmdbuffer.append(CMD)
        
        encode_int(cmdbuffer, StatusType.THRESH_EXTEND, 1 if enable else 0)
        encode_int(cmdbuffer, StatusType.OUTPUT_SSRC, ssrc)
        encode_int(cmdbuffer, StatusType.COMMAND_TAG, secrets.randbits(31))
        encode_eol(cmdbuffer)
        
        logger.info(f"Setting FM threshold extension for SSRC {ssrc}: {enable}")
        self.send_command(cmdbuffer)
    
    def set_agc_threshold(self, ssrc: int, threshold_db: float):
        """
        Set AGC threshold (level above noise floor to activate AGC)
        
        Args:
            ssrc: SSRC of the channel
            threshold_db: Threshold in dB relative to noise floor
        
        Example:
            >>> control.set_agc_threshold(ssrc=12345, threshold_db=10)
        """
        _validate_ssrc(ssrc)
        
        cmdbuffer = bytearray()
        cmdbuffer.append(CMD)
        
        encode_float(cmdbuffer, StatusType.AGC_THRESHOLD, threshold_db)
        encode_int(cmdbuffer, StatusType.OUTPUT_SSRC, ssrc)
        encode_int(cmdbuffer, StatusType.COMMAND_TAG, secrets.randbits(31))
        encode_eol(cmdbuffer)
        
        logger.info(f"Setting AGC threshold for SSRC {ssrc}: {threshold_db} dB")
        self.send_command(cmdbuffer)
    
    def set_opus_bitrate(self, ssrc: int, bitrate: int):
        """
        Set Opus encoder bitrate
        
        Args:
            ssrc: SSRC of the channel
            bitrate: Bitrate in bits/sec (0 for auto, typical: 32000-128000)
        
        Example:
            >>> control.set_opus_bitrate(ssrc=12345, bitrate=64000)  # 64 kbps
            >>> control.set_opus_bitrate(ssrc=12345, bitrate=0)      # Auto
        """
        _validate_ssrc(ssrc)
        if bitrate < 0:
            raise ValidationError(f"Bitrate must be non-negative, got {bitrate}")
        
        cmdbuffer = bytearray()
        cmdbuffer.append(CMD)
        
        encode_int(cmdbuffer, StatusType.OPUS_BIT_RATE, bitrate)
        encode_int(cmdbuffer, StatusType.OUTPUT_SSRC, ssrc)
        encode_int(cmdbuffer, StatusType.COMMAND_TAG, secrets.randbits(31))
        encode_eol(cmdbuffer)
        
        logger.info(f"Setting Opus bitrate for SSRC {ssrc}: {bitrate} bps")
        self.send_command(cmdbuffer)
    
    def set_opus_dtx(self, ssrc: int, enable: bool):
        """
        Set Opus Discontinuous Transmission (DTX)
        
        DTX reduces bandwidth by not transmitting during silence periods.
        
        Args:
            ssrc: SSRC of the channel
            enable: True to enable DTX, False to disable
        
        Example:
            >>> control.set_opus_dtx(ssrc=12345, enable=True)
        """
        _validate_ssrc(ssrc)
        
        cmdbuffer = bytearray()
        cmdbuffer.append(CMD)
        
        encode_bool(cmdbuffer, StatusType.OPUS_DTX, enable)
        encode_int(cmdbuffer, StatusType.OUTPUT_SSRC, ssrc)
        encode_int(cmdbuffer, StatusType.COMMAND_TAG, secrets.randbits(31))
        encode_eol(cmdbuffer)
        
        logger.info(f"Setting Opus DTX for SSRC {ssrc}: {enable}")
        self.send_command(cmdbuffer)
    
    def set_opus_application(self, ssrc: int, application: int):
        """
        Set Opus encoder application type
        
        Args:
            ssrc: SSRC of the channel
            application: Application type
                2048 = VOIP (voice, optimized for speech)
                2049 = AUDIO (music/general audio)
                2051 = RESTRICTED_LOWDELAY (lowest latency)
        
        Example:
            >>> control.set_opus_application(ssrc=12345, application=2048)  # VOIP
        """
        _validate_ssrc(ssrc)
        if application not in [2048, 2049, 2051]:
            raise ValidationError(f"Invalid Opus application type: {application}")
        
        cmdbuffer = bytearray()
        cmdbuffer.append(CMD)
        
        encode_int(cmdbuffer, StatusType.OPUS_APPLICATION, application)
        encode_int(cmdbuffer, StatusType.OUTPUT_SSRC, ssrc)
        encode_int(cmdbuffer, StatusType.COMMAND_TAG, secrets.randbits(31))
        encode_eol(cmdbuffer)
        
        logger.info(f"Setting Opus application for SSRC {ssrc}: {application}")
        self.send_command(cmdbuffer)
    
    def set_opus_bandwidth(self, ssrc: int, bandwidth: int):
        """
        Set Opus encoder audio bandwidth limitation
        
        Args:
            ssrc: SSRC of the channel
            bandwidth: Bandwidth limit in Hz
                1101 = NARROWBAND (4 kHz)
                1102 = MEDIUMBAND (6 kHz)
                1103 = WIDEBAND (8 kHz)
                1104 = SUPERWIDEBAND (12 kHz)
                1105 = FULLBAND (20 kHz)
        
        Example:
            >>> control.set_opus_bandwidth(ssrc=12345, bandwidth=1103)  # Wideband
        """
        _validate_ssrc(ssrc)
        if bandwidth not in [1101, 1102, 1103, 1104, 1105]:
            raise ValidationError(f"Invalid Opus bandwidth: {bandwidth}")
        
        cmdbuffer = bytearray()
        cmdbuffer.append(CMD)
        
        encode_int(cmdbuffer, StatusType.OPUS_BANDWIDTH, bandwidth)
        encode_int(cmdbuffer, StatusType.OUTPUT_SSRC, ssrc)
        encode_int(cmdbuffer, StatusType.COMMAND_TAG, secrets.randbits(31))
        encode_eol(cmdbuffer)
        
        logger.info(f"Setting Opus bandwidth for SSRC {ssrc}: {bandwidth}")
        self.send_command(cmdbuffer)
    
    def set_opus_fec(self, ssrc: int, loss_percent: int):
        """
        Set Opus Forward Error Correction (FEC) expected packet loss rate
        
        FEC adds redundancy to protect against packet loss.
        
        Args:
            ssrc: SSRC of the channel
            loss_percent: Expected packet loss percentage (0-100)
        
        Example:
            >>> control.set_opus_fec(ssrc=12345, loss_percent=5)  # 5% expected loss
        """
        _validate_ssrc(ssrc)
        if not (0 <= loss_percent <= 100):
            raise ValidationError(f"Loss percent must be 0-100, got {loss_percent}")
        
        cmdbuffer = bytearray()
        cmdbuffer.append(CMD)
        
        encode_int(cmdbuffer, StatusType.OPUS_FEC, loss_percent)
        encode_int(cmdbuffer, StatusType.OUTPUT_SSRC, ssrc)
        encode_int(cmdbuffer, StatusType.COMMAND_TAG, secrets.randbits(31))
        encode_eol(cmdbuffer)
        
        logger.info(f"Setting Opus FEC for SSRC {ssrc}: {loss_percent}% expected loss")
        self.send_command(cmdbuffer)
    
    def set_max_delay(self, ssrc: int, max_blocks: int):
        """
        Set maximum allowable aggregation delay in blocks (0-5)
        
        Controls how many blocks radiod may aggregate before sending a packet.
        Higher values reduce packet rate but increase latency.
        
        Args:
            ssrc: SSRC of the channel
            max_blocks: Maximum delay in blocks (0-5). At 20ms/block: 0=immediate, 5=100ms
        
        Raises:
            ValidationError: If max_blocks is not 0-5
        
        Example:
            >>> control.set_max_delay(ssrc=12345, max_blocks=2)  # up to 40ms
        """
        _validate_ssrc(ssrc)
        if not (0 <= max_blocks <= 5):
            raise ValidationError(f"max_blocks must be 0-5, got {max_blocks}")
        
        cmdbuffer = bytearray()
        cmdbuffer.append(CMD)
        
        encode_int(cmdbuffer, StatusType.MAXDELAY, max_blocks)
        encode_int(cmdbuffer, StatusType.OUTPUT_SSRC, ssrc)
        encode_int(cmdbuffer, StatusType.COMMAND_TAG, secrets.randbits(31))
        encode_eol(cmdbuffer)
        
        logger.info(f"Setting max delay for SSRC {ssrc}: {max_blocks} blocks")
        self.send_command(cmdbuffer)
    
    # Backward compatibility alias
    def set_packet_buffering(self, ssrc: int, min_blocks: int):
        """Deprecated: use set_max_delay() instead."""
        self.set_max_delay(ssrc, min_blocks)
    
    def set_filter2(self, ssrc: int, blocksize: int, kaiser_beta: Optional[float] = None):
        """
        Configure secondary filter (linear modes only)
        
        The secondary filter provides additional selectivity after the main filter.
        
        Args:
            ssrc: SSRC of the channel
            blocksize: Filter blocksize (0 to disable, 1-10 to enable)
            kaiser_beta: Kaiser window beta for filter2 (optional)
        
        Example:
            >>> control.set_filter2(ssrc=12345, blocksize=5, kaiser_beta=3.0)
            >>> control.set_filter2(ssrc=12345, blocksize=0)  # Disable
        """
        _validate_ssrc(ssrc)
        if not (0 <= blocksize <= 10):
            raise ValidationError(f"blocksize must be 0-10, got {blocksize}")
        
        cmdbuffer = bytearray()
        cmdbuffer.append(CMD)
        
        encode_int(cmdbuffer, StatusType.FILTER2, blocksize)
        if kaiser_beta is not None:
            encode_float(cmdbuffer, StatusType.FILTER2_KAISER_BETA, kaiser_beta)
        
        encode_int(cmdbuffer, StatusType.OUTPUT_SSRC, ssrc)
        encode_int(cmdbuffer, StatusType.COMMAND_TAG, secrets.randbits(31))
        encode_eol(cmdbuffer)
        
        logger.info(f"Setting filter2 for SSRC {ssrc}: blocksize={blocksize}, beta={kaiser_beta}")
        self.send_command(cmdbuffer)
    
    def set_spectrum(self, ssrc: int, bin_bw_hz: Optional[float] = None, bin_count: Optional[int] = None,
                     crossover_hz: Optional[float] = None, kaiser_beta: Optional[float] = None):
        """
        Configure spectrum analyzer mode parameters
        
        Args:
            ssrc: SSRC of the channel
            bin_bw_hz: Bin bandwidth in Hz (optional)
            bin_count: Number of frequency bins (optional)
            crossover_hz: Crossover frequency between algorithms in Hz (optional)
            kaiser_beta: Kaiser window beta for spectrum analysis (optional)
        
        Example:
            >>> control.set_spectrum(ssrc=12345, bin_bw_hz=100, bin_count=512)
        """
        _validate_ssrc(ssrc)
        
        cmdbuffer = bytearray()
        cmdbuffer.append(CMD)
        
        if bin_bw_hz is not None:
            _validate_positive(bin_bw_hz, "Bin bandwidth")
            encode_float(cmdbuffer, StatusType.RESOLUTION_BW, bin_bw_hz)
        if bin_count is not None:
            if bin_count <= 0:
                raise ValidationError(f"bin_count must be positive, got {bin_count}")
            encode_int(cmdbuffer, StatusType.BIN_COUNT, bin_count)
        if crossover_hz is not None:
            _validate_positive(crossover_hz, "Crossover frequency")
            encode_float(cmdbuffer, StatusType.CROSSOVER, crossover_hz)
        if kaiser_beta is not None:
            encode_float(cmdbuffer, StatusType.SPECTRUM_SHAPE, kaiser_beta)
        
        encode_int(cmdbuffer, StatusType.OUTPUT_SSRC, ssrc)
        encode_int(cmdbuffer, StatusType.COMMAND_TAG, secrets.randbits(31))
        encode_eol(cmdbuffer)
        
        logger.info(f"Setting spectrum for SSRC {ssrc}: bw={bin_bw_hz} Hz, bins={bin_count}, crossover={crossover_hz} Hz")
        self.send_command(cmdbuffer)
    
    def set_status_interval(self, ssrc: int, interval: int):
        """
        Set automatic status reporting interval on data channel
        
        Args:
            ssrc: SSRC of the channel
            interval: Status interval in frames (0 to disable automatic status)
        
        Example:
            >>> control.set_status_interval(ssrc=12345, interval=50)  # Every 50 frames
            >>> control.set_status_interval(ssrc=12345, interval=0)   # Disable
        """
        _validate_ssrc(ssrc)
        if interval < 0:
            raise ValidationError(f"interval must be non-negative, got {interval}")
        
        cmdbuffer = bytearray()
        cmdbuffer.append(CMD)
        
        encode_int(cmdbuffer, StatusType.STATUS_INTERVAL, interval)
        encode_int(cmdbuffer, StatusType.OUTPUT_SSRC, ssrc)
        encode_int(cmdbuffer, StatusType.COMMAND_TAG, secrets.randbits(31))
        encode_eol(cmdbuffer)
        
        logger.info(f"Setting status interval for SSRC {ssrc}: {interval} frames")
        self.send_command(cmdbuffer)
    
    def set_demod_type(self, ssrc: int, demod_type: int):
        """
        Set demodulator type

        Args:
            ssrc: SSRC of the channel
            demod_type: Demodulator type (0=LINEAR, 1=FM, 2=WFM, 3=SPECTRUM, 4=SPECTRUM2)

        Raises:
            ValidationError: If demod_type is invalid

        Example:
            >>> control.set_demod_type(ssrc=12345, demod_type=1)  # FM
        """
        _validate_ssrc(ssrc)
        if not (0 <= demod_type <= 4):
            raise ValidationError(f"Invalid demod_type: {demod_type} (must be 0-4)")
        
        cmdbuffer = bytearray()
        cmdbuffer.append(CMD)
        
        encode_int(cmdbuffer, StatusType.DEMOD_TYPE, demod_type)
        encode_int(cmdbuffer, StatusType.OUTPUT_SSRC, ssrc)
        encode_int(cmdbuffer, StatusType.COMMAND_TAG, secrets.randbits(31))
        encode_eol(cmdbuffer)
        
        logger.info(f"Setting demod type for SSRC {ssrc}: {demod_type}")
        self.send_command(cmdbuffer)
    
    def set_output_encoding(self, ssrc: int, encoding: int):
        """
        Set output data encoding
        
        Args:
            ssrc: SSRC of the channel
            encoding: Encoding type (use Encoding constants from types.py)
                     0=NO_ENCODING, 1=S16BE, 2=S16LE, 3=F32, 4=F16, 5=OPUS
        
        Example:
            >>> from ka9q.types import Encoding
            >>> control.set_output_encoding(ssrc=12345, encoding=Encoding.S16LE)
            >>> control.set_output_encoding(ssrc=12345, encoding=Encoding.OPUS)
        """
        _validate_ssrc(ssrc)
        
        cmdbuffer = bytearray()
        cmdbuffer.append(CMD)
        
        encode_int(cmdbuffer, StatusType.OUTPUT_ENCODING, encoding)
        encode_int(cmdbuffer, StatusType.OUTPUT_SSRC, ssrc)
        encode_int(cmdbuffer, StatusType.COMMAND_TAG, secrets.randbits(31))
        encode_eol(cmdbuffer)
        
        logger.info(f"Setting output encoding for SSRC {ssrc}: {encoding}")
        self.send_command(cmdbuffer)
    
    def set_rf_gain(self, ssrc: int, gain_db: float):
        """
        Set RF front-end gain
        
        Args:
            ssrc: SSRC of the channel
            gain_db: RF gain in dB (hardware-dependent range)
        
        Note:
            Only works with hardware that supports variable RF gain (e.g., RX888)
        
        Example:
            >>> control.set_rf_gain(ssrc=12345, gain_db=20)
        """
        _validate_ssrc(ssrc)
        
        cmdbuffer = bytearray()
        cmdbuffer.append(CMD)
        
        encode_float(cmdbuffer, StatusType.RF_GAIN, gain_db)
        encode_int(cmdbuffer, StatusType.OUTPUT_SSRC, ssrc)
        encode_int(cmdbuffer, StatusType.COMMAND_TAG, secrets.randbits(31))
        encode_eol(cmdbuffer)
        
        logger.info(f"Setting RF gain for SSRC {ssrc}: {gain_db} dB")
        self.send_command(cmdbuffer)
    
    def set_rf_attenuation(self, ssrc: int, atten_db: float):
        """
        Set RF front-end attenuation
        
        Args:
            ssrc: SSRC of the channel
            atten_db: RF attenuation in dB (hardware-dependent range)
        
        Note:
            Only works with hardware that supports variable RF attenuation (e.g., RX888)
        
        Example:
            >>> control.set_rf_attenuation(ssrc=12345, atten_db=10)
        """
        _validate_ssrc(ssrc)
        
        cmdbuffer = bytearray()
        cmdbuffer.append(CMD)
        
        encode_float(cmdbuffer, StatusType.RF_ATTEN, atten_db)
        encode_int(cmdbuffer, StatusType.OUTPUT_SSRC, ssrc)
        encode_int(cmdbuffer, StatusType.COMMAND_TAG, secrets.randbits(31))
        encode_eol(cmdbuffer)
        
        logger.info(f"Setting RF attenuation for SSRC {ssrc}: {atten_db} dB")
        self.send_command(cmdbuffer)
    
    def set_destination(self, ssrc: int, address: str, port: int = 5004):
        """
        Set RTP output destination (multicast address)
        
        This sets both the data and status destination addresses.
        
        Args:
            ssrc: SSRC of the channel
            address: Multicast IP address or mDNS name
            port: RTP port number (default: 5004)
        
        Example:
            >>> control.set_destination(ssrc=12345, address="239.1.2.3", port=5004)
            >>> control.set_destination(ssrc=12345, address="wspr.local")
        """
        _validate_ssrc(ssrc)
        _validate_multicast_address(address)
        
        cmdbuffer = bytearray()
        cmdbuffer.append(CMD)
        
        encode_socket(cmdbuffer, StatusType.OUTPUT_DATA_DEST_SOCKET, address, port)
        encode_int(cmdbuffer, StatusType.OUTPUT_SSRC, ssrc)
        encode_int(cmdbuffer, StatusType.COMMAND_TAG, secrets.randbits(31))
        encode_eol(cmdbuffer)
        
        logger.info(f"Setting destination for SSRC {ssrc}: {address}:{port}")
        self.send_command(cmdbuffer)
    
    def set_first_lo(self, ssrc: int, frequency_hz: float):
        """
        Set first LO (front-end tuner) frequency
        
        This tunes the SDR hardware itself. Use with caution as it affects all channels.
        
        Args:
            ssrc: SSRC of the channel (used for command routing)
            frequency_hz: First LO frequency in Hz
        
        Example:
            >>> control.set_first_lo(ssrc=12345, frequency_hz=14.1e6)
        """
        _validate_ssrc(ssrc)
        _validate_frequency(frequency_hz)
        
        cmdbuffer = bytearray()
        cmdbuffer.append(CMD)
        
        encode_double(cmdbuffer, StatusType.FIRST_LO_FREQUENCY, frequency_hz)
        encode_int(cmdbuffer, StatusType.OUTPUT_SSRC, ssrc)
        encode_int(cmdbuffer, StatusType.COMMAND_TAG, secrets.randbits(31))
        encode_eol(cmdbuffer)
        
        logger.info(f"Setting first LO frequency for SSRC {ssrc}: {frequency_hz/1e6:.3f} MHz")
        self.send_command(cmdbuffer)
    
    def set_options(self, ssrc: int, set_bits: int = 0, clear_bits: int = 0):
        """
        Set or clear option bits
        
        Option bits are used for experimental features and debugging.
        
        Args:
            ssrc: SSRC of the channel
            set_bits: Bit mask of options to set (OR operation)
            clear_bits: Bit mask of options to clear (AND NOT operation)
        
        Example:
            >>> control.set_options(ssrc=12345, set_bits=0x01)  # Set bit 0
            >>> control.set_options(ssrc=12345, clear_bits=0x02)  # Clear bit 1
        """
        _validate_ssrc(ssrc)
        
        cmdbuffer = bytearray()
        cmdbuffer.append(CMD)
        
        if set_bits:
            encode_int64(cmdbuffer, StatusType.SETOPTS, set_bits)
        if clear_bits:
            encode_int64(cmdbuffer, StatusType.CLEAROPTS, clear_bits)
        
        encode_int(cmdbuffer, StatusType.OUTPUT_SSRC, ssrc)
        encode_int(cmdbuffer, StatusType.COMMAND_TAG, secrets.randbits(31))
        encode_eol(cmdbuffer)
        
        logger.info(f"Setting options for SSRC {ssrc}: set=0x{set_bits:x}, clear=0x{clear_bits:x}")
        self.send_command(cmdbuffer)
    
    # ------------------------------------------------------------------
    # Additional setters for full `control` parity
    # ------------------------------------------------------------------

    def set_lock(self, ssrc: int, lock: bool):
        """Lock/unlock the tuner (ignore retune commands when locked)."""
        _validate_ssrc(ssrc)
        cmdbuffer = bytearray()
        cmdbuffer.append(CMD)
        encode_int(cmdbuffer, StatusType.LOCK, 1 if lock else 0)
        encode_int(cmdbuffer, StatusType.OUTPUT_SSRC, ssrc)
        encode_int(cmdbuffer, StatusType.COMMAND_TAG, secrets.randbits(31))
        encode_eol(cmdbuffer)
        logger.info(f"Setting LOCK={lock} for SSRC {ssrc}")
        self.send_command(cmdbuffer)

    def set_pl_tone(self, ssrc: int, freq_hz: float):
        """Set FM PL (CTCSS) tone-squelch frequency in Hz. 0 disables."""
        _validate_ssrc(ssrc)
        cmdbuffer = bytearray()
        cmdbuffer.append(CMD)
        encode_float(cmdbuffer, StatusType.PL_TONE, freq_hz)
        encode_int(cmdbuffer, StatusType.OUTPUT_SSRC, ssrc)
        encode_int(cmdbuffer, StatusType.COMMAND_TAG, secrets.randbits(31))
        encode_eol(cmdbuffer)
        logger.info(f"Setting PL tone = {freq_hz} Hz for SSRC {ssrc}")
        self.send_command(cmdbuffer)

    def set_headroom(self, ssrc: int, headroom_db: float):
        """Set audio-level headroom (dB below full scale, typically negative)."""
        _validate_ssrc(ssrc)
        cmdbuffer = bytearray()
        cmdbuffer.append(CMD)
        encode_float(cmdbuffer, StatusType.HEADROOM, headroom_db)
        encode_int(cmdbuffer, StatusType.OUTPUT_SSRC, ssrc)
        encode_int(cmdbuffer, StatusType.COMMAND_TAG, secrets.randbits(31))
        encode_eol(cmdbuffer)
        logger.info(f"Setting headroom = {headroom_db} dB for SSRC {ssrc}")
        self.send_command(cmdbuffer)

    def set_agc_hangtime(self, ssrc: int, seconds: float):
        """Set AGC hang time in seconds."""
        _validate_ssrc(ssrc)
        cmdbuffer = bytearray()
        cmdbuffer.append(CMD)
        encode_float(cmdbuffer, StatusType.AGC_HANGTIME, seconds)
        encode_int(cmdbuffer, StatusType.OUTPUT_SSRC, ssrc)
        encode_int(cmdbuffer, StatusType.COMMAND_TAG, secrets.randbits(31))
        encode_eol(cmdbuffer)
        self.send_command(cmdbuffer)

    def set_agc_recovery_rate(self, ssrc: int, db_per_sec: float):
        """Set AGC recovery rate in dB/sec."""
        _validate_ssrc(ssrc)
        cmdbuffer = bytearray()
        cmdbuffer.append(CMD)
        encode_float(cmdbuffer, StatusType.AGC_RECOVERY_RATE, db_per_sec)
        encode_int(cmdbuffer, StatusType.OUTPUT_SSRC, ssrc)
        encode_int(cmdbuffer, StatusType.COMMAND_TAG, secrets.randbits(31))
        encode_eol(cmdbuffer)
        self.send_command(cmdbuffer)

    def set_kaiser_beta(self, ssrc: int, beta: float):
        """Set primary filter Kaiser β."""
        self.set_filter(ssrc, kaiser_beta=beta)

    def set_description(self, ssrc: int, description: str):
        """Set the front-end / channel description string."""
        _validate_ssrc(ssrc)
        cmdbuffer = bytearray()
        cmdbuffer.append(CMD)
        encode_string(cmdbuffer, StatusType.DESCRIPTION, description)
        encode_int(cmdbuffer, StatusType.OUTPUT_SSRC, ssrc)
        encode_int(cmdbuffer, StatusType.COMMAND_TAG, secrets.randbits(31))
        encode_eol(cmdbuffer)
        self.send_command(cmdbuffer)

    # ------------------------------------------------------------------
    # Typed status access (used by CLI / TUI)
    # ------------------------------------------------------------------

    def poll_status(self, ssrc: int, timeout: float = 2.0):
        """Send a poll for a specific SSRC and return a typed ChannelStatus.

        Unlike :meth:`tune`, this does not change channel state — it sends
        a command containing only the SSRC + command tag, which radiod
        answers with a full status packet.
        """
        import select
        import time as _time
        from .status import decode_status_packet

        _validate_ssrc(ssrc)
        _validate_timeout(timeout)

        command_tag = secrets.randbits(31)
        cmdbuffer = bytearray()
        cmdbuffer.append(CMD)
        encode_int(cmdbuffer, StatusType.COMMAND_TAG, command_tag)
        encode_int(cmdbuffer, StatusType.OUTPUT_SSRC, ssrc)
        encode_eol(cmdbuffer)

        status_sock = self._get_or_create_status_listener()
        start = _time.time()
        last_send = 0.0
        retry = 0.1
        while _time.time() - start < timeout:
            now = _time.time()
            if now - last_send >= retry:
                self.send_command(cmdbuffer)
                last_send = now
                retry = min(retry * 2, 1.0)
            remaining = timeout - (now - start)
            ready = select.select([status_sock], [], [], min(retry, remaining, 0.5))
            if not ready[0]:
                continue
            try:
                buf, _addr = status_sock.recvfrom(8192)
            except socket.timeout:
                continue
            st = decode_status_packet(buf)
            if st is None:
                continue
            if st.ssrc == ssrc and st.command_tag == command_tag:
                return st
        raise TimeoutError(f"No status response received for SSRC {ssrc} within {timeout}s")

    def listen_status(self, callback, duration: Optional[float] = None,
                      ssrcs: Optional[set] = None):
        """Passively receive status packets and invoke ``callback(ChannelStatus)``.

        radiod periodically multicasts status for every channel. This method
        joins the status group and fans those packets out to the caller,
        optionally filtering by SSRC. Blocks for ``duration`` seconds, or
        indefinitely if None.
        """
        import select
        import time as _time
        from .status import decode_status_packet

        status_sock = self._get_or_create_status_listener()
        start = _time.time()
        while True:
            if duration is not None and _time.time() - start >= duration:
                return
            remaining = None if duration is None else max(0.0, duration - (_time.time() - start))
            ready = select.select([status_sock], [], [], 0.5 if remaining is None else min(0.5, remaining))
            if not ready[0]:
                continue
            try:
                buf, _addr = status_sock.recvfrom(8192)
            except socket.timeout:
                continue
            st = decode_status_packet(buf)
            if st is None or st.ssrc is None:
                continue
            if ssrcs is not None and st.ssrc not in ssrcs:
                continue
            try:
                callback(st)
            except Exception as exc:
                logger.warning(f"listen_status callback raised: {exc}")

    # ── Continuous STATUS listener (v3.16.0+) ──────────────────────

    @property
    def status_listener(self):
        """The continuous STATUS listener, or ``None`` if not started.

        Created lazily by :py:meth:`start_status_listener`.  See
        :py:class:`ka9q.status_listener.StatusListener` for usage —
        register channels and callbacks via that object directly.
        """
        return self._status_listener

    def start_status_listener(self, socket_timeout: float = 0.5):
        """Start a continuous STATUS multicast listener (idempotent).

        Spawns a background thread that subscribes to radiod's STATUS
        multicast and refreshes the ``gps_time``/``rtp_timesnap`` anchor
        on every broadcast for all channels registered via
        :py:meth:`StatusListener.register_channel`.

        Returns the :py:class:`StatusListener` instance.

        See :py:class:`ka9q.status_listener.StatusListener` for full
        usage, including per-SSRC callbacks.

        Closing the :py:class:`RadiodControl` (via :py:meth:`close` or
        the context manager) stops the listener.
        """
        from .status_listener import StatusListener
        if self._status_listener is None:
            self._status_listener = StatusListener(
                status_address=self.status_address,
                interface=self.interface,
                socket_timeout=socket_timeout,
            )
        if not self._status_listener.running:
            self._status_listener.start()
        return self._status_listener

    def stop_status_listener(self, timeout: float = 2.0):
        """Stop the continuous STATUS listener, if running."""
        if self._status_listener is not None:
            self._status_listener.stop(timeout=timeout)

    def close(self):
        """
        Close all sockets with proper error handling

        This method is safe to call multiple times and handles errors gracefully.
        """
        errors = []

        # Stop continuous STATUS listener if running
        if self._status_listener is not None:
            try:
                self._status_listener.stop()
            except Exception as e:
                errors.append(f"status listener: {e}")
            finally:
                self._status_listener = None

        # Close control socket
        if self.socket:
            try:
                self.socket.close()
                logger.debug("Control socket closed")
            except Exception as e:
                errors.append(f"control socket: {e}")
            finally:
                self.socket = None
        
        # Close cached status listener socket
        if self._status_sock:
            try:
                logger.debug("Closing cached status listener socket")
                self._status_sock.close()
            except Exception as e:
                errors.append(f"status socket: {e}")
            finally:
                self._status_sock = None
        
        if errors:
            error_msg = "; ".join(errors)
            logger.warning(f"Errors during socket cleanup: {error_msg}")
    
    def __del__(self):
        """
        Ensure resources are cleaned up on garbage collection
        
        This provides a safety net for unclosed connections and helps
        detect resource leaks during development.
        """
        try:
            self.close()
        except Exception:
            pass  # Can't raise exceptions in __del__

