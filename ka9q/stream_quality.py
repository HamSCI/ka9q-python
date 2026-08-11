"""
Stream Quality Metadata for ka9q-python

Provides data structures for tracking stream quality and gap information.
This metadata accompanies sample streams delivered to applications.

Gap Types (detected by ka9q-python core):
- NETWORK_LOSS: RTP packets lost in transit (sequence gap)
- RESEQUENCE_TIMEOUT: Packet arrived too late to resequence
- EMPTY_PAYLOAD: RTP packet received with no data
- STREAM_START: Gap at beginning before first packet
- STREAM_INTERRUPTION: radiod stopped sending packets

Applications may add their own gap types (e.g., cadence_fill, late_start)
when they perform segmentation.
"""

from dataclasses import dataclass, field
from typing import List, Optional
from enum import Enum


class GapSource(Enum):
    """
    Gap types detected by ka9q-python core layer.
    
    Applications may define additional gap types for app-level gaps
    (e.g., segment boundary padding, late start, tone lock wait).
    """
    NETWORK_LOSS = "network_loss"
    """RTP sequence gap - packets lost in transit"""
    
    RESEQUENCE_TIMEOUT = "resequence_timeout"
    """Packet arrived after resequence window expired"""
    
    EMPTY_PAYLOAD = "empty_payload"
    """RTP packet received but payload was empty/zeros"""
    
    STREAM_START = "stream_start"
    """Initial gap before first packet received"""
    
    STREAM_INTERRUPTION = "stream_interruption"
    """Extended silence - radiod may have stopped sending"""


@dataclass
class GapEvent:
    """
    Single gap detected in the sample stream.
    
    Position is relative to stream start (cumulative sample count).
    Applications can convert to segment-relative positions as needed.
    """
    source: GapSource
    """What caused this gap"""
    
    position_samples: int
    """Offset from stream start (cumulative sample count)"""
    
    duration_samples: int
    """Gap size in samples (zeros inserted)"""
    
    timestamp_utc: str
    """ISO format timestamp when gap was detected"""
    
    packets_affected: int = 0
    """Number of packets involved (for NETWORK_LOSS)"""
    
    def to_dict(self) -> dict:
        """Serialize for JSON/storage"""
        return {
            'source': self.source.value,
            'position_samples': self.position_samples,
            'duration_samples': self.duration_samples,
            'timestamp_utc': self.timestamp_utc,
            'packets_affected': self.packets_affected,
        }


@dataclass
class StreamQuality:
    """
    Quality metadata for a batch of samples delivered to application.
    
    Includes both per-batch and cumulative statistics.
    Applications receive this with every sample callback.
    """
    
    # === Per-batch info ===
    batch_start_sample: int = 0
    """Position of first sample in this batch (cumulative from stream start)"""
    
    batch_samples_delivered: int = 0
    """Number of samples in this batch"""
    
    batch_gaps: List[GapEvent] = field(default_factory=list)
    """Gaps detected in this batch"""
    
    # === Cumulative (stream lifetime) ===
    total_samples_delivered: int = 0
    """Total samples delivered since stream start"""
    
    total_samples_expected: int = 0
    """Total samples expected based on elapsed time"""
    
    total_gaps_filled: int = 0
    """Total zero-fill samples inserted for gaps"""
    
    total_gap_events: int = 0
    """Number of distinct gap events"""
    
    # === RTP statistics ===
    rtp_packets_received: int = 0
    """Packets received from multicast"""
    
    rtp_packets_expected: int = 0
    """Packets expected based on sequence numbers"""
    
    rtp_packets_lost: int = 0
    """Packets never received (sequence gaps)"""
    
    rtp_packets_late: int = 0
    """Packets that arrived after resequence window"""
    
    rtp_packets_duplicate: int = 0
    """Duplicate packets (same sequence number)"""
    
    rtp_packets_resequenced: int = 0
    """Packets that arrived out of order but were resequenced"""
    
    # === Timing ===
    stream_start_utc: str = ""
    """ISO format timestamp when stream started"""
    
    last_packet_utc: str = ""
    """ISO format timestamp of most recent packet"""
    
    # === RTP timing (for applications needing precise timing) ===
    first_rtp_timestamp: int = 0
    """RTP timestamp of first packet received"""
    
    last_rtp_timestamp: int = 0
    
    # RTP timestamp of the FIRST sample of the delivered batch, from the
    # resequencer's emission tracking (gap fills included) — the truthful
    # label for the resequenced stream.  None when the producer predates
    # this field or nothing has been delivered.  Prefer this over
    # last_rtp_timestamp for sample labelling: the latter is the last
    # RECEIVED packet's header and desynchronizes from delivered samples
    # under loss.
    delivered_rtp_start: object = None
    """RTP timestamp of most recent packet"""
    
    sample_rate: int = 0
    """Sample rate in Hz (for RTP timestamp conversion)"""
    
    @property
    def completeness_pct(self) -> float:
        """Percentage of expected samples that were actually received (not gap-filled)"""
        if self.total_samples_expected == 0:
            return 100.0
        actual = self.total_samples_delivered - self.total_gaps_filled
        return min(100.0, (actual / self.total_samples_expected) * 100)
    
    @property
    def has_gaps(self) -> bool:
        """True if any gaps have been detected"""
        return self.total_gap_events > 0
    
    def to_dict(self) -> dict:
        """Serialize for JSON/storage"""
        return {
            # Batch
            'batch_start_sample': self.batch_start_sample,
            'batch_samples_delivered': self.batch_samples_delivered,
            'batch_gaps': [g.to_dict() for g in self.batch_gaps],
            
            # Cumulative
            'total_samples_delivered': self.total_samples_delivered,
            'total_samples_expected': self.total_samples_expected,
            'total_gaps_filled': self.total_gaps_filled,
            'total_gap_events': self.total_gap_events,
            'completeness_pct': self.completeness_pct,
            
            # RTP stats
            'rtp_packets_received': self.rtp_packets_received,
            'rtp_packets_expected': self.rtp_packets_expected,
            'rtp_packets_lost': self.rtp_packets_lost,
            'rtp_packets_late': self.rtp_packets_late,
            'rtp_packets_duplicate': self.rtp_packets_duplicate,
            'rtp_packets_resequenced': self.rtp_packets_resequenced,
            
            # Timing
            'stream_start_utc': self.stream_start_utc,
            'last_packet_utc': self.last_packet_utc,
            
            # RTP timing
            'first_rtp_timestamp': self.first_rtp_timestamp,
            'last_rtp_timestamp': self.last_rtp_timestamp,
            'sample_rate': self.sample_rate,
        }
    
    def copy(self) -> 'StreamQuality':
        """Create a copy (for passing to callbacks without mutation issues)"""
        return StreamQuality(
            batch_start_sample=self.batch_start_sample,
            batch_samples_delivered=self.batch_samples_delivered,
            batch_gaps=list(self.batch_gaps),
            total_samples_delivered=self.total_samples_delivered,
            total_samples_expected=self.total_samples_expected,
            total_gaps_filled=self.total_gaps_filled,
            total_gap_events=self.total_gap_events,
            rtp_packets_received=self.rtp_packets_received,
            rtp_packets_expected=self.rtp_packets_expected,
            rtp_packets_lost=self.rtp_packets_lost,
            rtp_packets_late=self.rtp_packets_late,
            rtp_packets_duplicate=self.rtp_packets_duplicate,
            rtp_packets_resequenced=self.rtp_packets_resequenced,
            stream_start_utc=self.stream_start_utc,
            last_packet_utc=self.last_packet_utc,
            first_rtp_timestamp=self.first_rtp_timestamp,
            last_rtp_timestamp=self.last_rtp_timestamp,
            sample_rate=self.sample_rate,
        )
