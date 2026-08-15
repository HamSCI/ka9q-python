"""
RTP Packet Resequencer with Gap Detection

Handles out-of-order packet delivery and maintains continuous sample streams.
Uses KA9Q timing architecture: RTP timestamps are the primary reference.

Key behaviors:
- Circular buffer for resequencing jittered packets
- Detects gaps via RTP timestamp jumps
- Fills gaps with zeros to maintain sample count integrity
- Tracks quality metrics for downstream applications
- Handles fragmented IQ packets (uses actual packet sample count for timestamp tracking)

Fragmented IQ support:
  radiod fragments large IQ packets to fit within UDP MTU limits.
  Each fragment has a timestamp that reflects the samples it contains.
  For example, IQ 16kHz F32 (320 samples/20ms = 2560 bytes) fragments into:
    - Fragment 1: 1440 bytes = 180 samples, ts_inc=180
    - Fragment 2: 1120 bytes = 140 samples, ts_inc=140
  The resequencer uses actual packet sample count (not nominal samples_per_packet)
  for timestamp tracking to correctly handle these fragmented streams.

Design principle: Sample count integrity > real-time delivery
"""

import numpy as np
import logging
from collections import deque
from datetime import datetime, timezone
from typing import Optional, Tuple, List
from dataclasses import dataclass

from .stream_quality import GapSource, GapEvent

logger = logging.getLogger(__name__)


@dataclass
class RTPPacket:
    """Parsed RTP packet with samples"""
    sequence: int           # RTP sequence number (16-bit, wraps)
    timestamp: int          # RTP timestamp (32-bit, wraps)
    ssrc: int               # RTP SSRC identifier
    samples: np.ndarray     # IQ samples (complex64 or float32)
    wallclock: Optional[float] = None  # Unix timestamp if available


@dataclass
class ResequencerStats:
    """Statistics from resequencer operation"""
    packets_received: int = 0
    packets_resequenced: int = 0
    packets_duplicate: int = 0
    gaps_detected: int = 0
    samples_output: int = 0
    samples_filled: int = 0
    
    def to_dict(self) -> dict:
        return {
            'packets_received': self.packets_received,
            'packets_resequenced': self.packets_resequenced,
            'packets_duplicate': self.packets_duplicate,
            'gaps_detected': self.gaps_detected,
            'samples_output': self.samples_output,
            'samples_filled': self.samples_filled,
        }


class PacketResequencer:
    """
    Resequence out-of-order RTP packets and detect gaps.
    
    Design:
    - Circular buffer of N packets (handles network jitter)
    - Process packets in sequence order
    - Detect gaps via RTP timestamp jumps
    - Fill gaps with zeros to maintain sample count integrity
    
    Usage:
        reseq = PacketResequencer(buffer_size=64, samples_per_packet=320)
        
        # For each received packet:
        samples, gap_events = reseq.process_packet(packet)
        if samples is not None:
            # Deliver to application
            app.on_samples(samples, gap_events)
    """
    
    # Maximum gap to fill: 60 seconds at 16 kHz = 960,000 samples
    # Larger gaps indicate stream restart or corruption
    MAX_GAP_SAMPLES = 960_000
    
    def __init__(
        self,
        buffer_size: int = 64,
        samples_per_packet: int = 320,
        sample_rate: int = 16000,
    ):
        """
        Initialize resequencer.
        
        Args:
            buffer_size: Circular buffer size (packets). 64 handles ~2s jitter @ 320 samples/packet
            samples_per_packet: Expected samples per RTP packet
            sample_rate: Sample rate in Hz (for gap duration calculations)
        """
        self.buffer_size = buffer_size
        self.samples_per_packet = samples_per_packet
        self.sample_rate = sample_rate
        
        # Circular buffer: sequence_num -> packet
        self.buffer: deque = deque(maxlen=buffer_size)
        # RTP timestamp of the first sample of the most recent emitted
        # chunk (None until the first emission).  See _try_output.
        self.last_chunk_rtp_start = None
        self.buffer_seq_nums: set = set()
        
        # State tracking
        self.initialized = False
        self.next_expected_seq: Optional[int] = None
        self.next_expected_ts: Optional[int] = None
        self.cumulative_samples: int = 0  # Total samples output
        
        # Statistics
        self.stats = ResequencerStats()
        
        logger.debug(
            f"PacketResequencer: buffer={buffer_size}, "
            f"samples/pkt={samples_per_packet}, rate={sample_rate}"
        )
    
    def process_packet(
        self,
        packet: RTPPacket
    ) -> Tuple[Optional[np.ndarray], List[GapEvent]]:
        """
        Process incoming RTP packet.
        
        Args:
            packet: Parsed RTP packet with samples
        
        Returns:
            (samples, gap_events):
            - samples: Continuous sample array ready for output (may include gap fills)
            - gap_events: List of gaps detected (empty if none)
        """
        self.stats.packets_received += 1
        
        # Initialize on first packet
        if not self.initialized:
            self._initialize(packet)
            return None, []
        
        # Check for duplicate
        if packet.sequence in self.buffer_seq_nums:
            logger.debug(f"Duplicate packet seq={packet.sequence}")
            self.stats.packets_duplicate += 1
            return None, []
        
        # radiod zero-fills a dropped output block, so this packet may
        # carry synthetic samples with a perfectly contiguous timestamp.
        # Detect it here, before the resequencer -- which by definition
        # cannot see it, there being no timestamp gap to find.
        zero_gap = self._detect_zero_block(packet)

        # Add to buffer
        self._add_to_buffer(packet)
        
        # Try to output packets in sequence order
        samples, gaps = self._try_output()
        if zero_gap is not None:
            gaps = list(gaps) + [zero_gap]
        return samples, gaps

    def _detect_zero_block(self, packet: RTPPacket) -> Optional[GapEvent]:
        """A packet of nothing but exact zeros is radiod's drop marker.

        Cheap on the hot path: real samples almost always have a
        non-zero first element, so the full scan is only reached for
        packets that are already suspect.
        """
        samples = packet.samples
        if samples is None or len(samples) == 0:
            return None
        if samples[0] != 0 or samples.any():
            return None
        n = len(samples)
        self.stats.samples_filled += n
        logger.warning(
            f"radiod block drop: {n} zero samples "
            f"({n / self.sample_rate * 1000:.1f} ms) at seq={packet.sequence} "
            f"-- synthetic, not received signal"
        )
        return GapEvent(
            source=GapSource.RADIOD_BLOCK_DROP,
            position_samples=self.cumulative_samples,
            duration_samples=n,
            timestamp_utc=datetime.now(timezone.utc).isoformat(),
            packets_affected=1,
        )
    
    def _initialize(self, packet: RTPPacket):
        """Initialize sequencer with first packet"""
        self.next_expected_seq = packet.sequence
        self.next_expected_ts = packet.timestamp
        self._add_to_buffer(packet)
        self.initialized = True
        logger.info(f"Resequencer initialized: seq={packet.sequence}, ts={packet.timestamp}")
    
    def _add_to_buffer(self, packet: RTPPacket):
        """Add packet to circular buffer"""
        self.buffer.append(packet)
        self.buffer_seq_nums.add(packet.sequence)
        
        # If buffer full, oldest automatically removed by deque
        while len(self.buffer_seq_nums) > len(self.buffer):
            # Sync set with deque contents
            actual_seqs = {p.sequence for p in self.buffer}
            self.buffer_seq_nums = actual_seqs
    
    def _try_output(self) -> Tuple[Optional[np.ndarray], List[GapEvent]]:
        """Try to output next packet(s) in sequence"""
        output_samples = []
        gap_events = []
        # True RTP timestamp of the first sample this call will emit (gap
        # fills included: a fill starts exactly at next_expected_ts).
        # Published via last_chunk_rtp_start so consumers can label the
        # RESEQUENCED stream truthfully — labelling with the last RECEIVED
        # packet's header desynchronizes labels from delivered samples
        # under loss (hf-timestd T6 origin slips, 2026-08-11).
        chunk_start_ts = self.next_expected_ts
        
        while True:
            # Look for next expected sequence number
            next_pkt = None
            for pkt in self.buffer:
                if pkt.sequence == self.next_expected_seq:
                    next_pkt = pkt
                    break
            
            if next_pkt is None:
                # Packet not in buffer - check if we should skip ahead
                if len(self.buffer) >= self.buffer_size // 2:
                    # Buffer filling up - packet probably lost
                    samples, gaps = self._handle_lost_packet()
                    if samples is not None:
                        output_samples.append(samples)
                        gap_events.extend(gaps)
                        continue  # Try to output more
                break  # Keep waiting
            
            # Found next packet - check for timestamp gap
            if next_pkt.timestamp != self.next_expected_ts:
                gap = self._detect_gap(next_pkt)
                if gap is not None:
                    gap_events.append(gap)
                    # Insert gap fill
                    gap_fill = np.zeros(gap.duration_samples, dtype=next_pkt.samples.dtype)
                    output_samples.append(gap_fill)
                    self.stats.samples_filled += gap.duration_samples
            
            # Remove from buffer and output
            self.buffer.remove(next_pkt)
            self.buffer_seq_nums.discard(next_pkt.sequence)
            output_samples.append(next_pkt.samples)
            
            # Update state
            # Use actual packet sample count for timestamp tracking
            # This handles fragmented IQ packets where each fragment has fewer
            # samples than the nominal samples_per_packet
            actual_samples = len(next_pkt.samples)
            self.next_expected_seq = (next_pkt.sequence + 1) & 0xFFFF
            self.next_expected_ts = next_pkt.timestamp + actual_samples
        
        # Combine output
        if output_samples:
            combined = np.concatenate(output_samples)
            self.stats.samples_output += len(combined)
            self.cumulative_samples += len(combined)
            self.last_chunk_rtp_start = chunk_start_ts & 0xFFFFFFFF
            return combined, gap_events
        
        return None, []
    
    def _detect_gap(self, next_pkt: RTPPacket) -> Optional[GapEvent]:
        """
        Detect gap using KA9Q signed 32-bit arithmetic technique.
        
        This handles RTP timestamp wraps naturally:
        - Positive difference = forward gap (fill with zeros)
        - Negative difference = backward jump (ignore, likely reorder)
        """
        # Signed 32-bit difference (Phil Karn's technique)
        ts_diff = (next_pkt.timestamp - self.next_expected_ts) & 0xFFFFFFFF
        if ts_diff >= 0x80000000:
            ts_gap = ts_diff - 0x100000000  # Negative
        else:
            ts_gap = ts_diff
        
        # Only fill forward gaps
        if ts_gap <= 0:
            if ts_gap < 0:
                logger.debug(f"Backward timestamp jump: {ts_gap} samples (reorder?)")
            return None
        
        # Cap absurdly large gaps
        if ts_gap > self.MAX_GAP_SAMPLES:
            logger.warning(f"Capping gap from {ts_gap} to {self.MAX_GAP_SAMPLES}")
            ts_gap = self.MAX_GAP_SAMPLES
        
        self.stats.gaps_detected += 1
        
        # Estimate packets lost
        packets_lost = ts_gap // self.samples_per_packet
        
        logger.warning(
            f"Gap: {ts_gap} samples ({ts_gap / self.sample_rate * 1000:.1f}ms), "
            f"~{packets_lost} packets"
        )
        
        return GapEvent(
            source=GapSource.NETWORK_LOSS,
            position_samples=self.cumulative_samples,
            duration_samples=ts_gap,
            timestamp_utc=datetime.now(timezone.utc).isoformat(),
            packets_affected=packets_lost,
        )
    
    def _handle_lost_packet(self) -> Tuple[Optional[np.ndarray], List[GapEvent]]:
        """Handle case where expected packet is definitely lost"""
        if len(self.buffer) == 0:
            return None, []
        
        # Find earliest packet by sequence
        earliest = min(
            self.buffer,
            key=lambda p: self._seq_distance(self.next_expected_seq, p.sequence)
        )
        
        # Calculate gap
        ts_diff = (earliest.timestamp - self.next_expected_ts) & 0xFFFFFFFF
        if ts_diff >= 0x80000000:
            ts_gap = ts_diff - 0x100000000
        else:
            ts_gap = ts_diff
        
        gap_events = []
        output_samples = []
        
        # Create gap fill if forward gap
        if ts_gap > 0:
            if ts_gap > self.MAX_GAP_SAMPLES:
                ts_gap = self.MAX_GAP_SAMPLES
            
            self.stats.gaps_detected += 1
            packets_lost = (earliest.sequence - self.next_expected_seq) & 0xFFFF
            
            gap = GapEvent(
                source=GapSource.NETWORK_LOSS,
                position_samples=self.cumulative_samples,
                duration_samples=ts_gap,
                timestamp_utc=datetime.now(timezone.utc).isoformat(),
                packets_affected=packets_lost,
            )
            gap_events.append(gap)
            
            gap_fill = np.zeros(ts_gap, dtype=earliest.samples.dtype)
            output_samples.append(gap_fill)
            self.stats.samples_filled += ts_gap
            
            logger.warning(
                f"Lost packet recovery: skip to seq={earliest.sequence}, "
                f"gap={ts_gap} samples"
            )
        
        # Remove and output the packet
        self.buffer.remove(earliest)
        self.buffer_seq_nums.discard(earliest.sequence)
        output_samples.append(earliest.samples)
        
        # Update state - use actual packet sample count for fragmented packets
        actual_samples = len(earliest.samples)
        self.next_expected_seq = (earliest.sequence + 1) & 0xFFFF
        self.next_expected_ts = earliest.timestamp + actual_samples
        self.stats.packets_resequenced += 1
        
        combined = np.concatenate(output_samples) if output_samples else None
        if combined is not None:
            self.stats.samples_output += len(combined)
            self.cumulative_samples += len(combined)
        
        return combined, gap_events
    
    def _seq_distance(self, from_seq: int, to_seq: int) -> int:
        """Calculate forward distance between sequence numbers (handles wrap)"""
        dist = (to_seq - from_seq) & 0xFFFF
        return dist if dist < 32768 else dist - 65536
    
    def flush(self) -> Tuple[np.ndarray, List[GapEvent]]:
        """
        Flush remaining packets in buffer (for shutdown).
        
        Returns all buffered samples in sequence order with any gaps filled.
        """
        output_samples = []
        gap_events = []
        
        # Sort by sequence
        sorted_pkts = sorted(self.buffer, key=lambda p: p.sequence)
        
        for pkt in sorted_pkts:
            # Check for gap
            if pkt.timestamp != self.next_expected_ts:
                gap = self._detect_gap(pkt)
                if gap is not None:
                    gap_events.append(gap)
                    gap_fill = np.zeros(gap.duration_samples, dtype=pkt.samples.dtype)
                    output_samples.append(gap_fill)
            
            output_samples.append(pkt.samples)
            # Use actual packet sample count for fragmented packets
            self.next_expected_ts = pkt.timestamp + len(pkt.samples)
        
        # Clear buffer
        self.buffer.clear()
        self.buffer_seq_nums.clear()
        
        if output_samples:
            combined = np.concatenate(output_samples)
            self.stats.samples_output += len(combined)
            self.cumulative_samples += len(combined)
            return combined, gap_events
        
        return np.array([], dtype=np.complex64), gap_events
    
    def get_stats(self) -> dict:
        """Get resequencer statistics"""
        stats = self.stats.to_dict()
        stats['buffer_used'] = len(self.buffer)
        stats['buffer_size'] = self.buffer_size
        stats['cumulative_samples'] = self.cumulative_samples
        return stats
    
    def reset(self):
        """Reset resequencer state (for stream restart)"""
        self.buffer.clear()
        self.buffer_seq_nums.clear()
        self.initialized = False
        self.next_expected_seq = None
        self.next_expected_ts = None
        self.cumulative_samples = 0
        self.stats = ResequencerStats()
