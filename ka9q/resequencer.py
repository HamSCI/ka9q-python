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
import time
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
    packets_late: int = 0       # arrived behind next_expected_seq; dropped
    gaps_detected: int = 0
    interruptions: int = 0      # STREAM_INTERRUPTION events (nothing filled)
    samples_output: int = 0
    samples_filled: int = 0
    
    def to_dict(self) -> dict:
        return {
            'packets_received': self.packets_received,
            'packets_resequenced': self.packets_resequenced,
            'packets_duplicate': self.packets_duplicate,
            'packets_late': self.packets_late,
            'gaps_detected': self.gaps_detected,
            'interruptions': self.interruptions,
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
    
    # Gaps up to this many samples are always zero-filled at their true
    # length (60 s at 16 kHz).  Kept as the floor of max_fill_samples so
    # no gap that was filled before is treated differently now.
    MAX_GAP_SAMPLES = 960_000

    # Gaps up to this long are zero-filled at their TRUE length, so that
    # first_rtp_timestamp + sample index stays the true RTP of every
    # delivered sample.  A longer forward jump (an RTP clock re-based by a
    # radiod restart, or an outage longer than this) is not filled: the
    # stream reports a STREAM_INTERRUPTION and the next chunk starts at the
    # post-gap packet's own RTP.  The bound exists to cap the fill's memory
    # (120 s of complex64 at 64 kHz is about 61 MB).
    DEFAULT_MAX_FILL_SECONDS = 120.0

    # Absolute memory ceiling on one zero fill, whatever the sample rate:
    # the fill holds at most this many bytes of the packet's dtype.  At
    # 192 kS/s complex64 that comes to about 44 s.  A jump past it breaks
    # continuity (STREAM_INTERRUPTION) instead of filling.
    DEFAULT_FILL_BYTE_BUDGET = 64 * 1024 * 1024

    # A packet up to buffer_size sequence numbers behind next_expected_seq
    # counts as late (a duplicate, or a straggler recovery already skipped)
    # and gets dropped.  One further behind may belong to a restarted
    # sequence: radiod re-creates a channel from its template, so the
    # sequence starts again from the template's value.  After this many
    # such packets in sequence with each other, the resequencer re-syncs
    # to the new run (STREAM_INTERRUPTION).  A lone stale or forged packet
    # only gets dropped.
    RESYNC_PACKETS = 4
    LINE_ANCHOR_SPAN = 8192
    # A run counts as stragglers only if it lies within this many
    # buffer_size behind next_expected_seq, every sequence number in it is
    # one recovery zero-filled recently (the last FILLED_SEQ_MEMORY), and
    # its timestamps fit the stream's RTP line.  radiod presets a
    # re-created channel's RTP in whole blocks from its uptime, so a
    # restart can land exactly on the old line; a packet recovery never
    # skipped cannot be a straggler, whatever its timestamp.
    STRAGGLER_WINDOW_BUFFERS = 4
    FILLED_SEQ_MEMORY = 256

    # Interruption ERRORs: at most one per this many seconds; the rest go
    # to DEBUG and get counted in the next ERROR.
    INTERRUPTION_LOG_INTERVAL_SEC = 10.0

    def __init__(
        self,
        buffer_size: int = 64,
        samples_per_packet: int = 320,
        sample_rate: int = 16000,
        max_fill_seconds: Optional[float] = None,
        fill_byte_budget: Optional[int] = None,
    ):
        """
        Initialize resequencer.
        
        Args:
            buffer_size: Circular buffer size (packets). 64 handles ~2s jitter @ 320 samples/packet
            samples_per_packet: Expected samples per RTP packet
            sample_rate: Sample rate in Hz (for gap duration calculations)
            max_fill_seconds: Longest gap zero-filled at its true length
                (default DEFAULT_MAX_FILL_SECONDS; never less than
                MAX_GAP_SAMPLES samples).  Longer gaps break continuity
                with a STREAM_INTERRUPTION event instead.
            fill_byte_budget: Most bytes one zero fill may hold (default
                DEFAULT_FILL_BYTE_BUDGET).  Caps the fill below
                max_fill_seconds at high sample rates.
        """
        self.buffer_size = buffer_size
        self.samples_per_packet = samples_per_packet
        self.sample_rate = sample_rate
        if max_fill_seconds is None:
            max_fill_seconds = self.DEFAULT_MAX_FILL_SECONDS
        self.max_fill_samples = max(
            self.MAX_GAP_SAMPLES, int(max_fill_seconds * sample_rate))
        self.fill_byte_budget = (self.DEFAULT_FILL_BYTE_BUDGET
                                 if fill_byte_budget is None
                                 else int(fill_byte_budget))
        
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

        # Sequence-reset detection (see RESYNC_PACKETS)
        self._resync_run: List[RTPPacket] = []
        self._resync_pending: Optional[List[RTPPacket]] = None
        self._interrupt_next = False
        # (seq, ts) of a packet on the current stream's RTP line, for
        # telling stragglers from a restarted sequence.  Moves at every
        # break and every LINE_ANCHOR_SPAN packets.
        self._line_anchor: Optional[Tuple[int, int]] = None
        # Sequence numbers recovery skipped (zero-filled) since the last
        # break; see STRAGGLER_WINDOW_BUFFERS.
        self._filled_seqs: deque = deque(maxlen=self.FILLED_SEQ_MEMORY)
        self._last_interrupt_log = None
        self._interrupt_logs_suppressed = 0
        
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
            - gap_events: Gaps inside `samples` (empty if none).  Each
              event's position_samples is its offset in the emitted
              stream (cumulative from stream start).
        """
        self.stats.packets_received += 1
        
        # Initialize on first packet
        if not self.initialized:
            self._initialize(packet)
            return None, []

        if self._resync_pending is not None:
            self._apply_resync()
        
        # Check for duplicate
        if packet.sequence in self.buffer_seq_nums:
            logger.debug(f"Duplicate packet seq={packet.sequence}")
            self.stats.packets_duplicate += 1
            return None, []

        # A packet behind next_expected_seq has been emitted already, or
        # skipped by recovery: a duplicate of an emitted packet, or one more
        # than buffer_size/2 late.  Letting it in would move the stream
        # backward (re-emitting old samples, then filling zeros for time
        # already delivered), so drop it.  Far behind, it may instead open
        # a restarted sequence (see RESYNC_PACKETS).
        behind = -self._seq_distance(self.next_expected_seq, packet.sequence)
        if behind > 0:
            self.stats.packets_late += 1
            if behind <= self.buffer_size:
                logger.debug(f"Late packet seq={packet.sequence} dropped "
                             f"(expecting {self.next_expected_seq})")
                return None, []
            return self._resync_candidate(packet)
        self._resync_run = []
        
        # Add to buffer
        self._add_to_buffer(packet)
        
        # Try to output packets in sequence order
        return self._try_output()

    def _resync_candidate(
        self, packet: RTPPacket
    ) -> Tuple[Optional[np.ndarray], List[GapEvent]]:
        """A packet far behind next_expected_seq.  Collect a run of them in
        sequence with each other; at RESYNC_PACKETS, re-sync to the run."""
        run = self._resync_run
        if run and packet.sequence == (run[-1].sequence + 1) & 0xFFFF:
            run.append(packet)
        else:
            self._resync_run = run = [packet]
        if len(run) < self.RESYNC_PACKETS:
            logger.debug(f"Far-behind packet seq={packet.sequence} held as "
                         f"re-sync candidate {len(run)}/{self.RESYNC_PACKETS}")
            return None, []
        if self._run_is_stragglers(run):
            # Real stragglers, more than buffer_size late: recovery has
            # already filled their span.  Re-syncing would deliver that
            # time twice.  They stay counted as late.
            logger.debug(f"{len(run)} far-behind packets from "
                         f"seq={run[0].sequence} fit the stream's RTP line: "
                         f"stragglers, dropped")
            self._resync_run = []
            return None, []
        # The run's packets were counted as late; they are not.
        self.stats.packets_late -= len(run)
        self._resync_run = []
        logger.warning(
            f"RTP sequence restarted: {len(run)} packets in sequence from "
            f"seq={run[0].sequence}, expected {self.next_expected_seq}; "
            f"re-syncing (radiod restart or channel re-created)")
        self._resync_pending = run
        if not self.buffer:
            self._apply_resync()
            return self._try_output()
        # Emit the old stream's pending packets first, as their own chunk.
        # The re-sync applies on the next call (or flush).
        out, gaps = self._try_output(force=True)
        if self.buffer:
            logger.warning(
                f"re-sync: {len(self.buffer)} old packet(s) wait past an "
                f"interruption; the re-sync drops them on the next packet, "
                f"or flush() emits them first at stop")
        if out is None:
            self._apply_resync()
            return self._try_output()
        return out, gaps

    def _run_is_stragglers(self, run: List[RTPPacket]) -> bool:
        """Real stragglers: close behind, skipped by recovery, on the line."""
        behind = -self._seq_distance(self.next_expected_seq, run[0].sequence)
        if behind > self.STRAGGLER_WINDOW_BUFFERS * self.buffer_size:
            return False
        filled = set(self._filled_seqs)
        if any(p.sequence not in filled for p in run):
            return False
        return self._run_fits_old_line(run)

    def _run_fits_old_line(self, run: List[RTPPacket]) -> bool:
        """True when the run's timestamps sit on the current stream's RTP
        line: ts - next_expected_ts matches (seq - next_expected_seq) times
        the stream's RTP step per packet, within one packet.  The step comes
        from the line anchor (an average over many packets, so fragmented
        streams fit too), or from the run itself while the anchor sits too
        close.  A restarted radiod presets RTP from its uptime, so a genuine
        new run lands on that line only by coincidence."""
        def s32(x):
            x &= 0xFFFFFFFF
            return x - 0x100000000 if x >= 0x80000000 else x
        step = s32(run[-1].timestamp - run[0].timestamp) / (len(run) - 1)
        if self._line_anchor is not None:
            a_seq, a_ts = self._line_anchor
            span = self._seq_distance(a_seq, self.next_expected_seq)
            if span >= 2 * len(run):
                step = s32(self.next_expected_ts - a_ts) / span
        d0 = self._seq_distance(self.next_expected_seq, run[0].sequence)
        off = s32(run[0].timestamp - self.next_expected_ts)
        tol = max(abs(step), max(len(p.samples) for p in run), 1.0)
        return abs(off - d0 * step) <= tol

    def _apply_resync(self) -> None:
        """Restart the stream at the held run of a restarted sequence."""
        run, self._resync_pending = self._resync_pending, None
        self.buffer.clear()
        self.buffer_seq_nums.clear()
        self.next_expected_seq = run[0].sequence
        self.next_expected_ts = run[0].timestamp
        self._line_anchor = (run[0].sequence, run[0].timestamp)
        self._filled_seqs.clear()
        for p in run:
            self._add_to_buffer(p)
        self._interrupt_next = True

    def _log_interruption(self, msg: str) -> None:
        """ERROR at most once per INTERRUPTION_LOG_INTERVAL_SEC."""
        now = time.monotonic()
        if (self._last_interrupt_log is not None and
                now - self._last_interrupt_log
                < self.INTERRUPTION_LOG_INTERVAL_SEC):
            self._interrupt_logs_suppressed += 1
            logger.debug(msg)
            return
        if self._interrupt_logs_suppressed:
            msg += (f" [{self._interrupt_logs_suppressed} more since the "
                    f"last report; {self.stats.interruptions} in all]")
        self._interrupt_logs_suppressed = 0
        self._last_interrupt_log = now
        logger.error(msg)

    def _detect_zero_block(self, packet: RTPPacket,
                           position: int) -> Optional[GapEvent]:
        """A packet of nothing but exact zeros is radiod's drop marker.

        radiod zero-fills a dropped output block, so such a packet carries
        synthetic samples with a perfectly contiguous timestamp; no
        timestamp gap exists for the resequencer to find.  Checked as the
        packet is EMITTED, so the event travels with the zeros it marks and
        `position` is their offset in the emitted stream.

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
            position_samples=position,
            duration_samples=n,
            timestamp_utc=datetime.now(timezone.utc).isoformat(),
            packets_affected=1,
        )
    
    def _initialize(self, packet: RTPPacket):
        """Initialize sequencer with first packet"""
        self.next_expected_seq = packet.sequence
        self.next_expected_ts = packet.timestamp
        self._line_anchor = (packet.sequence, packet.timestamp)
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

    def _ts_gap(self, pkt: RTPPacket) -> int:
        """Signed 32-bit distance from next_expected_ts to pkt (Phil
        Karn's technique: handles RTP wrap)."""
        ts_diff = (pkt.timestamp - self.next_expected_ts) & 0xFFFFFFFF
        return ts_diff - 0x100000000 if ts_diff >= 0x80000000 else ts_diff

    def _try_output(
        self, force: bool = False
    ) -> Tuple[Optional[np.ndarray], List[GapEvent]]:
        """Output the next packet(s) in sequence, with any gap fills.

        The only place (with flush, which calls it) that counts emitted
        samples.  `pos` tracks the offset in the emitted stream of the
        next sample appended, so every gap event carries its true offset.

        force=True (flush) recovers past every hole instead of waiting for
        the buffer to fill.
        """
        output_samples = []
        gap_events = []
        pos = self.cumulative_samples
        # True RTP timestamp of the first sample this call will emit (gap
        # fills included: a fill starts exactly at next_expected_ts).
        # Published via last_chunk_rtp_start so consumers can label the
        # RESEQUENCED stream truthfully — labelling with the last RECEIVED
        # packet's header desynchronizes labels from delivered samples
        # under loss (hf-timestd T6 origin slips, 2026-08-11).
        chunk_start_ts = self.next_expected_ts

        if self._interrupt_next:
            # This chunk opens a re-synced sequence (see _apply_resync).
            self._interrupt_next = False
            self.stats.interruptions += 1
            self._log_interruption(
                f"stream continuity breaks at sample {pos}: RTP sequence "
                f"restarted at seq={self.next_expected_seq} "
                f"(STREAM_INTERRUPTION)")
            gap_events.append(GapEvent(
                source=GapSource.STREAM_INTERRUPTION,
                position_samples=pos,
                duration_samples=0,
                timestamp_utc=datetime.now(timezone.utc).isoformat(),
                packets_affected=0,
            ))
        
        while True:
            # Look for next expected sequence number
            next_pkt = None
            for pkt in self.buffer:
                if pkt.sequence == self.next_expected_seq:
                    next_pkt = pkt
                    break

            recovered = False
            if next_pkt is None:
                # Packet not in buffer - skip ahead once the buffer fills
                # (the packet is probably lost), or at flush.
                if not self.buffer or (
                        not force and len(self.buffer) < self.buffer_size // 2):
                    break  # Keep waiting
                next_pkt = min(
                    self.buffer,
                    key=lambda p: self._seq_distance(self.next_expected_seq,
                                                     p.sequence))
                recovered = True

            limit = self._fill_limit(next_pkt.samples.dtype)
            if recovered:
                gap = self._handle_lost_packet(next_pkt, pos, limit)
            elif next_pkt.timestamp != self.next_expected_ts:
                gap = self._detect_gap(next_pkt, pos, limit)
            else:
                gap = None

            if gap is not None and gap.source is GapSource.STREAM_INTERRUPTION:
                if output_samples:
                    # Never share a chunk across the break: this call ends
                    # here and the post-gap packet opens the next one, so
                    # its label (last_chunk_rtp_start) is its own RTP.
                    break
                jump = self._ts_gap(next_pkt)
                hint = ""
                if -len(next_pkt.samples) <= jump < 0:
                    hint = (" -- a small backward step on every packet means "
                            "the parsed sample count exceeds the RTP step: "
                            "channel count or encoding mismatch?")
                self.stats.interruptions += 1
                self._log_interruption(
                    f"RTP jumped {jump} samples at "
                    f"seq={next_pkt.sequence} (fill bound {limit} samples, "
                    f"backward jumps never filled): not filled; "
                    f"stream continuity breaks at sample {pos} "
                    f"(STREAM_INTERRUPTION){hint}"
                )
                gap_events.append(gap)
                self.next_expected_ts = next_pkt.timestamp
                chunk_start_ts = next_pkt.timestamp
                self._line_anchor = (next_pkt.sequence, next_pkt.timestamp)
                self._filled_seqs.clear()
            elif gap is not None:
                if recovered:
                    lost = (next_pkt.sequence - self.next_expected_seq) & 0xFFFF
                    first = max(0, lost - self.FILLED_SEQ_MEMORY)
                    self._filled_seqs.extend(
                        (self.next_expected_seq + i) & 0xFFFF
                        for i in range(first, lost))
                gap_events.append(gap)
                output_samples.append(
                    np.zeros(gap.duration_samples, dtype=next_pkt.samples.dtype))
                self.stats.samples_filled += gap.duration_samples
                pos += gap.duration_samples
            
            # Remove from buffer and output
            self.buffer.remove(next_pkt)
            self.buffer_seq_nums.discard(next_pkt.sequence)
            zero_gap = self._detect_zero_block(next_pkt, pos)
            if zero_gap is not None:
                gap_events.append(zero_gap)
            output_samples.append(next_pkt.samples)
            
            # Update state
            # Use actual packet sample count for timestamp tracking
            # This handles fragmented IQ packets where each fragment has fewer
            # samples than the nominal samples_per_packet
            actual_samples = len(next_pkt.samples)
            pos += actual_samples
            self.next_expected_seq = (next_pkt.sequence + 1) & 0xFFFF
            self.next_expected_ts = next_pkt.timestamp + actual_samples
            if self._line_anchor is None or self._seq_distance(
                    self._line_anchor[0], next_pkt.sequence) \
                    > self.LINE_ANCHOR_SPAN:
                self._line_anchor = (next_pkt.sequence, next_pkt.timestamp)
            if recovered and not force:
                self.stats.packets_resequenced += 1
        
        # Combine output
        if output_samples:
            combined = np.concatenate(output_samples)
            self.stats.samples_output += len(combined)
            self.cumulative_samples += len(combined)
            self.last_chunk_rtp_start = chunk_start_ts & 0xFFFFFFFF
            return combined, gap_events
        
        return None, []

    def _fill_limit(self, dtype) -> int:
        """Longest gap zero-filled for samples of `dtype`: max_fill_samples,
        capped by fill_byte_budget bytes."""
        itemsize = max(1, np.dtype(dtype).itemsize)
        return min(self.max_fill_samples, self.fill_byte_budget // itemsize)

    def _gap_event(self, ts_gap: int, position: int, packets_lost: int,
                   limit: int) -> Optional[GapEvent]:
        """The event for an RTP jump of ts_gap samples at `position`:
        NETWORK_LOSS (filled at its true length) for a forward gap up to
        `limit`; STREAM_INTERRUPTION (nothing filled, continuity broken)
        for a longer forward gap or any backward jump.  Late and duplicate
        packets never get here (process_packet drops them), so a backward
        jump in sequence order means the RTP clock moved."""
        if ts_gap == 0:
            return None
        if ts_gap < 0 or ts_gap > limit:
            return GapEvent(
                source=GapSource.STREAM_INTERRUPTION,
                position_samples=position,
                duration_samples=0,
                timestamp_utc=datetime.now(timezone.utc).isoformat(),
                packets_affected=packets_lost,
            )
        self.stats.gaps_detected += 1
        logger.warning(
            f"Gap: {ts_gap} samples ({ts_gap / self.sample_rate * 1000:.1f}ms), "
            f"~{packets_lost} packets"
        )
        return GapEvent(
            source=GapSource.NETWORK_LOSS,
            position_samples=position,
            duration_samples=ts_gap,
            timestamp_utc=datetime.now(timezone.utc).isoformat(),
            packets_affected=packets_lost,
        )
    
    def _detect_gap(self, next_pkt: RTPPacket, position: int,
                    limit: int) -> Optional[GapEvent]:
        """
        Gap before the in-sequence packet next_pkt, at stream offset
        `position`, using KA9Q signed 32-bit arithmetic (handles RTP wrap):
        - Positive difference = forward gap (fill with zeros up to `limit`)
        - Negative difference = backward jump (STREAM_INTERRUPTION)
        """
        ts_gap = self._ts_gap(next_pkt)
        return self._gap_event(ts_gap, position,
                               max(ts_gap, 0) // self.samples_per_packet,
                               limit)
    
    def _handle_lost_packet(self, earliest: RTPPacket, position: int,
                            limit: int) -> Optional[GapEvent]:
        """Gap before `earliest`, the packet recovery skips ahead to when
        the expected one is definitely lost.  Counts nothing: the caller
        emits and counts the fill and the packet."""
        ts_gap = self._ts_gap(earliest)
        packets_lost = (earliest.sequence - self.next_expected_seq) & 0xFFFF
        gap = self._gap_event(ts_gap, position, packets_lost, limit)
        if gap is not None and gap.source is GapSource.NETWORK_LOSS:
            logger.warning(
                f"Lost packet recovery: skip to seq={earliest.sequence}, "
                f"gap={ts_gap} samples"
            )
        return gap
    
    def _seq_distance(self, from_seq: int, to_seq: int) -> int:
        """Calculate forward distance between sequence numbers (handles wrap)"""
        dist = (to_seq - from_seq) & 0xFFFF
        return dist if dist < 32768 else dist - 65536
    
    def flush(self) -> Tuple[np.ndarray, List[GapEvent]]:
        """
        Flush remaining packets in buffer (for shutdown).
        
        Returns buffered samples in sequence order (wrap-safe, from
        next_expected_seq) with any gaps filled, counted and labelled
        (last_chunk_rtp_start) like any other emission.

        A chunk never spans a STREAM_INTERRUPTION, so flush stops before
        one and leaves the post-gap packets buffered: call it again while
        `pending` stays true, and each call returns the next chunk, which
        opens with the interruption event.
        """
        samples, gap_events = None, []
        if self._resync_pending is not None and not self.buffer:
            self._apply_resync()
        if self.initialized:
            samples, gap_events = self._try_output(force=True)
        if samples is None and self._resync_pending is not None:
            self._apply_resync()
            samples, gap_events = self._try_output(force=True)
        if samples is None:
            # Nothing left to emit: drop whatever remains.
            self.buffer.clear()
            self.buffer_seq_nums.clear()
        
        if samples is not None:
            return samples, gap_events
        
        return np.array([], dtype=np.complex64), gap_events
    
    @property
    def pending(self) -> bool:
        """True while packets wait in the buffer (see flush)."""
        return bool(self.buffer) or self._resync_pending is not None

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
        self._resync_run = []
        self._resync_pending = None
        self._interrupt_next = False
        self._line_anchor = None
        self._filled_seqs.clear()
