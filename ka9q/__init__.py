"""
ka9q: Python interface for ka9q-radio

A general-purpose library for controlling ka9q-radio channels and streams.
No assumptions about your application - works for everything from AM radio
listening to SuperDARN radar monitoring.

Recommended usage (self-healing stream):
    from ka9q import RadiodControl, ManagedStream
    
    def on_samples(samples, quality):
        process(samples)
    
    def on_dropped(reason):
        print(f"Stream dropped: {reason}")
    
    def on_restored(channel):
        print(f"Stream restored: {channel.frequency/1e6:.3f} MHz")
    
    with RadiodControl("radiod.local") as control:
        # ManagedStream auto-heals through radiod restarts
        stream = ManagedStream(
            control=control,
            frequency_hz=14.074e6,
            preset="usb",
            sample_rate=12000,
            on_samples=on_samples,
            on_stream_dropped=on_dropped,
            on_stream_restored=on_restored,
        )
        stream.start()
        # ... stream continues through disruptions ...
        stream.stop()

Manual channel management:
    from ka9q import RadiodControl, RadiodStream
    
    with RadiodControl("radiod.local") as control:
        # ensure_channel() verifies the channel before returning
        channel = control.ensure_channel(
            frequency_hz=14.074e6,
            preset="usb",
            sample_rate=12000
        )
        stream = RadiodStream(channel, on_samples=my_callback)
        stream.start()

Lower-level usage (explicit control):
    from ka9q import RadiodControl, allocate_ssrc
    
    with RadiodControl("radiod.local") as control:
        ssrc = control.create_channel(
            frequency_hz=10.0e6,
            preset="am",
            sample_rate=12000
        )
        print(f"Created channel with SSRC: {ssrc}")
"""
try:
    from importlib.metadata import version as _pkg_version, PackageNotFoundError as _PkgNotFound
    try:
        __version__ = _pkg_version("ka9q-python")
    except _PkgNotFound:
        __version__ = "0.0.0+unknown"
    del _pkg_version, _PkgNotFound
except ImportError:
    __version__ = "0.0.0+unknown"
__author__ = 'Michael Hauan AC0G'

from .control import RadiodControl, allocate_ssrc
from .discovery import (
    discover_channels,
    discover_channels_native,
    discover_channels_via_control,
    discover_radiod_services,
    ChannelInfo
)
from .types import StatusType, Encoding, DemodType, WindowType
from .status import (
    FrontendStatus,
    ChannelStatus,
    PllStatus,
    FmStatus,
    SpectrumStatus,
    Filter2Status,
    OpusStatus,
    decode_status_packet,
)
from .exceptions import Ka9qError, ConnectionError, CommandError, ValidationError
from .rtp_recorder import (
    RTPRecorder,
    RecorderState,
    RTPHeader,
    RecordingMetrics,
    parse_rtp_header,
    rtp_to_utc,
    rtp_to_wallclock,
)
from .stream_quality import (
    GapSource,
    GapEvent,
    StreamQuality,
)
from .resequencer import (
    PacketResequencer,
    RTPPacket,
    ResequencerStats,
)
from .stream import (
    RadiodStream,
)
from .managed_stream import (
    ManagedStream,
    ManagedStreamStats,
    StreamState,
)
from .multi_stream import MultiStream
from .slot_clock import SlotClock, Slot, SlotClockDesyncError, rtp_diff
from .spectrum_stream import SpectrumStream
from .status_listener import StatusListener, StatusListenerStats

__all__ = [
    # Control
    'RadiodControl',
    'allocate_ssrc',
    
    # Discovery
    'discover_channels',
    'discover_channels_native',
    'discover_channels_via_control',
    'discover_radiod_services',
    'ChannelInfo',
    
    # Types
    'StatusType',
    'Encoding',
    'DemodType',
    'WindowType',

    # Typed status
    'FrontendStatus',
    'ChannelStatus',
    'PllStatus',
    'FmStatus',
    'SpectrumStatus',
    'Filter2Status',
    'OpusStatus',
    'decode_status_packet',
    
    # Exceptions
    'Ka9qError',
    'ConnectionError',
    'CommandError',
    'ValidationError',
    
    # Low-level RTP (packet-oriented)
    'RTPRecorder',
    'RecorderState',
    'RTPHeader',
    'RecordingMetrics',
    'parse_rtp_header',
    'rtp_to_utc',
    'rtp_to_wallclock',
    
    # Slot timing (epoch-aligned, RTP-referenced)
    'SlotClock',
    'Slot',
    'SlotClockDesyncError',
    'rtp_diff',

    # Stream API (sample-oriented)
    'RadiodStream',
    'StreamQuality',
    'GapSource',
    'GapEvent',
    'PacketResequencer',
    'RTPPacket',
    'ResequencerStats',
    
    # Managed Stream (self-healing, single channel)
    'ManagedStream',
    'ManagedStreamStats',
    'StreamState',

    # Multi Stream (shared socket, multiple channels)
    'MultiStream',

    # Spectrum Stream (FFT bin data receiver)
    'SpectrumStream',

    # Continuous STATUS listener (live timing anchor refresh)
    'StatusListener',
    'StatusListenerStats',

    # Utilities
    'generate_multicast_ip',
    'ChannelMonitor',

    # L6 BPSK PPS calibration
    'BpskPpsCalibrator',
    'PpsCalibrationResult',
    'NotchFilter500Hz',
]

from .addressing import generate_multicast_ip
from .monitor import ChannelMonitor
from .compat import KA9Q_RADIO_COMMIT
from .pps_calibrator import BpskPpsCalibrator, PpsCalibrationResult, NotchFilter500Hz
