"""
Generic RTP Recorder with Timing Support

This module provides a generic RTP recording framework with:
- State machine (idle → armed → recording → resync)
- Precise timing from radiod (GPS_TIME, RTP_TIMESNAP)
- Callbacks for application-specific storage
- Sequence number and timestamp validation
- Automatic resynchronization on errors

Designed to be reusable across different recording applications
(WSPR, FT8, general-purpose, etc.)
"""

import socket
import struct
import logging
import time
from enum import Enum
from typing import Optional, Callable, NamedTuple, Dict, Any
from dataclasses import dataclass
import threading

from .discovery import ChannelInfo

logger = logging.getLogger(__name__)


# RTP Constants (from radiod)
GPS_UTC_OFFSET = 315964800  # GPS epoch (1980-01-06) - Unix epoch (1970-01-01)  
UNIX_EPOCH = 2208988800     # Unix epoch in NTP seconds
BILLION = 1_000_000_000
GPS_LEAP_SECONDS = 18   # GPS time is ahead of UTC by 18 seconds (as of 2025)


class RecorderState(Enum):
    """Recorder state machine states"""
    IDLE = "idle"              # Not recording
    ARMED = "armed"            # Waiting for trigger condition
    RECORDING = "recording"    # Actively recording
    RESYNC = "resync"          # Lost sync, trying to recover


class RTPHeader(NamedTuple):
    """Parsed RTP packet header"""
    version: int
    padding: bool
    extension: bool
    csrc_count: int
    marker: bool
    payload_type: int
    sequence: int
    timestamp: int
    ssrc: int


@dataclass
class RecordingMetrics:
    """Recording session metrics"""
    packets_received: int = 0
    packets_dropped: int = 0
    packets_out_of_order: int = 0
    bytes_received: int = 0
    sequence_errors: int = 0
    timestamp_jumps: int = 0
    state_changes: int = 0
    recording_start_time: Optional[float] = None
    recording_stop_time: Optional[float] = None
    
    def to_dict(self) -> dict:
        """Convert metrics to dictionary"""
        return {
            'packets_received': self.packets_received,
            'packets_dropped': self.packets_dropped,
            'packets_out_of_order': self.packets_out_of_order,
            'bytes_received': self.bytes_received,
            'sequence_errors': self.sequence_errors,
            'timestamp_jumps': self.timestamp_jumps,
            'state_changes': self.state_changes,
            'recording_duration': (
                self.recording_stop_time - self.recording_start_time
                if self.recording_start_time and self.recording_stop_time
                else None
            )
        }


def parse_rtp_header(data: bytes) -> Optional[RTPHeader]:
    """
    Parse RTP packet header
    
    Args:
        data: Raw packet bytes (minimum 12 bytes)
    
    Returns:
        RTPHeader if valid, None if invalid
    """
    if len(data) < 12:
        return None
    
    # Parse RTP header (RFC 3550)
    byte0, byte1 = struct.unpack('!BB', data[0:2])
    
    version = (byte0 >> 6) & 0x03
    padding = bool(byte0 & 0x20)
    extension = bool(byte0 & 0x10)
    csrc_count = byte0 & 0x0F
    
    marker = bool(byte1 & 0x80)
    payload_type = byte1 & 0x7F
    
    sequence, timestamp, ssrc = struct.unpack('!HIL', data[2:12])
    
    return RTPHeader(
        version=version,
        padding=padding,
        extension=extension,
        csrc_count=csrc_count,
        marker=marker,
        payload_type=payload_type,
        sequence=sequence,
        timestamp=timestamp,
        ssrc=ssrc
    )


def rtp_to_utc(
    rtp_timestamp: int,
    channel: ChannelInfo,
    wallclock_hint_sec: Optional[float] = None,
) -> Optional[float]:
    """
    Convert an RTP timestamp to absolute UTC (Unix seconds).

    The timing REFERENCE is radiod's GPS_TIME/RTP_TIMESNAP anchor, not the
    host clock:  ``utc = gps_utc_at_snapshot + (rtp - rtp_at_snapshot)/rate``.
    The host clock (``wallclock_hint_sec`` / ``time.time()``) is consulted for
    ONE thing only — disambiguating the 32-bit RTP wrap epoch — and needs only
    ±half-a-period accuracy.  It never contributes to the sub-period value.

    (Formerly ``rtp_to_wallclock``; renamed 2026-06-27 — "wallclock" described
    the OUTPUT format, not the reference, and misread as a host-clock
    dependency.  ``rtp_to_wallclock`` remains as a deprecated alias below.)

    Uses the GPS_TIME/RTP_TIMESNAP timing information from radiod.

    Args:
        rtp_timestamp: RTP timestamp from packet header
        channel: ChannelInfo with gps_time, rtp_timesnap, sample_rate
        wallclock_hint_sec: Approximate UTC seconds, used solely to
            disambiguate the 32-bit RTP wrap epoch (see below).  Must be
            within ±period/2 of true UTC (period = 2**32 / sample_rate
            seconds, ≥6 hours for typical sample rates).  When omitted,
            falls back to ``time.time()`` — convenient but couples the
            result to the host system clock.  Callers that have an
            hf-timestd authority offset available should pass it
            explicitly to keep the labeling path off the chrony-disciplined
            system clock (METROLOGY.md §4.5 RTP-reference invariant).

    Returns:
        Unix timestamp (seconds) or None if timing info unavailable

    Wraparound handling:
        RTP timestamps are 32-bit and wrap every 2**32 samples.  At
        96 kHz that's 12.43 hours; at 16 kHz it's 74.6 hours.  The
        ``rtp_timesnap``/``gps_time`` pair is captured once at SSRC
        discovery, so an SSRC alive longer than one wrap period is
        in a *later* RTP epoch than the snapshot — the naive signed
        32-bit subtraction is correct only within ±2**31 samples
        (~6 hours at 96 kHz) of the snapshot, beyond which it
        silently aliases.

        We disambiguate by picking the wrap-epoch count ``k`` (full
        2**32-sample periods elapsed since the snapshot) that places
        the resulting wall-clock time closest to ``wallclock_hint_sec``
        (or ``time.time()`` if no hint was given).  Either source needs
        only ±period/2 accuracy, so this stays robust even when the
        hinting reference is loose.

        Observed on bee1 2026-05-08: long-running SSRCs caused TSL3
        SHM samples stuck at the snapshot's wall-clock time, ~12.4 h
        behind.  Chrony filtered every sample and reach fell to 0.
    """
    # Read the (gps_time, rtp_timesnap) anchor as a single atomic
    # snapshot.  This matters when a background StatusListener (v3.16.0+)
    # is mutating the anchor concurrently: separate reads of
    # ``channel.gps_time`` and ``channel.rtp_timesnap`` could land
    # either side of an update and yield a torn pair off by up to one
    # listener-cadence interval (typically ~450 ms on bee1).  The
    # tuple lives in ``_anchor_pair``, a single GIL-atomic attribute.
    # See ChannelInfo.get_anchor() docstring for the full story.
    anchor = channel.get_anchor()
    if anchor is None:
        return None
    gps_time, rtp_timesnap = anchor
    if gps_time is None or rtp_timesnap is None:
        return None

    # Convert GPS nanoseconds to Unix time
    # GPS epoch is Jan 6, 1980; Unix epoch is Jan 1, 1970
    # gps_time is nanoseconds since GPS epoch, so add GPS_UTC_OFFSET (in ns)
    # AND subtract current GPS_LEAP_SECONDS (18s) to align with UTC
    sender_time = gps_time + BILLION * (GPS_UTC_OFFSET - GPS_LEAP_SECONDS)

    # Signed 32-bit RTP delta — correct within ±2**31 samples of
    # the snapshot.
    rtp_delta = int((rtp_timestamp - rtp_timesnap) & 0xFFFFFFFF)
    if rtp_delta > 0x7FFFFFFF:
        rtp_delta -= 0x100000000

    base_wall_ns = sender_time + (BILLION * rtp_delta // channel.sample_rate)

    # Adjust for wrap epochs.  One period = 2**32 samples =
    # BILLION * 2**32 // sample_rate ns.  Pick the integer k that
    # minimises |base_wall_ns + k*period - sys_now_ns| — i.e. the
    # value closest to the system clock.  Exact when sys clock is
    # within ±period/2 of true UTC.
    period_ns = BILLION * 0x100000000 // channel.sample_rate
    if wallclock_hint_sec is not None:
        sys_now_ns = int(wallclock_hint_sec * BILLION)
    else:
        sys_now_ns = int(time.time() * BILLION)
    diff_ns = sys_now_ns - base_wall_ns
    if period_ns > 0:
        # Round-to-nearest of diff_ns / period_ns (Python `//` is
        # floor; bias by half-period before flooring to round).
        if diff_ns >= 0:
            k = (diff_ns + period_ns // 2) // period_ns
        else:
            k = -(((-diff_ns) + period_ns // 2) // period_ns)
    else:
        k = 0
    wall_time_ns = base_wall_ns + k * period_ns

    # Apply L6 BPSK PPS chain-delay calibration if available.
    # This corrects for the end-to-end RF→ADC→DSP→RTP latency
    # measured by a local BPSK PPS injector.
    if channel.chain_delay_correction_ns is not None:
        wall_time_ns -= channel.chain_delay_correction_ns

    # Convert to Unix seconds
    return wall_time_ns / BILLION


# Deprecated alias — kept so existing callers keep working.  Prefer
# ``rtp_to_utc`` (the reference is RTP/GPS, not the host wall clock).
rtp_to_wallclock = rtp_to_utc


class RTPRecorder:
    """
    Generic RTP recorder with state machine and timing support
    
    Callbacks allow application-specific behavior:
    - on_packet: Called for each received packet
    - on_state_change: Called when state changes
    - on_recording_start: Called when recording starts
    - on_recording_stop: Called when recording stops
    """
    
    def __init__(
        self,
        channel: ChannelInfo,
        on_packet: Optional[Callable[[RTPHeader, bytes, float], None]] = None,
        on_state_change: Optional[Callable[[RecorderState, RecorderState], None]] = None,
        on_recording_start: Optional[Callable[[], None]] = None,
        on_recording_stop: Optional[Callable[[RecordingMetrics], None]] = None,
        max_packet_gap: int = 10,
        resync_threshold: int = 5,
        pass_all_packets: bool = False
    ):
        """
        Initialize RTP recorder
        
        Args:
            channel: ChannelInfo with RTP stream details and timing
            on_packet: Callback(header, payload, wallclock_time) for each packet
            on_state_change: Callback(old_state, new_state) on state changes
            on_recording_start: Callback when recording begins
            on_recording_stop: Callback(metrics) when recording ends
            max_packet_gap: Max sequence gap before triggering resync (ignored if pass_all_packets=True)
            resync_threshold: Number of good packets needed to recover from resync
            pass_all_packets: If True, pass ALL packets to callback regardless of sequence.
                             Metrics still track errors. Use when downstream has its own resequencer.
        """
        self.channel = channel
        self.on_packet = on_packet
        self.on_state_change = on_state_change
        self.on_recording_start = on_recording_start
        self.on_recording_stop = on_recording_stop
        
        self.max_packet_gap = max_packet_gap
        self.resync_threshold = resync_threshold
        self.pass_all_packets = pass_all_packets
        
        self.state = RecorderState.IDLE
        self.metrics = RecordingMetrics()
        
        # RTP state tracking
        self.last_sequence: Optional[int] = None
        self.last_timestamp: Optional[int] = None
        self.resync_good_packets = 0
        
        # Socket
        self.socket: Optional[socket.socket] = None
        self.running = False
        self.thread: Optional[threading.Thread] = None
        
        # Reconnection state for robustness
        self._reconnect_backoff = 1.0  # Initial backoff in seconds
        self._max_reconnect_backoff = 60.0  # Max backoff
        self._consecutive_errors = 0
    
    def _change_state(self, new_state: RecorderState):
        """Change state and trigger callback"""
        if new_state == self.state:
            return
        
        old_state = self.state
        self.state = new_state
        self.metrics.state_changes += 1
        
        logger.info(f"State: {old_state.value} → {new_state.value}")
        
        if self.on_state_change:
            try:
                self.on_state_change(old_state, new_state)
            except Exception as e:
                logger.error(f"Error in state_change callback: {e}", exc_info=True)
        
        # Trigger recording callbacks
        if new_state == RecorderState.RECORDING and self.on_recording_start:
            self.metrics.recording_start_time = time.time()
            try:
                self.on_recording_start()
            except Exception as e:
                logger.error(f"Error in recording_start callback: {e}", exc_info=True)
        
        elif old_state == RecorderState.RECORDING and new_state != RecorderState.RECORDING:
            self.metrics.recording_stop_time = time.time()
            if self.on_recording_stop:
                try:
                    self.on_recording_stop(self.metrics)
                except Exception as e:
                    logger.error(f"Error in recording_stop callback: {e}", exc_info=True)
    
    def _create_socket(self) -> socket.socket:
        """Create and configure RTP receive socket"""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        
        if hasattr(socket, 'SO_REUSEPORT'):
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass
        
        # Bind to port
        sock.bind(('0.0.0.0', self.channel.port))
        
        # Join multicast group
        mreq = struct.pack('=4s4s',
                          socket.inet_aton(self.channel.multicast_address),
                          socket.inet_aton('0.0.0.0'))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        
        sock.settimeout(1.0)  # Allow periodic checks of self.running
        
        logger.info(f"Listening on {self.channel.multicast_address}:{self.channel.port}")
        
        return sock
    
    def _validate_packet(self, header: RTPHeader) -> bool:
        """
        Validate RTP packet and update state
        
        Returns:
            True if packet should be processed, False if dropped
        """
        # Check SSRC - always filter wrong SSRC
        if header.ssrc != self.channel.ssrc:
            logger.debug(f"Wrong SSRC: {header.ssrc} (expected {self.channel.ssrc})")
            return False
        
        # First packet
        if self.last_sequence is None:
            self.last_sequence = header.sequence
            self.last_timestamp = header.timestamp
            return True
        
        # Check sequence number (track metrics even in pass_all mode)
        expected_seq = (self.last_sequence + 1) & 0xFFFF
        if header.sequence != expected_seq:
            seq_gap = (header.sequence - expected_seq) & 0xFFFF
            
            if seq_gap > self.max_packet_gap:
                logger.warning(
                    f"Large sequence gap: {seq_gap} packets "
                    f"(got {header.sequence}, expected {expected_seq})"
                )
                self.metrics.sequence_errors += 1
                
                # In pass_all mode, don't trigger resync - just log and continue
                if not self.pass_all_packets:
                    # Trigger resync if recording
                    if self.state == RecorderState.RECORDING:
                        self._change_state(RecorderState.RESYNC)
                        self.resync_good_packets = 0
                        return False
            else:
                self.metrics.packets_dropped += seq_gap - 1
        
        # Check timestamp progression
        if self.last_timestamp is not None:
            ts_delta = (header.timestamp - self.last_timestamp) & 0xFFFFFFFF
            
            # Detect large jumps (more than 1 second worth)
            if ts_delta > self.channel.sample_rate:
                logger.warning(
                    f"Timestamp jump: {ts_delta} samples "
                    f"({ts_delta / self.channel.sample_rate:.2f}s)"
                )
                self.metrics.timestamp_jumps += 1
        
        self.last_sequence = header.sequence
        self.last_timestamp = header.timestamp
        
        # In pass_all mode, skip resync state handling - always deliver
        if self.pass_all_packets:
            return True
        
        # Handle resync state (original behavior)
        if self.state == RecorderState.RESYNC:
            self.resync_good_packets += 1
            if self.resync_good_packets >= self.resync_threshold:
                logger.info(f"Resync successful after {self.resync_good_packets} packets")
                self._change_state(RecorderState.RECORDING)
                return True
            else:
                return False  # Drop packets during resync
        
        return True
    
    def _receive_loop(self):
        """Main packet receiving loop with automatic reconnection.
        
        Handles socket errors by attempting to recreate the socket with
        exponential backoff. This provides robustness against:
        - Network interface restarts
        - Multicast group membership drops
        - Transient network errors
        """
        while self.running:
            try:
                # Create socket if needed
                if self.socket is None:
                    self.socket = self._create_socket()
                    self._reconnect_backoff = 1.0  # Reset backoff on success
                    self._consecutive_errors = 0
                
                data, addr = self.socket.recvfrom(8192)
                
                self.metrics.packets_received += 1
                self.metrics.bytes_received += len(data)
                self._consecutive_errors = 0  # Reset on successful receive
                
                # Parse RTP header
                header = parse_rtp_header(data)
                if not header:
                    logger.debug("Invalid RTP packet")
                    continue
                
                # Validate packet
                if not self._validate_packet(header):
                    continue
                
                # Extract payload (skip RTP header + CSRC)
                header_len = 12 + (4 * header.csrc_count)
                payload = data[header_len:]
                
                # Compute wall-clock time
                wallclock = rtp_to_wallclock(header.timestamp, self.channel)
                
                # Call packet callback
                if self.on_packet:
                    try:
                        self.on_packet(header, payload, wallclock)
                    except Exception as e:
                        logger.error(f"Error in packet callback: {e}", exc_info=True)
            
            except socket.timeout:
                continue
                
            except OSError as e:
                # Socket error - attempt reconnection
                if not self.running:
                    break
                
                self._consecutive_errors += 1
                logger.error(
                    f"Socket error (attempt {self._consecutive_errors}): {e}",
                    exc_info=True
                )
                
                # Close broken socket
                if self.socket:
                    try:
                        self.socket.close()
                    except Exception:
                        pass
                    self.socket = None
                
                # Exponential backoff before reconnection
                logger.info(
                    f"Attempting socket reconnection in {self._reconnect_backoff:.1f}s..."
                )
                time.sleep(self._reconnect_backoff)
                
                # Increase backoff for next attempt (capped at max)
                self._reconnect_backoff = min(
                    self._reconnect_backoff * 2,
                    self._max_reconnect_backoff
                )
                
            except Exception as e:
                if self.running:
                    logger.error(f"Error receiving packet: {e}", exc_info=True)
        
        # Cleanup on exit
        if self.socket:
            try:
                self.socket.close()
            except Exception:
                pass
            self.socket = None
    
    def start(self):
        """Start receiving RTP packets"""
        if self.running:
            logger.warning("Recorder already running")
            return
        
        logger.info(f"Starting RTP recorder for SSRC {self.channel.ssrc}")
        
        self.running = True
        self.thread = threading.Thread(target=self._receive_loop, daemon=True)
        self.thread.start()
        
        self._change_state(RecorderState.ARMED)
    
    def stop(self):
        """Stop receiving RTP packets"""
        if not self.running:
            return
        
        logger.info("Stopping RTP recorder")
        
        self.running = False
        if self.thread:
            self.thread.join(timeout=5.0)
            self.thread = None
        
        self._change_state(RecorderState.IDLE)
    
    def start_recording(self):
        """Transition from ARMED to RECORDING"""
        if self.state == RecorderState.ARMED:
            self._change_state(RecorderState.RECORDING)
        else:
            logger.warning(f"Cannot start recording from state {self.state.value}")
    
    def stop_recording(self):
        """Transition from RECORDING back to ARMED"""
        if self.state in (RecorderState.RECORDING, RecorderState.RESYNC):
            self._change_state(RecorderState.ARMED)
        else:
            logger.warning(f"Cannot stop recording from state {self.state.value}")
    
    def get_metrics(self) -> Dict[str, Any]:
        """Get current recording metrics"""
        return self.metrics.to_dict()
    
    def reset_metrics(self):
        """Reset all metrics"""
        self.metrics = RecordingMetrics()
    
    def __del__(self):
        """
        Ensure recorder is stopped on garbage collection
        
        This provides a safety net for unclosed recorders and helps
        detect resource leaks during development.
        """
        try:
            self.stop()
        except Exception:
            pass  # Can't raise exceptions in __del__
