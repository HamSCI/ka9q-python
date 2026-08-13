"""MultiStream restore must re-send every creation-time setting (audit F1, P0).

_attempt_restore() used to call ensure_channel() with only 5 of the 10
identity/config fields captured at add_channel(); agc_enable/gain also feed
allocate_ssrc()'s hash, so a restore of a channel created with non-default
gain/AGC computed a *different SSRC* than the one being restored.  Asserted
at the RadiodControl.ensure_channel boundary, mirroring
tests/test_lifetime.py::TestMultiStreamLifetime.
"""
from unittest.mock import MagicMock

from ka9q.discovery import ChannelInfo
from ka9q.multi_stream import MultiStream

ADD_KWARGS = dict(
    frequency_hz=14_074_000.0,
    preset="usb",
    sample_rate=12000,
    encoding=4,          # F32LE requested
    agc_enable=1,
    gain=-10.0,
    low_edge=-250.0,
    high_edge=+250.0,
    kaiser_beta=11.0,
    lifetime=6000,
)


def _make_multi(ssrc=12345, granted_encoding=4):
    control = MagicMock()
    control.ensure_channel.return_value = ChannelInfo(
        ssrc=ssrc,
        preset="usb",
        sample_rate=12000,
        frequency=14_074_000.0,
        snr=0.0,
        multicast_address="239.1.2.3",
        port=5004,
        encoding=granted_encoding,
    )
    return MultiStream(control=control), control


def test_slot_stores_all_creation_settings():
    multi, _ = _make_multi()
    multi.add_channel(**ADD_KWARGS)
    slot = multi._slots[12345]
    assert slot.agc_enable == 1
    assert slot.gain == -10.0
    assert slot.low_edge == -250.0
    assert slot.high_edge == +250.0
    assert slot.kaiser_beta == 11.0
    assert slot.requested_encoding == 4
    assert slot.lifetime == 6000


def test_restore_resends_every_field_creation_sent():
    multi, control = _make_multi()
    multi.add_channel(**ADD_KWARGS)
    add_call = dict(control.ensure_channel.call_args.kwargs)
    add_call.pop("timeout")            # restore has no ACK-wait override
    slot = multi._slots[12345]
    slot.dropped = True
    control.ensure_channel.reset_mock()

    multi._attempt_restore(12345, slot)

    restore_call = dict(control.ensure_channel.call_args.kwargs)
    assert restore_call == add_call, (
        "restore must re-send exactly what add_channel sent "
        f"(symmetric difference of keys: {set(add_call) ^ set(restore_call)})"
    )


def test_restore_resends_requested_not_granted_encoding():
    # radiod downgraded F32(4) -> S16(1) at creation; the restore must
    # re-request 4 (what creation requested) so allocate_ssrc() computes
    # the same SSRC it computed at creation.
    multi, control = _make_multi(granted_encoding=1)
    multi.add_channel(**ADD_KWARGS)
    slot = multi._slots[12345]
    assert slot.encoding == 1              # granted encoding drives parsing
    slot.dropped = True
    control.ensure_channel.reset_mock()

    multi._attempt_restore(12345, slot)

    assert control.ensure_channel.call_args.kwargs["encoding"] == 4
    # and the slot keeps decoding with the (re-)granted wire encoding
    assert slot.encoding == 1
