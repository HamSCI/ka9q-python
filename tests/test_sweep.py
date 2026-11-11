"""Sweep (dechirp) TLVs 118-121 and the epoch pair 216-217."""
import struct
import warnings

import pytest

from ka9q.control import encode_double, encode_int64
from ka9q.status import decode_status_packet
from ka9q.types import StatusType
from tests.test_filter_edges import _bare_control, _capture_send


def _walk(buf: bytes) -> dict:
    """TLV walk -> {tag: raw value bytes}; keeps repeats out (last wins)."""
    out, cp = {}, 1
    while cp < len(buf):
        t = buf[cp]
        cp += 1
        if t == StatusType.EOL:
            break
        n = buf[cp]
        cp += 1
        out[t] = buf[cp:cp + n]
        cp += n
    return out


def _dbl(raw: bytes) -> float:
    return struct.unpack(">d", raw.rjust(8, b"\0"))[0]


def test_set_sweep_sends_four_tlvs_with_zero_start():
    c = _bare_control()
    sent = _capture_send(c)
    c.set_sweep(12345, -25733.913, 1.0, duration_s=0.5)
    tlv = _walk(sent[0])
    for tag in (StatusType.SWEEP_RATE, StatusType.SWEEP_DURATION,
                StatusType.SWEEP_PERIOD, StatusType.SWEEP_START):
        assert tag in tlv
    assert _dbl(tlv[StatusType.SWEEP_RATE]) == -25733.913
    assert _dbl(tlv[StatusType.SWEEP_PERIOD]) == 1.0
    assert _dbl(tlv[StatusType.SWEEP_DURATION]) == 0.5
    assert tlv[StatusType.SWEEP_START] == b""   # explicit zero, zero length
    assert _dbl(tlv[StatusType.SWEEP_START]) == 0.0


def test_set_sweep_duration_defaults_to_period():
    c = _bare_control()
    sent = _capture_send(c)
    c.set_sweep(12345, 99259.4, 0.5)
    tlv = _walk(sent[0])
    assert _dbl(tlv[StatusType.SWEEP_DURATION]) == 0.5


def test_decode_epoch_pair():
    buf = bytearray([0])
    encode_double(buf, StatusType.SWEEP_RATE, -25733.913)
    encode_double(buf, StatusType.SWEEP_DURATION, 1.0)
    encode_double(buf, StatusType.SWEEP_PERIOD, 1.0)
    encode_double(buf, StatusType.SWEEP_START, 0.25)
    encode_int64(buf, StatusType.SWEEP_EPOCH_SAMPLE, 123456789012)
    encode_double(buf, StatusType.SWEEP_EPOCH_FRAC, 0.375)
    buf.append(StatusType.EOL)
    st = decode_status_packet(bytes(buf))
    assert st.sweep_rate == -25733.913
    assert st.sweep_duration == 1.0
    assert st.sweep_period == 1.0
    assert st.sweep_start == 0.25
    assert st.sweep_epoch_sample == 123456789012
    assert st.sweep_epoch_frac == 0.375


def test_decode_epoch_sample_is_signed():
    buf = bytearray([0])
    encode_int64(buf, StatusType.SWEEP_EPOCH_SAMPLE, (-5000) & (2**64 - 1))
    buf.append(StatusType.EOL)
    st = decode_status_packet(bytes(buf))
    assert st.sweep_epoch_sample == -5000


def test_decode_no_sweep_fields_stay_none():
    st = decode_status_packet(bytes([0, StatusType.EOL]))
    assert st.sweep_rate is None and st.sweep_epoch_sample is None


def test_set_doppler_is_retired():
    c = _bare_control()
    sent = _capture_send(c)
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        with pytest.raises(RuntimeError, match="use set_sweep"):
            c.set_doppler(12345, 100.0, 1.0)
    assert any(issubclass(x.category, DeprecationWarning) for x in w)
    assert sent == []


@pytest.mark.parametrize("kw", [
    dict(rate_hz_per_s=float("nan"), period_s=1.0),
    dict(rate_hz_per_s=1.0, period_s=float("inf")),
    dict(rate_hz_per_s=1.0, period_s=0.0),
    dict(rate_hz_per_s=1.0, period_s=-1.0),
    dict(rate_hz_per_s=1.0, period_s=1e6 + 1),
    dict(rate_hz_per_s=1.0, period_s=1.0, duration_s=2.0),
    dict(rate_hz_per_s=1.0, period_s=1.0, duration_s=-0.1),
    dict(rate_hz_per_s=1.0, period_s=1.0, start_s=float("nan")),
])
def test_set_sweep_validates(kw):
    from ka9q.exceptions import ValidationError
    c = _bare_control()
    sent = _capture_send(c)
    with pytest.raises(ValidationError):
        c.set_sweep(12345, **kw)
    assert sent == []


def test_set_sweep_zero_rate_clears_without_period():
    c = _bare_control()
    sent = _capture_send(c)
    c.set_sweep(12345, 0.0, 0.0)
    assert len(sent) == 1


def test_pending_upstream_entries_exist_in_StatusType():
    import importlib.util, pathlib
    spec = importlib.util.spec_from_file_location(
        "sync_types", pathlib.Path(__file__).parent.parent / "scripts" / "sync_types.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    for name, num, _ in m.PENDING_UPSTREAM_STATUS_TABLE:
        assert getattr(StatusType, name) == num


def test_generator_emits_pending_entries():
    import importlib.util, pathlib
    spec = importlib.util.spec_from_file_location(
        "sync_types", pathlib.Path(__file__).parent.parent / "scripts" / "sync_types.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    out = m.generate_types_py([("EOL", 0, "")], [], [], [], "0" * 40)
    for name, num, _ in m.PENDING_UPSTREAM_STATUS_TABLE:
        assert f"    {name} = {num}" in out


def test_check_flags_upstream_reuse_of_pending_number():
    import importlib.util, pathlib
    spec = importlib.util.spec_from_file_location(
        "sync_types", pathlib.Path(__file__).parent.parent / "scripts" / "sync_types.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    issues = m._check_enum("StatusType", [("SOMETHING_ELSE", 216, "")],
                           {"SOMETHING_ELSE": 216, "SWEEP_EPOCH_FRAC": 216})
    assert any("REUSES" in i for i in issues)
