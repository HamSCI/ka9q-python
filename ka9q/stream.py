"""
RadiodStream - High-Level Sample Stream Interface

Provides a continuous sample stream from radiod with quality metadata.
This is the primary interface for applications consuming radio data.

Features:
- Automatic multicast subscription
- RTP packet reception and parsing
- Packet resequencing and gap filling
- Quality tracking (StreamQuality) with every callback
- Cross-platform support (Linux, macOS, Windows)

Usage:
    from ka9q import RadiodStream, StreamQuality
    
    def on_samples(samples: np.ndarray, quality: StreamQuality):
        # Process continuous sample stream
        print(f"Got {len(samples)} samples, completeness: {quality.completeness_pct:.1f}%")
    
    stream = RadiodStream(
        channel=channel_info,
        on_samples=on_samples,
    )
    stream.start()
    # ... run until done ...
    stream.stop()
"""

import socket
import struct
import logging
import threading
import time
import numpy as np
from datetime import datetime, timezone
from typing import Optional, Callable, List

from ._multicast import join_multicast_all_interfaces
from .discovery import ChannelInfo
from .rtp_recorder import RTPHeader, parse_rtp_header, rtp_to_wallclock
from .resequencer import PacketResequencer, RTPPacket
from .stream_quality import GapSource, GapEvent, StreamQuality
from .types import Encoding

logger = logging.getLogger(__name__)

# Type alias for sample callback
SampleCallback = Callable[[np.ndarray, StreamQuality], None]


# G.711 µ-law and A-law decode tables (ITU-T G.711). Each maps a uint8 byte
# to its signed 16-bit linear PCM equivalent. Precomputed once at import.
def _build_mulaw_table() -> np.ndarray:
    out = np.empty(256, dtype=np.int16)
    for i in range(256):
        u = ~i & 0xFF
        sign = u & 0x80
        exponent = (u >> 4) & 0x07
        mantissa = u & 0x0F
        sample = ((mantissa << 3) + 0x84) << exponent
        sample -= 0x84
        out[i] = -sample if sign else sample
    return out


def _build_alaw_table() -> np.ndarray:
    out = np.empty(256, dtype=np.int16)
    for i in range(256):
        a = i ^ 0x55
        sign = a & 0x80
        exponent = (a >> 4) & 0x07
        mantissa = a & 0x0F
        if exponent == 0:
            sample = (mantissa << 4) + 8
        else:
            sample = ((mantissa << 4) + 0x108) << (exponent - 1)
        out[i] = -sample if sign else sample
    return out


_MULAW_TABLE = _build_mulaw_table()
_ALAW_TABLE = _build_alaw_table()


def parse_rtp_samples(
    payload: bytes, encoding: int, is_iq: bool
) -> Optional[np.ndarray]:
    """Parse RTP payload samples based on encoding.

    Shared by RadiodStream and MultiStream. Covers every linear-PCM encoding
    radiod can grant: NO_ENCODING/S16LE/S16BE/F32LE/F32BE/F16LE/F16BE plus
    G.711 MULAW/ALAW.  OPUS (3/7) requires libopus and is handled by
    ``OpusDecoder`` in this module — not by this raw-sample helper.  AX25 (5)
    is a framed protocol, not audio samples, and is also out of scope here.

    Args:
        payload: Raw RTP payload bytes (after header).
        encoding: Channel encoding from ``ka9q.types.Encoding``.
        is_iq: True for IQ (complex) mode, False for audio (real) mode.

    Returns:
        Parsed samples as np.ndarray (complex64 for IQ, float32 for audio),
        or None on error.
    """
    try:
        floats = _decode_to_float32(payload, encoding, is_iq=is_iq)
        if floats is None:
            return None
        if is_iq:
            # radiod silently downgrades F32 → S16 for some IQ-channel
            # configurations (high sample rate + wide filter); confirmed on
            # bee1 2026-05-15 for T6/TSL3 BPSK PPS channel (encoding=4
            # requested, encoding=2 granted, decoded as F32LE → NaN-poisoned
            # input, TSL3 dark). Honor the granted encoding above.
            if len(floats) % 2 != 0:
                logger.warning(f"Odd number of samples in IQ payload: {len(floats)}")
                return None
            samples = floats[0::2] + 1j * floats[1::2]
            return samples.astype(np.complex64)
        return floats
    except Exception as e:
        logger.error(f"Failed to parse payload: {e}")
        return None


def _decode_to_float32(payload: bytes, encoding: int, *, is_iq: bool = False) -> Optional[np.ndarray]:
    """Decode raw RTP payload bytes to a flat float32 sample array.

    For IQ the caller interleaves; for audio the array is the output directly.
    Returns None for encodings this helper does not cover (OPUS, AX25).
    The ``is_iq`` flag only affects the wording of the fallback warning so
    callers can grep for IQ-specific decode anomalies (the bee1 TSL3 case).
    """
    if encoding in (Encoding.NO_ENCODING, Encoding.F32LE):
        return np.frombuffer(payload, dtype='<f4').astype(np.float32, copy=False)
    if encoding == Encoding.F32BE:
        return np.frombuffer(payload, dtype='>f4').astype(np.float32, copy=False)
    if encoding == Encoding.S16LE:
        return np.frombuffer(payload, dtype='<i2').astype(np.float32) / 32768.0
    if encoding == Encoding.S16BE:
        return np.frombuffer(payload, dtype='>i2').astype(np.float32) / 32768.0
    if encoding == Encoding.F16LE:
        return np.frombuffer(payload, dtype='<f2').astype(np.float32)
    if encoding == Encoding.F16BE:
        return np.frombuffer(payload, dtype='>f2').astype(np.float32)
    if encoding == Encoding.MULAW:
        idx = np.frombuffer(payload, dtype=np.uint8)
        return (_MULAW_TABLE[idx].astype(np.float32) / 32768.0)
    if encoding == Encoding.ALAW:
        idx = np.frombuffer(payload, dtype=np.uint8)
        return (_ALAW_TABLE[idx].astype(np.float32) / 32768.0)
    if encoding in (Encoding.OPUS, Encoding.OPUS_VOIP):
        # Opus payloads are codec frames, not raw samples — caller must use
        # ka9q.stream.OpusDecoder (requires libopus / opuslib).
        return None
    if encoding == Encoding.AX25:
        # AX25 is a framed protocol — bytes are the payload, not samples.
        return None
    kind = "IQ" if is_iq else "audio"
    logger.warning(f"Unsupported {kind} encoding {encoding}, falling back to F32LE")
    return np.frombuffer(payload, dtype='<f4').astype(np.float32, copy=False)


class OpusDecoder:
    """Optional Opus → float32 decoder for radiod OPUS / OPUS_VOIP streams.

    Requires the ``opuslib`` package (``pip install ka9q-python[opus]`` or
    ``pip install opuslib``).  Maintains internal codec state across calls so
    packet-loss concealment works end-to-end; create one instance per stream
    SSRC and feed it each RTP payload in order.

    Example::

        dec = OpusDecoder(sample_rate=48000, channels=1)
        for payload in opus_payloads:
            samples = dec.decode(payload)  # float32, mono
    """

    def __init__(self, sample_rate: int = 48000, channels: int = 1):
        try:
            import opuslib  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "Opus decoding requires opuslib — install with "
                "'pip install ka9q-python[opus]' or 'pip install opuslib'"
            ) from exc
        if sample_rate not in (8000, 12000, 16000, 24000, 48000):
            raise ValueError(
                f"Opus sample_rate must be one of 8000/12000/16000/24000/48000; got {sample_rate}"
            )
        if channels not in (1, 2):
            raise ValueError(f"Opus channels must be 1 or 2; got {channels}")
        self._sample_rate = sample_rate
        self._channels = channels
        self._dec = opuslib.Decoder(sample_rate, channels)
        # 120 ms is the largest Opus frame; allocate per-call.
        self._max_frame_samples = (sample_rate * 120) // 1000

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    @property
    def channels(self) -> int:
        return self._channels

    def decode(self, payload: bytes, *, fec: bool = False) -> np.ndarray:
        """Decode one Opus RTP payload to float32 PCM samples.

        For stereo streams the result is interleaved L,R,L,R,…  Empty payload
        triggers packet-loss concealment (one frame of silence/extrapolation).
        """
        if not payload and not fec:
            # Generate one PLC frame of typical Opus duration (20 ms).
            n_samples = (self._sample_rate * 20) // 1000
        else:
            n_samples = self._max_frame_samples
        pcm = self._dec.decode(payload, n_samples, decode_fec=fec)
        # opuslib returns bytes of int16 little-endian; convert to float32 [-1,1].
        int16s = np.frombuffer(pcm, dtype='<i2')
        return int16s.astype(np.float32) / 32768.0


class RadiodStream:
    """
    High-level interface to a radiod IQ/audio stream.
    
    Handles all low-level details:
    - Multicast subscription and RTP packet reception
    - Packet resequencing and gap detection
    - Gap filling with zeros for continuous stream
    - Quality tracking with detailed metrics
    
    Delivers to application:
    - Continuous sample stream (np.ndarray, complex64 or float32)
    - StreamQuality metadata with every callback
    """
    
    def __init__(
        self,
        channel: ChannelInfo,
        on_samples: Optional[SampleCallback] = None,
        samples_per_packet: int = 320,
        resequence_buffer_size: int = 64,
        deliver_interval_packets: int = 10,
    ):
        """
        Initialize RadiodStream.
        
        Args:
            channel: ChannelInfo with stream details (from discover_channels)
            on_samples: Callback(samples, quality) for sample delivery
            samples_per_packet: Expected samples per RTP packet (320 @ 16kHz)
            resequence_buffer_size: Packets to buffer for resequencing (64 = ~2s)
            deliver_interval_packets: Deliver to callback every N packets (batching)
        """
        self.channel = channel
        self.on_samples = on_samples
        self.samples_per_packet = samples_per_packet
        self.deliver_interval_packets = deliver_interval_packets
        
        # Resequencer
        self.resequencer = PacketResequencer(
            buffer_size=resequence_buffer_size,
            samples_per_packet=samples_per_packet,
            sample_rate=channel.sample_rate,
        )
        
        # Quality tracking
        self.quality = StreamQuality()
        
        # Sample accumulator for batched delivery
        self._sample_buffer: List[np.ndarray] = []
        self._gap_buffer: List[GapEvent] = []
        self._packets_since_delivery = 0
        
        # Socket and threading
        self._socket: Optional[socket.socket] = None
        self._running = False
        self._thread: Optional[threading.Thread] = None
        
        # Reconnection state for robustness
        self._reconnect_backoff = 1.0  # Initial backoff in seconds
        self._max_reconnect_backoff = 60.0  # Max backoff
        self._consecutive_errors = 0
        
        # Detect payload format from channel preset
        # IQ mode: complex64 (interleaved float32 I/Q)
        # Audio modes: float32 (mono or stereo)
        self._is_iq = channel.preset.lower() in ('iq', 'spectrum')
        
        # Payload samples differ from RTP timestamp increment in IQ mode
        # IQ: 160 complex samples per packet, but timestamp advances by 320
        # Audio: samples_per_packet real samples, timestamp advances same
        self._payload_samples_per_packet = samples_per_packet // 2 if self._is_iq else samples_per_packet
    
    def start(self):
        """Start receiving and delivering samples."""
        if self._running:
            logger.warning("Stream already running")
            return
        
        # Initialize quality tracking
        self.quality = StreamQuality(
            stream_start_utc=datetime.now(timezone.utc).isoformat(),
            sample_rate=self.channel.sample_rate,
        )
        
        # Track first RTP timestamp
        self._first_rtp_timestamp: Optional[int] = None
        
        # Reset resequencer
        self.resequencer.reset()
        
        # Start receive thread
        self._running = True
        self._thread = threading.Thread(target=self._receive_loop, daemon=True)
        self._thread.start()
        
        logger.info(
            f"RadiodStream started: {self.channel.multicast_address}:{self.channel.port} "
            f"SSRC={self.channel.ssrc}"
        )
    
    def stop(self) -> StreamQuality:
        """
        Stop receiving and return final quality metrics.
        
        Returns:
            Final StreamQuality with complete statistics
        """
        if not self._running:
            return self.quality.copy()
        
        self._running = False
        
        if self._thread:
            self._thread.join(timeout=5.0)
            self._thread = None
        
        # Flush resequencer
        final_samples, final_gaps = self.resequencer.flush()
        if len(final_samples) > 0 or final_gaps:
            self._sample_buffer.append(final_samples)
            self._gap_buffer.extend(final_gaps)
            self._deliver_samples()
        
        logger.info(
            f"RadiodStream stopped. Completeness: {self.quality.completeness_pct:.1f}%, "
            f"Gaps: {self.quality.total_gap_events}"
        )
        
        return self.quality.copy()
    
    def _create_socket(self) -> socket.socket:
        """Create and configure multicast receive socket."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

        # Allow address reuse so multiple processes can each receive the
        # same multicast group.  Deliberately NOT SO_REUSEPORT: with a
        # group-address bind, REUSEPORT makes the kernel load-balance
        # datagrams across the reuseport set, silently starving this
        # socket of most of its own packets.  SO_REUSEADDR alone permits
        # multiple multicast listeners without that load-balancing.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        # Large receive buffer — matches MultiStream (commit 3a6cf26).
        # Single-channel sockets see less aggregate throughput than
        # MultiStream's multi-channel one, but they're also more
        # vulnerable to GIL-stall packet loss because no other consumer
        # is draining when this one stalls.  64 MB gives the kernel
        # enough headroom across the typical sub-second GIL pause.
        # Honored only if ``net.core.rmem_max >= 64 MB``; sigmond's
        # rule_kernel_rcvbuf_adequate provisions this.
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 64 * 1024 * 1024)
        except OSError:
            pass

        # Bind to the channel's multicast GROUP address, NOT 0.0.0.0.
        # This is the per-client segmentation mechanism: radiod publishes
        # each channel to its own multicast group, and binding the socket
        # to that group makes the kernel deliver ONLY that group's packets.
        # A 0.0.0.0:port bind instead receives every multicast group the
        # host has joined on this RTP port — i.e. every other client's
        # channels too (tens of thousands of pps on a busy host) — which
        # this socket would then have to filter in Python, dropping its
        # own packets under the load.  Fall back to 0.0.0.0 only if the
        # group bind is rejected (non-Linux multicast-bind semantics).
        group_bound = True
        try:
            sock.bind((self.channel.multicast_address, self.channel.port))
        except OSError:
            sock.bind(('0.0.0.0', self.channel.port))
            group_bound = False

        if group_bound:
            # Group-address bind: the kernel already filters to this group,
            # so a single INADDR_ANY membership is the correct companion
            # (the kernel delivers the group on whatever interface it
            # arrives).  A per-interface "join every interface" membership
            # instead breaks delivery when combined with a group bind, so
            # it is only used on the 0.0.0.0 fallback path below.  We also
            # join loopback explicitly for a co-located TTL=0 radiod that
            # emits only on `lo`.
            joined: list[str] = []
            group = socket.inet_aton(self.channel.multicast_address)
            try:
                sock.setsockopt(
                    socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                    struct.pack("=4sl", group, socket.INADDR_ANY),
                )
                joined.append("*")
            except OSError as exc:
                logger.debug("INADDR_ANY join failed for %s: %s",
                             self.channel.multicast_address, exc)
            try:
                sock.setsockopt(
                    socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                    struct.pack("=4s4s", group, socket.inet_aton("127.0.0.1")),
                )
                joined.append("lo")
            except OSError:
                pass  # loopback membership is best-effort
        else:
            # 0.0.0.0 fallback: join on every local IPv4 interface, the
            # legacy behaviour that catches a radiod whose output arrives
            # on a non-default interface (including a TTL=0 loopback-only
            # co-located radiod).
            joined = join_multicast_all_interfaces(
                sock, self.channel.multicast_address,
            )

        if not joined:
            logger.warning(
                "RadiodStream: no interface accepted the multicast "
                "join for %s — recvfrom() will return nothing",
                self.channel.multicast_address,
            )
        else:
            logger.debug(
                "RadiodStream: bound %s (%s):%d joined via: %s",
                self.channel.multicast_address,
                "group" if group_bound else "0.0.0.0",
                self.channel.port, ", ".join(joined),
            )

        # Timeout for periodic running check
        sock.settimeout(1.0)

        return sock
    
    def _receive_loop(self):
        """Main packet receiving loop with automatic reconnection.
        
        Handles socket errors by attempting to recreate the socket with
        exponential backoff. This provides robustness against:
        - Network interface restarts
        - Multicast group membership drops
        - Transient network errors
        """
        while self._running:
            try:
                # Create socket if needed
                if self._socket is None:
                    self._socket = self._create_socket()
                    self._reconnect_backoff = 1.0  # Reset backoff on success
                    self._consecutive_errors = 0
                
                # Receive packet
                data, addr = self._socket.recvfrom(8192)
                self._process_packet(data)
                self._consecutive_errors = 0  # Reset on successful receive
                
            except socket.timeout:
                continue
                
            except OSError as e:
                # Socket error - attempt reconnection
                if not self._running:
                    break
                
                self._consecutive_errors += 1
                logger.error(
                    f"Socket error (attempt {self._consecutive_errors}): {e}",
                    exc_info=True
                )
                
                # Close broken socket
                if self._socket:
                    try:
                        self._socket.close()
                    except Exception:
                        pass
                    self._socket = None
                
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
                if self._running:
                    logger.error(f"Receive error: {e}", exc_info=True)
        
        # Cleanup on exit
        if self._socket:
            try:
                self._socket.close()
            except Exception:
                pass
            self._socket = None
    
    def _process_packet(self, data: bytes):
        """Process a received RTP packet."""
        # Fast SSRC peek BEFORE the full RTP parse.  radiod commonly
        # publishes many channels onto one shared data multicast group,
        # so this single socket sees every channel's packets — tens of
        # thousands per second on a busy host.  Fully parsing each one
        # only to discard a foreign SSRC starves the receive loop and the
        # kernel drops our *own* packets (manifesting downstream as a
        # resequencer that never advances).  The cheap 4-byte SSRC peek
        # lets us skip foreign packets at a fraction of the cost — the
        # same strategy MultiStream uses for the identical situation.
        if len(data) < 12:
            return
        if struct.unpack_from("!I", data, 8)[0] != self.channel.ssrc:
            return  # Foreign SSRC on the shared group — skip cheaply.

        # Parse RTP header (only our own packets reach here now)
        header = parse_rtp_header(data)
        if header is None:
            logger.debug("Invalid RTP packet")
            return

        # SSRC already verified by the fast peek above.

        # Update RTP stats
        self.quality.rtp_packets_received += 1
        
        # Track first and last RTP timestamps
        if self._first_rtp_timestamp is None:
            self._first_rtp_timestamp = header.timestamp
            self.quality.first_rtp_timestamp = header.timestamp
        self.quality.last_rtp_timestamp = header.timestamp
        
        # Extract payload
        header_len = 12 + (4 * header.csrc_count)
        payload = data[header_len:]
        
        if len(payload) == 0:
            # Empty payload - track as gap source
            self._record_empty_payload(header)
            return
        
        # Parse samples from payload
        samples = self._parse_samples(payload)
        if samples is None:
            return
        
        # Get wallclock time
        wallclock = rtp_to_wallclock(header.timestamp, self.channel)
        
        # Create packet for resequencer
        packet = RTPPacket(
            sequence=header.sequence,
            timestamp=header.timestamp,
            ssrc=header.ssrc,
            samples=samples,
            wallclock=wallclock,
        )
        
        # Process through resequencer
        output_samples, gap_events = self.resequencer.process_packet(packet)
        
        # Accumulate output
        if output_samples is not None:
            self._sample_buffer.append(output_samples)
            self._gap_buffer.extend(gap_events)
            self._packets_since_delivery += 1
            
            # Update gap stats
            for gap in gap_events:
                self.quality.total_gap_events += 1
                self.quality.total_gaps_filled += gap.duration_samples
            
            # Deliver if we've accumulated enough
            if self._packets_since_delivery >= self.deliver_interval_packets:
                self._deliver_samples()
        
        # Update timing
        if wallclock:
            self.quality.last_packet_utc = datetime.fromtimestamp(
                wallclock, tz=timezone.utc
            ).isoformat()
    
    def _parse_samples(self, payload: bytes) -> Optional[np.ndarray]:
        """Parse samples from RTP payload based on channel encoding."""
        enc = getattr(self.channel, 'encoding', 0)
        return parse_rtp_samples(payload, enc, self._is_iq)
    
    def _record_empty_payload(self, header: RTPHeader):
        """Record an empty payload as a gap event."""
        gap = GapEvent(
            source=GapSource.EMPTY_PAYLOAD,
            position_samples=self.quality.total_samples_delivered,
            duration_samples=self.samples_per_packet,
            timestamp_utc=datetime.now(timezone.utc).isoformat(),
            packets_affected=1,
        )
        self._gap_buffer.append(gap)
        self.quality.total_gap_events += 1
        self.quality.total_gaps_filled += self.samples_per_packet
    
    def _deliver_samples(self):
        """Deliver accumulated samples to callback."""
        if not self._sample_buffer:
            return
        
        # Combine samples
        samples = np.concatenate(self._sample_buffer)
        gaps = list(self._gap_buffer)
        
        # Update quality for this batch
        batch_start = self.quality.total_samples_delivered
        self.quality.batch_start_sample = batch_start
        self.quality.batch_samples_delivered = len(samples)
        self.quality.batch_gaps = gaps
        self.quality.total_samples_delivered += len(samples)
        
        # Update expected samples (based on actual payload samples per packet)
        self.quality.total_samples_expected = (
            self.quality.rtp_packets_received * self._payload_samples_per_packet
        )
        
        # Update RTP loss stats from resequencer
        reseq_stats = self.resequencer.get_stats()
        self.quality.rtp_packets_lost = reseq_stats.get('gaps_detected', 0)
        self.quality.rtp_packets_resequenced = reseq_stats.get('packets_resequenced', 0)
        self.quality.rtp_packets_duplicate = reseq_stats.get('packets_duplicate', 0)
        
        # Clear buffers
        self._sample_buffer = []
        self._gap_buffer = []
        self._packets_since_delivery = 0
        
        # Deliver to callback
        if self.on_samples:
            try:
                self.on_samples(samples, self.quality.copy())
            except Exception as e:
                logger.error(f"Error in sample callback: {e}", exc_info=True)
    
    @property
    def is_running(self) -> bool:
        """True if stream is actively receiving."""
        return self._running
    
    def get_quality(self) -> StreamQuality:
        """Get current quality metrics (copy)."""
        return self.quality.copy()
    
    def __del__(self):
        """
        Ensure stream is stopped on garbage collection
        
        This provides a safety net for unclosed streams and helps
        detect resource leaks during development.
        """
        try:
            self.stop()
        except Exception:
            pass  # Can't raise exceptions in __del__
