"""The parameter vector, not the preset label, says how to read a stream.

Phil Karn, 2026-09-21, on making PRESET write-only:

    I had always intended the individual parameters to be definitive, with a
    "preset" just being a convenient shorthand for loading a specific
    predefined set of them into the channel. ... I even renamed the field
    from "mode" to "preset" to emphasize this, so that people wouldn't think
    that the channel had an operating mode.

    The parameter vector {demod_type, channels, frequency, offset, filter
    high/low, etc} gives you everything you need to interpret the data
    stream from a channel.

    Eg, you send PRESET USB followed by retuning the filters to -3000, -50,
    you actually get the lower sideband. So now the preset actively lies
    about the channel.

`stream.py` decided complex-versus-real parsing from the preset string, so a
preset that stops arriving (or that lies) mis-frames every sample with
nothing raised.  These tests pin the derivation to the parameters instead.

⛔ The trap is WFM.  From ka9q-radio docs/table.csv:

    44,DEMOD_TYPE,      0 = linear (default), 1 = FM, 2 = WFM/Stereo, 3 = spectrum
    45,OUTPUT_CHANNELS, "1 or 2 in Linear and WFM, 1 in FM"

Two output channels means COMPLEX under Linear and STEREO AUDIO under WFM.
So `output_channels == 2` alone is not the test; it only means complex when
the demodulator is linear.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ka9q.control import decode_status_dict  # noqa: E402
from ka9q.discovery import ChannelInfo  # noqa: E402
from ka9q.stream import _derive_is_iq  # noqa: E402
from ka9q.types import DemodType, StatusType  # noqa: E402


LINEAR = int(DemodType.LINEAR_DEMOD)
FM = int(DemodType.FM_DEMOD)
WFM = int(DemodType.WFM_DEMOD)
SPECT = int(DemodType.SPECT_DEMOD)


def _tlv(type_val: int, payload: bytes) -> bytes:
    return bytes([type_val, len(payload)]) + payload


def _packet(*tlvs: bytes) -> bytes:
    # leading status-type byte, then TLVs, then EOL
    return b"\x00" + b"".join(tlvs) + b"\x00"


class TestDecoderCarriesTheVector:
    """decode_status_dict feeds ChannelInfo; it dropped both fields."""

    def test_demod_type_is_decoded(self):
        pkt = _packet(_tlv(int(StatusType.DEMOD_TYPE), bytes([LINEAR])))
        assert decode_status_dict(pkt).get("demod_type") == LINEAR

    def test_output_channels_is_decoded(self):
        pkt = _packet(_tlv(int(StatusType.OUTPUT_CHANNELS), bytes([2])))
        assert decode_status_dict(pkt).get("output_channels") == 2

    def test_a_packet_without_them_still_decodes(self):
        """Old radiod, or a partial status — absence must not raise."""
        d = decode_status_dict(_packet())
        assert d.get("demod_type") is None
        assert d.get("output_channels") is None


class TestChannelInfoCarriesTheVector:

    def test_fields_exist_and_default_to_none(self):
        ci = ChannelInfo(ssrc=1, preset="iq", sample_rate=96000,
                         frequency=45e6, snr=0.0,
                         multicast_address="239.0.0.1", port=5004)
        assert ci.demod_type is None
        assert ci.output_channels is None

    def test_they_can_be_set(self):
        ci = ChannelInfo(ssrc=1, preset="iq", sample_rate=96000,
                         frequency=45e6, snr=0.0,
                         multicast_address="239.0.0.1", port=5004,
                         demod_type=LINEAR, output_channels=2)
        assert (ci.demod_type, ci.output_channels) == (LINEAR, 2)


class TestDeriveIsIq:
    """The whole point: decide from parameters, fall back to the label."""

    def test_linear_with_two_channels_is_complex(self):
        assert _derive_is_iq(LINEAR, 2, "iq") is True

    def test_linear_with_one_channel_is_real(self):
        """SSB, CW, AM — all linear, all real output."""
        assert _derive_is_iq(LINEAR, 1, "usb") is False

    def test_wfm_with_two_channels_is_STEREO_not_complex(self):
        """⛔ The trap. Two channels here means left and right, not I and Q."""
        assert _derive_is_iq(WFM, 2, "wfm") is False

    def test_fm_is_real(self):
        assert _derive_is_iq(FM, 1, "fm") is False

    def test_spectrum_keeps_todays_behaviour(self):
        """Not a judgement about spectrum framing — just don't change it
        silently while fixing something else."""
        assert _derive_is_iq(SPECT, 1, "spectrum") is True

    def test_falls_back_to_the_preset_when_parameters_absent(self):
        """Old radiod, or any path that did not populate the vector."""
        assert _derive_is_iq(None, None, "iq") is True
        assert _derive_is_iq(None, None, "usb") is False

    def test_the_parameters_win_over_a_lying_preset(self):
        """Phil's example: PRESET USB then filters -3000,-50 gives LSB.
        A preset that no longer describes the channel must not decide."""
        assert _derive_is_iq(LINEAR, 2, "usb") is True

    def test_a_missing_preset_does_not_raise(self):
        """When the echo stops, the label may be None or empty."""
        assert _derive_is_iq(LINEAR, 2, None) is True
        assert _derive_is_iq(None, None, None) is False
        assert _derive_is_iq(None, None, "") is False
