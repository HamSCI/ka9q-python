"""Tests for low_edge / high_edge / kaiser_beta plumbing on
create_channel, ensure_channel, and MultiStream.add_channel.

The SDR clients we ship (hf-timestd's BPSK PPS calibrator, planned
SuperDARN and CODAR-sounder receivers) all need filter passbands that
differ from the preset's defaults — the iq preset defaults to ±5 kHz,
which is wrong for both narrow (BPSK CW carrier) and wide (radar chirp)
applications. This module verifies the plumbing without requiring a
live radiod.

Strategy mirrors test_lifetime.py: replace send_command / underlying
layers with mocks, then either walk the TLV bytes for tag presence or
inspect the mock call kwargs.
"""

import threading
import time
from unittest.mock import MagicMock

import pytest

from ka9q.control import CMD, RadiodControl, StatusType


def _bare_control() -> RadiodControl:
    """Construct a RadiodControl without touching the network.

    Mirrors the helper in test_lifetime.py; kept in-module to avoid
    cross-test imports.
    """
    c = RadiodControl.__new__(RadiodControl)
    c.status_address = "test.local"
    c.socket = MagicMock()
    c.dest_addr = ("239.1.2.3", 5006)
    c._socket_lock = threading.RLock()
    c.max_commands_per_sec = 100
    c._command_count = 0
    c._command_window_start = time.time()
    c._rate_limit_lock = threading.Lock()
    c.metrics = MagicMock()
    c.client_id = None
    return c


def _capture_send(control: RadiodControl) -> list[bytes]:
    sent: list[bytes] = []
    control.send_command = MagicMock(side_effect=lambda buf: sent.append(bytes(buf)))
    return sent


def _has_tag(buf: bytes, tag: int) -> bool:
    """Walk the TLV buffer looking for `tag`.

    Same byte-walker as test_lifetime._has_lifetime_tag, generalized.
    Skips the 1-byte CMD/STATUS prefix; honours the 0x80 extended-length
    encoding for >127-byte values (none of the tags we test here use it,
    but the walker has to handle them to skip past correctly).
    """
    cp = 1
    while cp < len(buf):
        t = buf[cp]
        cp += 1
        if t == StatusType.EOL:
            break
        if cp >= len(buf):
            break
        optlen = buf[cp]
        cp += 1
        if optlen & 0x80:
            n = optlen & 0x7F
            optlen = 0
            for _ in range(n):
                if cp >= len(buf):
                    return False
                optlen = (optlen << 8) | buf[cp]
                cp += 1
        if t == tag:
            return True
        cp += optlen
    return False


class TestCreateChannelFilterEdges:
    """create_channel(low_edge=, high_edge=, kaiser_beta=) emits the
    corresponding TLV tags in the channel-create command packet."""

    def test_low_edge_tag_emitted(self):
        control = _bare_control()
        sent = _capture_send(control)
        control.create_channel(
            frequency_hz=10e6, preset="iq", sample_rate=16000,
            low_edge=-500.0,
        )
        # First buffer is the create packet; later ones may be encoding etc.
        assert _has_tag(sent[0], StatusType.LOW_EDGE)

    def test_high_edge_tag_emitted(self):
        control = _bare_control()
        sent = _capture_send(control)
        control.create_channel(
            frequency_hz=10e6, preset="iq", sample_rate=16000,
            high_edge=+500.0,
        )
        assert _has_tag(sent[0], StatusType.HIGH_EDGE)

    def test_kaiser_beta_tag_emitted(self):
        control = _bare_control()
        sent = _capture_send(control)
        control.create_channel(
            frequency_hz=10e6, preset="iq", sample_rate=16000,
            kaiser_beta=11.0,
        )
        assert _has_tag(sent[0], StatusType.KAISER_BETA)

    def test_omitted_means_no_tag(self):
        """Default (None) produces a packet with no filter tags — so
        radiod uses the preset's defaults, preserving prior behaviour."""
        control = _bare_control()
        sent = _capture_send(control)
        control.create_channel(
            frequency_hz=10e6, preset="iq", sample_rate=16000,
        )
        assert not _has_tag(sent[0], StatusType.LOW_EDGE)
        assert not _has_tag(sent[0], StatusType.HIGH_EDGE)
        assert not _has_tag(sent[0], StatusType.KAISER_BETA)


class TestEnsureChannelFilterEdges:
    """ensure_channel plumbs filter args through to create_channel
    (create path) and to set_filter (reuse path)."""

    def test_create_path_forwards_to_create_channel(self):
        # No existing channel → ensure_channel goes through create.
        # poll_channel is called twice: the existence probe (None → force
        # create) and the post-create verify (return a matching channel so
        # ensure_channel doesn't raise TimeoutError).
        from ka9q.discovery import ChannelInfo
        control = _bare_control()
        control.create_channel = MagicMock(return_value=None)

        ch_info = ChannelInfo(
            ssrc=0, preset="iq", sample_rate=16000, frequency=45.375e6,
            snr=0.0, multicast_address="239.1.2.3", port=5004,
        )

        def poll_side_effect(ssrc, *args, **kwargs):
            if control.poll_channel.call_count == 1:
                return None
            ch_info.ssrc = ssrc
            return ch_info

        control.poll_channel = MagicMock(side_effect=poll_side_effect)

        control.ensure_channel(
            frequency_hz=45.375e6, preset="iq", sample_rate=16000,
            low_edge=-500.0, high_edge=+500.0, kaiser_beta=11.0,
        )

        kwargs = control.create_channel.call_args.kwargs
        assert kwargs["low_edge"] == -500.0
        assert kwargs["high_edge"] == +500.0
        assert kwargs["kaiser_beta"] == 11.0

    def test_reuse_path_calls_set_filter(self):
        """When a matching channel is found, ensure_channel should call
        set_filter with the requested edges so the filter is authoritative
        regardless of what the existing channel currently has."""
        from ka9q.discovery import ChannelInfo
        control = _bare_control()
        control.create_channel = MagicMock()
        control.set_filter = MagicMock()

        # Build an existing channel that matches freq/rate/preset.
        from ka9q.control import allocate_ssrc
        ssrc = allocate_ssrc(
            frequency_hz=45.375e6, preset="iq", sample_rate=16000,
            agc=False, gain=0.0, destination=None, encoding=0,
            radiod_host="test.local",
        )
        existing = ChannelInfo(
            ssrc=ssrc, preset="iq", sample_rate=16000, frequency=45.375e6,
            snr=0.0, multicast_address="239.1.2.3", port=5004,
        )
        # poll_channel finds the matching channel → reuse branch runs.
        control.poll_channel = MagicMock(return_value=existing)

        control.ensure_channel(
            frequency_hz=45.375e6, preset="iq", sample_rate=16000,
            low_edge=-500.0, high_edge=+500.0, kaiser_beta=11.0,
        )

        # Reuse path: create_channel must NOT be called; set_filter must be.
        control.create_channel.assert_not_called()
        control.set_filter.assert_called_once_with(
            ssrc, low_edge=-500.0, high_edge=+500.0, kaiser_beta=11.0,
        )

    def test_reuse_path_skips_set_filter_when_no_args(self):
        """When ensure_channel is called without filter args, the reuse
        path must NOT call set_filter — preserves prior behaviour and
        avoids a no-op TLV roundtrip."""
        from ka9q.discovery import ChannelInfo
        from ka9q.control import allocate_ssrc
        control = _bare_control()
        control.create_channel = MagicMock()
        control.set_filter = MagicMock()

        ssrc = allocate_ssrc(
            frequency_hz=45.375e6, preset="iq", sample_rate=16000,
            agc=False, gain=0.0, destination=None, encoding=0,
            radiod_host="test.local",
        )
        existing = ChannelInfo(
            ssrc=ssrc, preset="iq", sample_rate=16000, frequency=45.375e6,
            snr=0.0, multicast_address="239.1.2.3", port=5004,
        )
        # poll_channel finds the matching channel → reuse branch runs.
        control.poll_channel = MagicMock(return_value=existing)

        control.ensure_channel(
            frequency_hz=45.375e6, preset="iq", sample_rate=16000,
        )

        control.set_filter.assert_not_called()


class TestMultiStreamAddChannelFilterEdges:
    """MultiStream.add_channel forwards filter args to ensure_channel."""

    def test_forwards_filter_args(self):
        from ka9q.multi_stream import MultiStream
        mock_control = MagicMock()
        mock_control.ensure_channel.return_value = MagicMock(
            ssrc=12345, multicast_address="239.1.2.3", port=5004,
        )

        ms = MultiStream.__new__(MultiStream)
        ms._control = mock_control
        ms._slots = {}
        ms._multicast_address = None
        ms._port = None
        ms._resequence_buffer_size = 128
        ms._samples_per_packet = 200
        ms._deliver_interval = 0.05

        ms.add_channel(
            frequency_hz=45.375e6, preset="iq", sample_rate=16000,
            low_edge=-500.0, high_edge=+500.0, kaiser_beta=11.0,
        )

        kwargs = mock_control.ensure_channel.call_args.kwargs
        assert kwargs["low_edge"] == -500.0
        assert kwargs["high_edge"] == +500.0
        assert kwargs["kaiser_beta"] == 11.0
