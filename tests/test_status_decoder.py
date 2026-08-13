"""Unit tests for the typed status decoder."""
from __future__ import annotations

import pytest

from ka9q.control import (
    encode_double, encode_float, encode_int, encode_int64, encode_string,
    encode_eol,
)
from ka9q.status import ChannelStatus, FrontendStatus, decode_status_packet
from ka9q.types import StatusType


def _build_packet(*fields) -> bytes:
    buf = bytearray()
    buf.append(0)  # status response type
    for kind, tag, value in fields:
        if kind == "int":
            encode_int(buf, tag, value)
        elif kind == "int64":
            encode_int64(buf, tag, value)
        elif kind == "double":
            encode_double(buf, tag, value)
        elif kind == "float":
            encode_float(buf, tag, value)
        elif kind == "string":
            encode_string(buf, tag, value)
        elif kind == "bool":
            encode_int(buf, tag, 1 if value else 0)
        else:
            raise ValueError(kind)
    encode_eol(buf)
    return bytes(buf)


def test_decode_empty_and_invalid():
    assert decode_status_packet(b"") is None
    assert decode_status_packet(b"\x01stuff") is None  # not a status type


def test_decode_basic_channel():
    pkt = _build_packet(
        ("int", StatusType.OUTPUT_SSRC, 12345),
        ("int", StatusType.COMMAND_TAG, 999),
        ("double", StatusType.RADIO_FREQUENCY, 14.074e6),
        ("string", StatusType.PRESET, "usb"),
        ("int", StatusType.DEMOD_TYPE, 0),  # linear
        ("float", StatusType.LOW_EDGE, 200.0),
        ("float", StatusType.HIGH_EDGE, 2700.0),
        ("bool", StatusType.AGC_ENABLE, True),
        ("float", StatusType.GAIN, 12.0),
    )
    st = decode_status_packet(pkt)
    assert st is not None
    assert st.ssrc == 12345
    assert st.command_tag == 999
    assert st.frequency == pytest.approx(14.074e6)
    assert st.preset == "usb"
    assert st.demod_type == 0
    assert st.demod_name == "Linear"
    assert st.low_edge == pytest.approx(200.0)
    assert st.high_edge == pytest.approx(2700.0)
    assert st.bandwidth == pytest.approx(2500.0)
    assert st.agc_enable is True
    assert st.gain == pytest.approx(12.0)


def test_decode_frontend_and_gpsdo():
    pkt = _build_packet(
        ("string", StatusType.DESCRIPTION, "rx888 with GPSDO"),
        ("int", StatusType.INPUT_SAMPRATE, 64_800_000),
        ("int", StatusType.AD_BITS_PER_SAMPLE, 16),
        ("bool", StatusType.FE_ISREAL, True),
        ("double", StatusType.CALIBRATE, 1.2e-6),
        ("double", StatusType.FIRST_LO_FREQUENCY, 14.050e6),
        ("bool", StatusType.LOCK, False),
        ("float", StatusType.FE_LOW_EDGE, -500_000.0),
        ("float", StatusType.FE_HIGH_EDGE, 500_000.0),
        ("int", StatusType.LNA_GAIN, 20),
        ("int", StatusType.MIXER_GAIN, 10),
        ("int", StatusType.IF_GAIN, 5),
        ("float", StatusType.RF_LEVEL_CAL, -50.0),
        ("float", StatusType.IF_POWER, -3.0),
        ("int64", StatusType.AD_OVER, 0),
    )
    st = decode_status_packet(pkt)
    fe: FrontendStatus = st.frontend
    assert fe.description == "rx888 with GPSDO"
    assert fe.input_samprate == 64_800_000
    assert fe.ad_bits_per_sample == 16
    assert fe.isreal is True
    assert fe.calibrate == pytest.approx(1.2e-6)
    assert fe.calibrate_ppm == pytest.approx(1.2)
    assert fe.gpsdo_reference_hz == pytest.approx(10_000_012.0)
    assert fe.first_lo == pytest.approx(14.050e6)
    # First-LO mirrored onto channel too (what `control` displays).
    assert st.first_lo == pytest.approx(14.050e6)
    assert fe.lock is False
    assert st.lock is False
    assert fe.fe_low_edge == pytest.approx(-500_000.0)
    assert fe.fe_high_edge == pytest.approx(500_000.0)
    assert fe.lna_gain == 20
    assert fe.rf_level_cal == pytest.approx(-50.0)
    assert fe.if_power == pytest.approx(-3.0)
    # input_power_dbm = if_power + rf_level_cal - (lna+mix+if) = -3 - 50 - 35
    assert fe.input_power_dbm == pytest.approx(-88.0)


def test_decode_pll_fm_spectrum():
    pkt = _build_packet(
        ("int", StatusType.OUTPUT_SSRC, 1),
        ("bool", StatusType.PLL_ENABLE, True),
        ("bool", StatusType.PLL_LOCK, True),
        ("float", StatusType.PLL_BW, 100.0),
        ("float", StatusType.PLL_SNR, 20.0),
        ("float", StatusType.FREQ_OFFSET, 1.5),
        ("float", StatusType.PEAK_DEVIATION, 2500.0),
        ("float", StatusType.FM_SNR, 15.0),
        ("float", StatusType.PL_TONE, 100.0),
        ("int", StatusType.SPECTRUM_FFT_N, 1024),
        ("int", StatusType.BIN_COUNT, 512),
        ("float", StatusType.RESOLUTION_BW, 6250.0),
        ("int", StatusType.WINDOW_TYPE, 0),
    )
    st = decode_status_packet(pkt)
    assert st.pll.enable is True
    assert st.pll.lock is True
    assert st.pll.bw == pytest.approx(100.0)
    assert st.pll.snr == pytest.approx(20.0)
    assert st.pll.freq_offset == pytest.approx(1.5)
    assert st.fm.peak_deviation == pytest.approx(2500.0)
    assert st.fm.fm_snr == pytest.approx(15.0)
    assert st.fm.pl_tone == pytest.approx(100.0)
    assert st.spectrum.fft_n == 1024
    assert st.spectrum.bin_count == 512
    assert st.spectrum.resolution_bw == pytest.approx(6250.0)
    assert st.spectrum.window_type == 0


def test_field_accessors_and_snr():
    pkt = _build_packet(
        ("int", StatusType.OUTPUT_SSRC, 42),
        ("double", StatusType.RADIO_FREQUENCY, 10.0e6),
        ("double", StatusType.CALIBRATE, -2e-7),
        ("float", StatusType.LOW_EDGE, -1500.0),
        ("float", StatusType.HIGH_EDGE, 1500.0),
        ("float", StatusType.BASEBAND_POWER, -40.0),
        ("float", StatusType.NOISE_DENSITY, -170.0),
    )
    st = decode_status_packet(pkt)
    assert st.get_field("ssrc") == 42
    assert st.get_field("frontend.calibrate") == pytest.approx(-2e-7)
    assert st.get_field("pll.lock") is None
    names = st.field_names()
    assert "ssrc" in names
    assert "frequency" in names
    assert "frontend.calibrate" in names
    # S/N0 = baseband - noise_density = -40 - (-170) = 130 dB-Hz
    assert st.snr_per_hz == pytest.approx(130.0)
    # S/N ≈ baseband - (noise_density + 10log10(3000)) ≈ -40 - (-170 + 34.77) ≈ 95.2
    assert st.snr == pytest.approx(95.23, abs=0.1)


def test_decode_setopts_options_bitmask():
    """radiod encode_int64()s chan->options under SETOPTS on every status
    packet; it must land in ChannelStatus.options (audit F12)."""
    pkt = _build_packet(
        ("int", StatusType.OUTPUT_SSRC, 42),
        ("int64", StatusType.SETOPTS, 0b1010),
    )
    st = decode_status_packet(pkt)
    assert st is not None
    assert st.options == 0b1010


def test_options_none_when_absent():
    pkt = _build_packet(("int", StatusType.OUTPUT_SSRC, 42))
    st = decode_status_packet(pkt)
    assert st.options is None
