"""
Typed channel + frontend status for ka9q-radio.

radiod packs both the frontend state (`struct frontend`) and the channel state
(`struct channel`) into the same TLV status packet it multicasts on the status
group. This module defines typed dataclasses that mirror what the C `control`
program extracts from that packet, plus a decoder that populates them.

The decoder is a superset of :meth:`RadiodControl._decode_status_response`:
that method returns a flat dict for backward compatibility; this module returns
structured :class:`FrontendStatus` and :class:`ChannelStatus` objects suitable
for driving the TUI, CLI, and programmatic access.
"""
from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field, fields, asdict
from typing import Any, Dict, List, Optional

import numpy as np

from .types import StatusType, DemodType, Encoding, WindowType


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class FrontendStatus:
    """State of the SDR front-end (RX888 or similar).

    Populated from the same status packet that carries channel state —
    radiod encodes both together in `send_radio_status()`.
    """

    description: Optional[str] = None          # DESCRIPTION
    input_samprate: Optional[int] = None       # INPUT_SAMPRATE (Hz)
    input_samples: Optional[int] = None        # INPUT_SAMPLES (cumulative)
    ad_bits_per_sample: Optional[int] = None   # AD_BITS_PER_SAMPLE
    ad_over: Optional[int] = None              # AD_OVER (overrange count)
    samples_since_over: Optional[int] = None   # SAMPLES_SINCE_OVER
    isreal: Optional[bool] = None              # FE_ISREAL
    direct_conversion: Optional[bool] = None   # DIRECT_CONVERSION

    # Tuning / calibration
    calibrate: Optional[float] = None          # CALIBRATE — GPSDO clock error ratio
    first_lo: Optional[float] = None           # FIRST_LO_FREQUENCY (Hz)
    lock: Optional[bool] = None                # LOCK (tuner lock)
    fe_low_edge: Optional[float] = None        # FE_LOW_EDGE (Hz)
    fe_high_edge: Optional[float] = None       # FE_HIGH_EDGE (Hz)

    # Gains
    lna_gain: Optional[int] = None             # LNA_GAIN (dB)
    mixer_gain: Optional[int] = None           # MIXER_GAIN (dB)
    if_gain: Optional[int] = None              # IF_GAIN (dB)
    rf_gain: Optional[float] = None            # RF_GAIN (dB)
    rf_atten: Optional[float] = None           # RF_ATTEN (dB)
    rf_agc: Optional[bool] = None              # RF_AGC
    rf_level_cal: Optional[float] = None       # RF_LEVEL_CAL (dB, dBm↔dBFS)

    # Levels / calibration residuals
    if_power: Optional[float] = None           # IF_POWER (dBFS)
    dc_i_offset: Optional[float] = None        # DC_I_OFFSET
    dc_q_offset: Optional[float] = None        # DC_Q_OFFSET
    iq_imbalance: Optional[float] = None       # IQ_IMBALANCE (dB)
    iq_phase: Optional[float] = None           # IQ_PHASE (radians)

    @property
    def calibrate_ppm(self) -> Optional[float]:
        """GPSDO clock error expressed as parts-per-million."""
        return None if self.calibrate is None else self.calibrate * 1e6

    @property
    def gpsdo_reference_hz(self) -> Optional[float]:
        """Effective reference frequency implied by `calibrate`.

        Nominal 10 MHz reference scaled by (1 + calibrate). This is what the
        GPSDO is actually delivering to the RX888, per radiod's measurement.
        """
        if self.calibrate is None:
            return None
        return 10_000_000.0 * (1.0 + self.calibrate)

    @property
    def input_power_dbm(self) -> Optional[float]:
        """Absolute input power in dBm if the front-end is calibrated."""
        if self.if_power is None or self.rf_level_cal is None:
            return None
        # control.c: dBm = if_power (dBFS) + rf_level_cal + analog gain chain
        analog = 0.0
        for v in (self.lna_gain, self.mixer_gain, self.if_gain):
            if v is not None:
                analog += v
        if self.rf_atten is not None:
            analog += self.rf_atten  # attenuation is signed positive
        if self.rf_gain is not None:
            analog -= self.rf_gain
        return self.if_power + self.rf_level_cal - analog


@dataclass
class PllStatus:
    enable: Optional[bool] = None
    lock: Optional[bool] = None
    square: Optional[bool] = None
    phase: Optional[float] = None          # radians
    bw: Optional[float] = None             # Hz
    snr: Optional[float] = None            # dB
    wraps: Optional[int] = None
    freq_offset: Optional[float] = None    # Hz


@dataclass
class FmStatus:
    peak_deviation: Optional[float] = None  # Hz
    fm_snr: Optional[float] = None          # dB
    pl_tone: Optional[float] = None         # Hz (configured)
    pl_deviation: Optional[float] = None    # Hz (measured)
    deemph_tc: Optional[float] = None       # seconds
    deemph_gain: Optional[float] = None     # dB
    threshold_extend: Optional[bool] = None


@dataclass
class SpectrumStatus:
    avg: Optional[int] = None
    base: Optional[float] = None            # dB
    step: Optional[float] = None            # dB per byte
    shape: Optional[float] = None           # Kaiser β or equivalent
    fft_n: Optional[int] = None
    overlap: Optional[float] = None         # 0-1
    resolution_bw: Optional[float] = None   # Hz
    noise_bw: Optional[float] = None        # bins
    bin_count: Optional[int] = None
    crossover: Optional[float] = None       # Hz
    window_type: Optional[int] = None       # see WindowType

    # Spectrum bin vectors — populated from BIN_DATA or BIN_BYTE_DATA TLVs.
    # bin_data: float32 power values (SPECT_DEMOD), frequency order:
    #   bin 0 = DC, bins 1..N/2 = positive, bins N/2+1..N-1 = negative
    bin_data: Optional[np.ndarray] = field(default=None, repr=False)
    # bin_byte_data: raw uint8 quantised bins (SPECT2_DEMOD), same order.
    # Convert to dB with: dB = base + byte_value * step
    bin_byte_data: Optional[np.ndarray] = field(default=None, repr=False)

    @property
    def bin_power_db(self) -> Optional[np.ndarray]:
        """Spectrum bins as dB values, regardless of source format.

        For SPECT_DEMOD (bin_data): 10*log10(value), clamped to -150 dB.
        For SPECT2_DEMOD (bin_byte_data): base + byte * step.
        Returns None if no bin data is present.
        """
        if self.bin_data is not None:
            with np.errstate(divide='ignore'):
                db = 10.0 * np.log10(np.maximum(self.bin_data, 1e-30))
            return db.astype(np.float32)
        if self.bin_byte_data is not None:
            base = self.base if self.base is not None else 0.0
            step_val = self.step if self.step is not None else 1.0
            return (base + self.bin_byte_data.astype(np.float32) * step_val)
        return None


@dataclass
class Filter2Status:
    blocking: Optional[int] = None          # FILTER2 (block factor)
    blocksize: Optional[int] = None
    fir_length: Optional[int] = None
    kaiser_beta: Optional[float] = None


@dataclass
class OpusStatus:
    bit_rate: Optional[int] = None
    dtx: Optional[bool] = None
    application: Optional[int] = None
    bandwidth: Optional[int] = None
    fec: Optional[int] = None


@dataclass
class ChannelStatus:
    """State of one demodulation channel, plus the frontend it rides on."""

    # Identification & timing
    ssrc: Optional[int] = None
    command_tag: Optional[int] = None
    cmd_cnt: Optional[int] = None
    gps_time: Optional[int] = None
    rtp_timesnap: Optional[int] = None
    description: Optional[str] = None
    status_interval: Optional[int] = None
    status_dest_socket: Optional[dict] = None
    rtp_pt: Optional[int] = None

    # Demodulator class
    demod_type: Optional[int] = None
    preset: Optional[str] = None

    # Tuning
    frequency: Optional[float] = None          # RADIO_FREQUENCY (tuned carrier)
    first_lo: Optional[float] = None           # FIRST_LO_FREQUENCY (also in frontend)
    second_lo: Optional[float] = None          # SECOND_LO_FREQUENCY
    shift: Optional[float] = None              # SHIFT_FREQUENCY
    doppler: Optional[float] = None            # DOPPLER_FREQUENCY
    doppler_rate: Optional[float] = None       # DOPPLER_FREQUENCY_RATE

    # Filter (primary)
    low_edge: Optional[float] = None           # LOW_EDGE (Hz)
    high_edge: Optional[float] = None          # HIGH_EDGE (Hz)
    kaiser_beta: Optional[float] = None
    filter_blocksize: Optional[int] = None
    filter_fir_length: Optional[int] = None
    filter_drops: Optional[int] = None
    noise_bw: Optional[float] = None           # NOISE_BW

    # Squelch
    snr_squelch_enable: Optional[bool] = None  # SNR_SQUELCH
    squelch_open: Optional[float] = None       # dB
    squelch_close: Optional[float] = None      # dB

    # AGC / gain / levels
    agc_enable: Optional[bool] = None
    gain: Optional[float] = None               # linear→dB (GAIN)
    headroom: Optional[float] = None           # HEADROOM (dB)
    agc_hangtime: Optional[float] = None       # seconds
    agc_recovery_rate: Optional[float] = None  # dB/sec
    agc_threshold: Optional[float] = None      # dB
    output_level: Optional[float] = None       # dB (OUTPUT_LEVEL)
    baseband_power: Optional[float] = None     # dB
    noise_density: Optional[float] = None      # dB/Hz
    envelope: Optional[bool] = None

    # Output / RTP
    output_ssrc: Optional[int] = None
    output_samprate: Optional[int] = None
    output_channels: Optional[int] = None
    output_encoding: Optional[int] = None
    output_data_dest_socket: Optional[dict] = None
    output_data_source_socket: Optional[dict] = None
    output_ttl: Optional[int] = None
    output_samples: Optional[int] = None
    output_data_packets: Optional[int] = None
    output_metadata_packets: Optional[int] = None
    output_errors: Optional[int] = None
    maxdelay: Optional[int] = None

    # Options / modes
    independent_sideband: Optional[bool] = None
    lock: Optional[bool] = None                # LOCK (channel-level)
    lifetime: Optional[int] = None             # LIFETIME — frames until self-destruct
                                               # (0 = infinite; >0 decrements at the radiod
                                               # frame rate ≈ 50 Hz; a bare poll does NOT
                                               # extend it — only a command carrying a
                                               # LIFETIME tag refreshes the countdown)
    options: Optional[int] = None              # SETOPTS — demod option bitmask;
                                               # radiod encodes chan->options on
                                               # every status packet (audit F12)

    # Test points
    tp1: Optional[float] = None
    tp2: Optional[float] = None

    # Sub-structures
    pll: PllStatus = field(default_factory=PllStatus)
    fm: FmStatus = field(default_factory=FmStatus)
    spectrum: SpectrumStatus = field(default_factory=SpectrumStatus)
    filter2: Filter2Status = field(default_factory=Filter2Status)
    opus: OpusStatus = field(default_factory=OpusStatus)

    # Frontend embedded in the same packet
    frontend: FrontendStatus = field(default_factory=FrontendStatus)

    # --- Derived / convenience ---------------------------------------------

    @property
    def bandwidth(self) -> Optional[float]:
        if self.high_edge is None or self.low_edge is None:
            return None
        return abs(self.high_edge - self.low_edge)

    @property
    def snr(self) -> Optional[float]:
        """S/N in dB using baseband power minus noise integrated over the filter."""
        bw = self.bandwidth
        if bw is None or bw <= 0:
            return None
        if self.baseband_power is None or self.noise_density is None:
            return None
        try:
            noise_db = self.noise_density + 10 * math.log10(bw)
            s_plus_n = 10 ** (self.baseband_power / 10.0)
            n = 10 ** (noise_db / 10.0)
            if n <= 0:
                return None
            lin = s_plus_n / n - 1.0
            if lin <= 0:
                return None
            return 10 * math.log10(lin)
        except (ValueError, OverflowError):
            return None

    @property
    def snr_per_hz(self) -> Optional[float]:
        """S/N0 (dB-Hz): S/N normalized to 1 Hz noise bandwidth."""
        if self.baseband_power is None or self.noise_density is None:
            return None
        return self.baseband_power - self.noise_density

    @property
    def demod_name(self) -> str:
        if self.demod_type is None:
            return "?"
        return {
            DemodType.LINEAR_DEMOD: "Linear",
            DemodType.FM_DEMOD: "FM",
            DemodType.WFM_DEMOD: "WFM",
            DemodType.SPECT_DEMOD: "Spectrum",
            DemodType.SPECT2_DEMOD: "Spectrum2",
        }.get(self.demod_type, f"demod#{self.demod_type}")

    @property
    def encoding_name(self) -> str:
        if self.output_encoding is None:
            return "?"
        for name, val in vars(Encoding).items():
            if not name.startswith("_") and not callable(val) and val == self.output_encoding:
                return name
        return f"enc#{self.output_encoding}"

    def to_dict(self) -> Dict[str, Any]:
        """Flatten to a JSON-friendly dict (with nested sub-structures)."""
        return asdict(self)

    def get_field(self, path: str) -> Any:
        """Dotted-path accessor, e.g. 'pll.lock' or 'frontend.calibrate'."""
        obj: Any = self
        for part in path.split('.'):
            if isinstance(obj, dict):
                obj = obj.get(part)
            else:
                obj = getattr(obj, part, None)
            if obj is None:
                return None
        return obj

    def field_names(self) -> list[str]:
        """All dotted paths that are populated (non-None)."""
        out: list[str] = []

        def walk(prefix: str, dc: Any) -> None:
            for f in fields(dc):
                val = getattr(dc, f.name)
                if val is None:
                    continue
                path = f"{prefix}{f.name}"
                if hasattr(val, "__dataclass_fields__"):
                    walk(path + ".", val)
                else:
                    out.append(path)

        walk("", self)
        return out


# ---------------------------------------------------------------------------
# Decoder
# ---------------------------------------------------------------------------

def decode_status_packet(buffer: bytes) -> Optional[ChannelStatus]:
    """Decode a radiod status packet into a :class:`ChannelStatus`.

    Returns None if the buffer is not a status packet (first byte != 0).
    Unknown or UNUSED TLV tags are silently skipped.
    """
    # Imported here to avoid circular import at module load time.
    from .control import (
        decode_bool, decode_double, decode_float, decode_int,
        decode_int32, decode_int64, decode_socket, decode_string,
    )

    if not buffer or buffer[0] != 0:
        return None

    st = ChannelStatus()
    fe = st.frontend
    pll = st.pll
    fm = st.fm
    sp = st.spectrum
    f2 = st.filter2
    op = st.opus

    cp = 1
    n = len(buffer)
    while cp < n:
        t = buffer[cp]; cp += 1
        if t == StatusType.EOL:
            break
        if cp >= n:
            break
        optlen = buffer[cp]; cp += 1
        if optlen & 0x80:
            lol = optlen & 0x7f
            optlen = 0
            for _ in range(lol):
                if cp >= n:
                    break
                optlen = (optlen << 8) | buffer[cp]; cp += 1
        if cp + optlen > n:
            break
        data = buffer[cp:cp + optlen]
        cp += optlen

        # ---- Identification / timing ----
        if t == StatusType.COMMAND_TAG:
            st.command_tag = decode_int32(data, optlen)
        elif t == StatusType.CMD_CNT:
            st.cmd_cnt = decode_int(data, optlen)
        elif t == StatusType.GPS_TIME:
            st.gps_time = decode_int64(data, optlen)
        elif t == StatusType.RTP_TIMESNAP:
            st.rtp_timesnap = decode_int32(data, optlen)
        elif t == StatusType.DESCRIPTION:
            s = decode_string(data, optlen)
            st.description = s
            fe.description = s
        elif t == StatusType.STATUS_DEST_SOCKET:
            st.status_dest_socket = decode_socket(data, optlen)
        elif t == StatusType.STATUS_INTERVAL:
            st.status_interval = decode_int(data, optlen)
        elif t == StatusType.RTP_PT:
            st.rtp_pt = decode_int(data, optlen)

        # ---- Frontend (input side) ----
        elif t == StatusType.INPUT_SAMPRATE:
            fe.input_samprate = decode_int(data, optlen)
        elif t == StatusType.INPUT_SAMPLES:
            fe.input_samples = decode_int64(data, optlen)
        elif t == StatusType.AD_BITS_PER_SAMPLE:
            fe.ad_bits_per_sample = decode_int(data, optlen)
        elif t == StatusType.AD_OVER:
            fe.ad_over = decode_int64(data, optlen)
        elif t == StatusType.SAMPLES_SINCE_OVER:
            fe.samples_since_over = decode_int64(data, optlen)
        elif t == StatusType.FE_ISREAL:
            fe.isreal = decode_bool(data, optlen)
        elif t == StatusType.DIRECT_CONVERSION:
            fe.direct_conversion = decode_bool(data, optlen)
        elif t == StatusType.CALIBRATE:
            fe.calibrate = decode_double(data, optlen)
        elif t == StatusType.FIRST_LO_FREQUENCY:
            v = decode_double(data, optlen)
            fe.first_lo = v
            st.first_lo = v
        elif t == StatusType.SECOND_LO_FREQUENCY:
            st.second_lo = decode_double(data, optlen)
        elif t == StatusType.LOCK:
            b = decode_bool(data, optlen)
            fe.lock = b
            st.lock = b
        elif t == StatusType.FE_LOW_EDGE:
            fe.fe_low_edge = decode_float(data, optlen)
        elif t == StatusType.FE_HIGH_EDGE:
            fe.fe_high_edge = decode_float(data, optlen)
        elif t == StatusType.LNA_GAIN:
            fe.lna_gain = decode_int(data, optlen)
        elif t == StatusType.MIXER_GAIN:
            fe.mixer_gain = decode_int(data, optlen)
        elif t == StatusType.IF_GAIN:
            fe.if_gain = decode_int(data, optlen)
        elif t == StatusType.RF_GAIN:
            fe.rf_gain = decode_float(data, optlen)
        elif t == StatusType.RF_ATTEN:
            fe.rf_atten = decode_float(data, optlen)
        elif t == StatusType.RF_AGC:
            fe.rf_agc = decode_bool(data, optlen)
        elif t == StatusType.RF_LEVEL_CAL:
            fe.rf_level_cal = decode_float(data, optlen)
        elif t == StatusType.IF_POWER:
            fe.if_power = decode_float(data, optlen)
        elif t == StatusType.DC_I_OFFSET:
            fe.dc_i_offset = decode_float(data, optlen)
        elif t == StatusType.DC_Q_OFFSET:
            fe.dc_q_offset = decode_float(data, optlen)
        elif t == StatusType.IQ_IMBALANCE:
            fe.iq_imbalance = decode_float(data, optlen)
        elif t == StatusType.IQ_PHASE:
            fe.iq_phase = decode_float(data, optlen)

        # ---- Channel tuning ----
        elif t == StatusType.RADIO_FREQUENCY:
            st.frequency = decode_double(data, optlen)
        elif t == StatusType.SHIFT_FREQUENCY:
            st.shift = decode_double(data, optlen)
        elif t == StatusType.DOPPLER_FREQUENCY:
            st.doppler = decode_double(data, optlen)
        elif t == StatusType.DOPPLER_FREQUENCY_RATE:
            st.doppler_rate = decode_double(data, optlen)

        # ---- Demod class / preset ----
        elif t == StatusType.DEMOD_TYPE:
            st.demod_type = decode_int(data, optlen)
        elif t == StatusType.PRESET:
            st.preset = decode_string(data, optlen)

        # ---- Filter ----
        elif t == StatusType.LOW_EDGE:
            st.low_edge = decode_float(data, optlen)
        elif t == StatusType.HIGH_EDGE:
            st.high_edge = decode_float(data, optlen)
        elif t == StatusType.KAISER_BETA:
            st.kaiser_beta = decode_float(data, optlen)
        elif t == StatusType.FILTER_BLOCKSIZE:
            st.filter_blocksize = decode_int(data, optlen)
        elif t == StatusType.FILTER_FIR_LENGTH:
            st.filter_fir_length = decode_int(data, optlen)
        elif t == StatusType.FILTER_DROPS:
            st.filter_drops = decode_int(data, optlen)
        elif t == StatusType.NOISE_BW:
            st.noise_bw = decode_float(data, optlen)

        # ---- Filter2 ----
        elif t == StatusType.FILTER2:
            f2.blocking = decode_int(data, optlen)
        elif t == StatusType.FILTER2_BLOCKSIZE:
            f2.blocksize = decode_int(data, optlen)
        elif t == StatusType.FILTER2_FIR_LENGTH:
            f2.fir_length = decode_int(data, optlen)
        elif t == StatusType.FILTER2_KAISER_BETA:
            f2.kaiser_beta = decode_float(data, optlen)

        # ---- Squelch ----
        elif t == StatusType.SNR_SQUELCH:
            st.snr_squelch_enable = decode_bool(data, optlen)
        elif t == StatusType.SQUELCH_OPEN:
            st.squelch_open = decode_float(data, optlen)
        elif t == StatusType.SQUELCH_CLOSE:
            st.squelch_close = decode_float(data, optlen)

        # ---- AGC / levels ----
        elif t == StatusType.AGC_ENABLE:
            st.agc_enable = decode_bool(data, optlen)
        elif t == StatusType.GAIN:
            st.gain = decode_float(data, optlen)
        elif t == StatusType.HEADROOM:
            st.headroom = decode_float(data, optlen)
        elif t == StatusType.AGC_HANGTIME:
            st.agc_hangtime = decode_float(data, optlen)
        elif t == StatusType.AGC_RECOVERY_RATE:
            st.agc_recovery_rate = decode_float(data, optlen)
        elif t == StatusType.AGC_THRESHOLD:
            st.agc_threshold = decode_float(data, optlen)
        elif t == StatusType.OUTPUT_LEVEL:
            st.output_level = decode_float(data, optlen)
        elif t == StatusType.BASEBAND_POWER:
            st.baseband_power = decode_float(data, optlen)
        elif t == StatusType.NOISE_DENSITY:
            st.noise_density = decode_float(data, optlen)
        elif t == StatusType.ENVELOPE:
            st.envelope = decode_bool(data, optlen)

        # ---- PLL ----
        elif t == StatusType.PLL_ENABLE:
            pll.enable = decode_bool(data, optlen)
        elif t == StatusType.PLL_LOCK:
            pll.lock = decode_bool(data, optlen)
        elif t == StatusType.PLL_SQUARE:
            pll.square = decode_bool(data, optlen)
        elif t == StatusType.PLL_PHASE:
            pll.phase = decode_float(data, optlen)
        elif t == StatusType.PLL_BW:
            pll.bw = decode_float(data, optlen)
        elif t == StatusType.PLL_SNR:
            pll.snr = decode_float(data, optlen)
        elif t == StatusType.PLL_WRAPS:
            pll.wraps = decode_int64(data, optlen)
        elif t == StatusType.FREQ_OFFSET:
            pll.freq_offset = decode_float(data, optlen)

        # ---- FM ----
        elif t == StatusType.PEAK_DEVIATION:
            fm.peak_deviation = decode_float(data, optlen)
        elif t == StatusType.FM_SNR:
            fm.fm_snr = decode_float(data, optlen)
        elif t == StatusType.PL_TONE:
            fm.pl_tone = decode_float(data, optlen)
        elif t == StatusType.PL_DEVIATION:
            fm.pl_deviation = decode_float(data, optlen)
        elif t == StatusType.DEEMPH_TC:
            fm.deemph_tc = decode_float(data, optlen)
        elif t == StatusType.DEEMPH_GAIN:
            fm.deemph_gain = decode_float(data, optlen)
        elif t == StatusType.THRESH_EXTEND:
            fm.threshold_extend = decode_bool(data, optlen)

        # ---- Spectrum ----
        elif t == StatusType.SPECTRUM_AVG:
            sp.avg = decode_int(data, optlen)
        elif t == StatusType.SPECTRUM_BASE:
            sp.base = decode_float(data, optlen)
        elif t == StatusType.SPECTRUM_STEP:
            sp.step = decode_float(data, optlen)
        elif t == StatusType.SPECTRUM_SHAPE:
            sp.shape = decode_float(data, optlen)
        elif t == StatusType.SPECTRUM_FFT_N:
            sp.fft_n = decode_int(data, optlen)
        elif t == StatusType.SPECTRUM_OVERLAP:
            sp.overlap = decode_float(data, optlen)
        elif t == StatusType.RESOLUTION_BW:
            sp.resolution_bw = decode_float(data, optlen)
        elif t == StatusType.BIN_COUNT:
            sp.bin_count = decode_int(data, optlen)
        elif t == StatusType.CROSSOVER:
            sp.crossover = decode_float(data, optlen)
        elif t == StatusType.WINDOW_TYPE:
            sp.window_type = decode_int(data, optlen)

        # ---- Independent sideband / lock / lifetime ----
        elif t == StatusType.INDEPENDENT_SIDEBAND:
            st.independent_sideband = decode_bool(data, optlen)
        elif t == StatusType.LIFETIME:
            st.lifetime = decode_int(data, optlen)
        elif t == StatusType.SETOPTS:
            st.options = decode_int64(data, optlen)

        # ---- Test points ----
        elif t == StatusType.TP1:
            st.tp1 = decode_float(data, optlen)
        elif t == StatusType.TP2:
            st.tp2 = decode_float(data, optlen)

        # ---- Output / RTP ----
        elif t == StatusType.OUTPUT_SSRC:
            v = decode_int32(data, optlen)
            st.output_ssrc = v
            st.ssrc = v
        elif t == StatusType.OUTPUT_SAMPRATE:
            st.output_samprate = decode_int(data, optlen)
        elif t == StatusType.OUTPUT_CHANNELS:
            st.output_channels = decode_int(data, optlen)
        elif t == StatusType.OUTPUT_ENCODING:
            st.output_encoding = decode_int(data, optlen)
        elif t == StatusType.OUTPUT_DATA_DEST_SOCKET:
            st.output_data_dest_socket = decode_socket(data, optlen)
        elif t == StatusType.OUTPUT_DATA_SOURCE_SOCKET:
            st.output_data_source_socket = decode_socket(data, optlen)
        elif t == StatusType.OUTPUT_TTL:
            st.output_ttl = decode_int(data, optlen)
        elif t == StatusType.OUTPUT_SAMPLES:
            st.output_samples = decode_int64(data, optlen)
        elif t == StatusType.OUTPUT_DATA_PACKETS:
            st.output_data_packets = decode_int64(data, optlen)
        elif t == StatusType.OUTPUT_METADATA_PACKETS:
            st.output_metadata_packets = decode_int64(data, optlen)
        elif t == StatusType.OUTPUT_ERRORS:
            st.output_errors = decode_int64(data, optlen)
        elif t == StatusType.MAXDELAY:
            st.maxdelay = decode_int(data, optlen)

        # ---- Opus ----
        elif t == StatusType.OPUS_BIT_RATE:
            op.bit_rate = decode_int(data, optlen)
        elif t == StatusType.OPUS_DTX:
            op.dtx = decode_bool(data, optlen)
        elif t == StatusType.OPUS_APPLICATION:
            op.application = decode_int(data, optlen)
        elif t == StatusType.OPUS_BANDWIDTH:
            op.bandwidth = decode_int(data, optlen)
        elif t == StatusType.OPUS_FEC:
            op.fec = decode_int(data, optlen)

        # ---- Spectrum bin vectors ----
        elif t == StatusType.BIN_DATA:
            # SPECT_DEMOD: vector of float32 power values (big-endian IEEE 754)
            n_bins = optlen // 4
            if n_bins > 0:
                sp.bin_data = np.array(
                    struct.unpack_from("!" + "f" * n_bins, data),
                    dtype=np.float32,
                )
        elif t == StatusType.BIN_BYTE_DATA:
            # SPECT2_DEMOD: vector of uint8 quantised log-power values
            if optlen > 0:
                sp.bin_byte_data = np.frombuffer(data[:optlen], dtype=np.uint8).copy()

        # Unused tags — skip silently.

    return st


__all__ = [
    "FrontendStatus",
    "ChannelStatus",
    "PllStatus",
    "FmStatus",
    "SpectrumStatus",
    "Filter2Status",
    "OpusStatus",
    "decode_status_packet",
]
