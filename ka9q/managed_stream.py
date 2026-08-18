"""
ManagedStream - Self-Healing RTP Stream with Automatic Restoration

Provides a robust stream interface that automatically detects when an RTP stream
has dropped (e.g., radiod restart) and restores it as quickly as possible.

Features:
- Stream health monitoring with configurable timeout
- Automatic channel restoration via ensure_channel()
- Callbacks for stream drop and restoration events
- Continuous sample delivery through disruptions (with gap tracking)

Usage:
    from ka9q import RadiodControl, ManagedStream
    
    def on_samples(samples, quality):
        process(samples)
    
    def on_stream_dropped(reason):
        print(f"Stream dropped: {reason}")
    
    def on_stream_restored(channel):
        print(f"Stream restored: {channel.frequency/1e6:.3f} MHz")
    
    with RadiodControl("radiod.local") as control:
        stream = ManagedStream(
            control=control,
            frequency_hz=14.074e6,
            preset="usb",
            sample_rate=12000,
            on_samples=on_samples,
            on_stream_dropped=on_stream_dropped,
            on_stream_restored=on_stream_restored,
        )
        stream.start()
        # ... stream auto-heals through radiod restarts ...
        stream.stop()
"""

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional, Callable, List

import numpy as np

from .discovery import ChannelInfo
from .stream import RadiodStream, SampleCallback
from .stream_quality import StreamQuality, GapSource, GapEvent

logger = logging.getLogger(__name__)


class StreamState(Enum):
    """Current state of the managed stream"""
    STOPPED = "stopped"
    STARTING = "starting"
    HEALTHY = "healthy"
    DROPPED = "dropped"
    RESTORING = "restoring"


# Callback type aliases
StreamDroppedCallback = Callable[[str], None]  # reason
StreamRestoredCallback = Callable[[ChannelInfo], None]  # new channel info


@dataclass
class ManagedStreamStats:
    """Statistics for managed stream health and restoration"""
    state: StreamState = StreamState.STOPPED
    total_drops: int = 0
    total_restorations: int = 0
    last_drop_time: Optional[str] = None
    last_restore_time: Optional[str] = None
    last_drop_reason: Optional[str] = None
    current_healthy_duration_sec: float = 0.0
    total_healthy_duration_sec: float = 0.0
    total_dropped_duration_sec: float = 0.0
    
    def copy(self) -> 'ManagedStreamStats':
        """Return a copy of stats"""
        return ManagedStreamStats(
            state=self.state,
            total_drops=self.total_drops,
            total_restorations=self.total_restorations,
            last_drop_time=self.last_drop_time,
            last_restore_time=self.last_restore_time,
            last_drop_reason=self.last_drop_reason,
            current_healthy_duration_sec=self.current_healthy_duration_sec,
            total_healthy_duration_sec=self.total_healthy_duration_sec,
            total_dropped_duration_sec=self.total_dropped_duration_sec,
        )


class ManagedStream:
    """
    Self-healing RTP stream with automatic restoration.
    
    Monitors stream health and automatically restores the channel when
    the RTP stream drops (e.g., due to radiod restart).
    
    The stream is considered "dropped" when no packets are received for
    longer than the configured timeout. Upon detection, the class will:
    1. Notify via on_stream_dropped callback
    2. Attempt to restore the channel using ensure_channel()
    3. Restart the underlying RadiodStream
    4. Notify via on_stream_restored callback
    
    Sample delivery continues through disruptions, with gaps tracked
    in the StreamQuality metadata.
    """
    
    def __init__(
        self,
        control,  # RadiodControl instance
        frequency_hz: float,
        preset: str = "iq",
        sample_rate: int = 16000,
        agc_enable: int = 0,
        gain: float = 0.0,
        destination: Optional[str] = None,
        encoding: int = 0,
        on_samples: Optional[SampleCallback] = None,
        on_stream_dropped: Optional[StreamDroppedCallback] = None,
        on_stream_restored: Optional[StreamRestoredCallback] = None,
        drop_timeout_sec: float = 3.0,
        restore_interval_sec: float = 1.0,
        max_restore_attempts: int = 0,  # 0 = unlimited
        samples_per_packet: int = 320,
        resequence_buffer_size: int = 64,
        deliver_interval_packets: int = 10,
        raw_payloads: bool = False,
    ):
        """
        Initialize ManagedStream.

        Args:
            control: RadiodControl instance for channel management
            frequency_hz: Center frequency in Hz
            preset: Demodulation mode ("iq", "usb", "lsb", "am", "fm", "cw")
            sample_rate: Output sample rate in Hz
            agc_enable: Enable AGC (0=off, 1=on)
            gain: Manual gain in dB (when AGC off)
            destination: RTP destination multicast address (optional)
            encoding: Output encoding (0=none, 1=S16LE, 2=S16BE, 4=F32LE, etc.)
            on_samples: Callback(samples, quality) for sample delivery
            on_stream_dropped: Callback(reason) when stream drops
            on_stream_restored: Callback(channel) when stream is restored
            drop_timeout_sec: Seconds without packets before declaring drop (default: 3.0)
            restore_interval_sec: Seconds between restore attempts (default: 1.0)
            max_restore_attempts: Max restore attempts, 0=unlimited (default: 0)
            samples_per_packet: Expected samples per RTP packet
            resequence_buffer_size: Packets to buffer for resequencing
            raw_payloads: Deliver undecoded RTP payloads (List[bytes], one per
                packet) instead of samples — required for the framed encodings
                OPUS/OPUS_VOIP/AX25, which have no raw-sample representation.
                See :class:`ka9q.stream.RadiodStream` for the full contract.
            deliver_interval_packets: Deliver to callback every N packets
        """
        self._control = control
        self._frequency_hz = frequency_hz
        self._preset = preset
        self._sample_rate = sample_rate
        self._agc_enable = agc_enable
        self._gain = gain
        self._destination = destination
        self._encoding = encoding
        
        # Callbacks
        self._on_samples = on_samples
        self._on_stream_dropped = on_stream_dropped
        self._on_stream_restored = on_stream_restored
        
        # Health monitoring config
        self._drop_timeout_sec = drop_timeout_sec
        self._restore_interval_sec = restore_interval_sec
        self._max_restore_attempts = max_restore_attempts
        
        # RadiodStream config
        self._samples_per_packet = samples_per_packet
        self._resequence_buffer_size = resequence_buffer_size
        self._deliver_interval_packets = deliver_interval_packets
        self._raw_payloads = raw_payloads
        
        # State
        self._state = StreamState.STOPPED
        self._channel: Optional[ChannelInfo] = None
        self._stream: Optional[RadiodStream] = None
        self._running = False
        
        # Health monitoring
        self._last_packet_time: float = 0.0
        self._healthy_since: float = 0.0
        self._dropped_since: float = 0.0
        self._restore_attempts: int = 0
        
        # Threading
        self._monitor_thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        
        # Statistics
        self._stats = ManagedStreamStats()
        
        # Aggregate quality across stream restarts
        self._total_quality = StreamQuality()
    
    def start(self) -> ChannelInfo:
        """
        Start the managed stream.
        
        Establishes the channel and begins receiving samples.
        
        Returns:
            ChannelInfo for the established channel
            
        Raises:
            TimeoutError: If channel cannot be established
        """
        if self._running:
            logger.warning("ManagedStream already running")
            return self._channel
        
        logger.info(
            f"ManagedStream starting: {self._frequency_hz/1e6:.3f} MHz, "
            f"{self._preset}, {self._sample_rate} Hz"
        )
        
        self._state = StreamState.STARTING
        self._stats.state = StreamState.STARTING
        self._running = True
        
        # Establish initial channel
        self._channel = self._control.ensure_channel(
            frequency_hz=self._frequency_hz,
            preset=self._preset,
            sample_rate=self._sample_rate,
            agc_enable=self._agc_enable,
            gain=self._gain,
            destination=self._destination,
            encoding=self._encoding,
        )
        
        # Start the underlying stream
        self._start_stream()
        
        # Start health monitor
        self._monitor_thread = threading.Thread(
            target=self._health_monitor_loop,
            daemon=True,
            name="ManagedStream-Monitor"
        )
        self._monitor_thread.start()
        
        logger.info(
            f"ManagedStream started: SSRC={self._channel.ssrc}, "
            f"{self._channel.multicast_address}:{self._channel.port}"
        )
        
        return self._channel
    
    def stop(self) -> ManagedStreamStats:
        """
        Stop the managed stream.
        
        Returns:
            Final ManagedStreamStats with health statistics
        """
        if not self._running:
            return self._stats.copy()
        
        logger.info("ManagedStream stopping...")
        self._running = False
        
        # Stop monitor thread
        if self._monitor_thread:
            self._monitor_thread.join(timeout=5.0)
            self._monitor_thread = None
        
        # Stop underlying stream
        self._stop_stream()
        
        # Update final stats
        with self._lock:
            if self._state == StreamState.HEALTHY:
                self._stats.total_healthy_duration_sec += time.time() - self._healthy_since
            elif self._state == StreamState.DROPPED:
                self._stats.total_dropped_duration_sec += time.time() - self._dropped_since
            
            self._state = StreamState.STOPPED
            self._stats.state = StreamState.STOPPED
        
        logger.info(
            f"ManagedStream stopped. Drops: {self._stats.total_drops}, "
            f"Restorations: {self._stats.total_restorations}"
        )
        
        return self._stats.copy()
    
    def _start_stream(self):
        """Start the underlying RadiodStream."""
        if self._stream:
            self._stop_stream()
        
        self._stream = RadiodStream(
            channel=self._channel,
            on_samples=self._handle_samples,
            samples_per_packet=self._samples_per_packet,
            resequence_buffer_size=self._resequence_buffer_size,
            deliver_interval_packets=self._deliver_interval_packets,
            raw_payloads=self._raw_payloads,
        )
        self._stream.start()
        
        # Reset health tracking
        self._last_packet_time = time.time()
        self._healthy_since = time.time()
        self._restore_attempts = 0
        
        with self._lock:
            self._state = StreamState.HEALTHY
            self._stats.state = StreamState.HEALTHY
    
    def _stop_stream(self):
        """Stop the underlying RadiodStream."""
        if self._stream:
            quality = self._stream.stop()
            # Aggregate quality stats
            self._total_quality.rtp_packets_received += quality.rtp_packets_received
            self._total_quality.total_samples_delivered += quality.total_samples_delivered
            self._total_quality.total_gap_events += quality.total_gap_events
            self._stream = None
    
    def _handle_samples(self, samples: np.ndarray, quality: StreamQuality):
        """Handle samples from underlying stream, update health tracking."""
        # Update last packet time for health monitoring
        self._last_packet_time = time.time()
        
        # Forward to user callback
        if self._on_samples:
            try:
                self._on_samples(samples, quality)
            except Exception as e:
                logger.error(f"Error in sample callback: {e}", exc_info=True)
    
    def _health_monitor_loop(self):
        """Monitor stream health and trigger restoration when needed."""
        check_interval = min(0.5, self._drop_timeout_sec / 4)
        
        while self._running:
            time.sleep(check_interval)
            
            if not self._running:
                break
            
            with self._lock:
                current_state = self._state
            
            if current_state == StreamState.HEALTHY:
                # Check for drop
                time_since_packet = time.time() - self._last_packet_time
                
                if time_since_packet > self._drop_timeout_sec:
                    self._handle_stream_drop(
                        f"No packets for {time_since_packet:.1f}s "
                        f"(timeout: {self._drop_timeout_sec}s)"
                    )
            
            elif current_state == StreamState.DROPPED:
                # Attempt restoration
                self._attempt_restore()
    
    def _handle_stream_drop(self, reason: str):
        """Handle detected stream drop."""
        logger.warning(f"Stream drop detected: {reason}")
        
        with self._lock:
            # Update stats
            if self._state == StreamState.HEALTHY:
                self._stats.total_healthy_duration_sec += time.time() - self._healthy_since
            
            self._state = StreamState.DROPPED
            self._stats.state = StreamState.DROPPED
            self._stats.total_drops += 1
            self._stats.last_drop_time = datetime.now(timezone.utc).isoformat()
            self._stats.last_drop_reason = reason
            self._dropped_since = time.time()
            self._restore_attempts = 0
        
        # Stop current stream
        self._stop_stream()
        
        # Notify callback
        if self._on_stream_dropped:
            try:
                self._on_stream_dropped(reason)
            except Exception as e:
                logger.error(f"Error in stream_dropped callback: {e}", exc_info=True)
    
    def _attempt_restore(self):
        """Attempt to restore the stream."""
        # Check max attempts
        if self._max_restore_attempts > 0 and self._restore_attempts >= self._max_restore_attempts:
            logger.error(
                f"Max restore attempts ({self._max_restore_attempts}) reached, giving up"
            )
            return
        
        self._restore_attempts += 1
        logger.info(f"Attempting stream restoration (attempt {self._restore_attempts})")
        
        with self._lock:
            self._state = StreamState.RESTORING
            self._stats.state = StreamState.RESTORING
        
        try:
            # Re-establish channel via ensure_channel
            self._channel = self._control.ensure_channel(
                frequency_hz=self._frequency_hz,
                preset=self._preset,
                sample_rate=self._sample_rate,
                agc_enable=self._agc_enable,
                gain=self._gain,
                destination=self._destination,
                encoding=self._encoding,
                timeout=self._restore_interval_sec * 2,  # Give it some time
            )
            
            # Restart stream
            self._start_stream()
            
            # Update stats
            with self._lock:
                self._stats.total_restorations += 1
                self._stats.last_restore_time = datetime.now(timezone.utc).isoformat()
                self._stats.total_dropped_duration_sec += time.time() - self._dropped_since
            
            logger.info(
                f"Stream restored: SSRC={self._channel.ssrc}, "
                f"{self._channel.frequency/1e6:.3f} MHz"
            )
            
            # Notify callback
            if self._on_stream_restored:
                try:
                    self._on_stream_restored(self._channel)
                except Exception as e:
                    logger.error(f"Error in stream_restored callback: {e}", exc_info=True)
        
        except TimeoutError as e:
            logger.warning(f"Restore attempt {self._restore_attempts} failed: {e}")
            with self._lock:
                self._state = StreamState.DROPPED
                self._stats.state = StreamState.DROPPED
            
            # Wait before next attempt
            time.sleep(self._restore_interval_sec)
        
        except Exception as e:
            logger.error(f"Restore attempt {self._restore_attempts} error: {e}", exc_info=True)
            with self._lock:
                self._state = StreamState.DROPPED
                self._stats.state = StreamState.DROPPED
            
            # Wait before next attempt
            time.sleep(self._restore_interval_sec)
    
    @property
    def state(self) -> StreamState:
        """Current stream state."""
        with self._lock:
            return self._state
    
    @property
    def channel(self) -> Optional[ChannelInfo]:
        """Current channel info (may change after restoration)."""
        return self._channel
    
    @property
    def is_healthy(self) -> bool:
        """True if stream is currently healthy and receiving packets."""
        with self._lock:
            return self._state == StreamState.HEALTHY
    
    def get_stats(self) -> ManagedStreamStats:
        """Get current health statistics (copy)."""
        with self._lock:
            stats = self._stats.copy()
            
            # Update current duration
            if self._state == StreamState.HEALTHY:
                stats.current_healthy_duration_sec = time.time() - self._healthy_since
            
            return stats
    
    def get_quality(self) -> StreamQuality:
        """Get current stream quality metrics."""
        if self._stream:
            return self._stream.get_quality()
        return self._total_quality.copy()
    
    def __enter__(self):
        """Context manager entry."""
        self.start()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.stop()
        return False
    
    def __del__(self):
        """Ensure stream is stopped on garbage collection."""
        try:
            self.stop()
        except Exception:
            pass
