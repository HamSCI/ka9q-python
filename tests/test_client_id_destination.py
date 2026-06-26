"""RadiodControl(client_id=...) destination-derivation tests.

When ``client_id`` is set, ``ensure_channel(destination=None)`` should
auto-derive a per-(client, radiod) multicast address so peer clients on
the same host land on distinct multicast groups without per-client
derivation code.  Tests mock ``_connect`` and short-circuit
``create_channel`` to capture the resolved destination without
requiring a live radiod or running the verify loop.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from ka9q.addressing import generate_multicast_ip
from ka9q.control import RadiodControl


def _ensure_dest(ctrl: RadiodControl, *, destination=None) -> object:
    """Drive ensure_channel to ``create_channel`` and return the
    destination it would have received.  ``create_channel`` raises to
    short-circuit the post-create verify loop (which would otherwise
    spin discover_channels for ``timeout`` seconds)."""
    captured: dict = {}

    def _capture(*_args, **kwargs):
        captured['destination'] = kwargs.get('destination')
        raise _ShortCircuit()

    # ensure_channel probes for an existing channel via poll_channel
    # (real DNS + multicast I/O); stub it to None so we always fall
    # through to the captured create_channel.
    with patch.object(ctrl, 'poll_channel', return_value=None), \
         patch.object(ctrl, 'create_channel', side_effect=_capture):
        try:
            ctrl.ensure_channel(
                frequency_hz=14_074_000.0,
                preset="iq",
                sample_rate=16000,
                destination=destination,
            )
        except _ShortCircuit:
            pass
    return captured.get('destination')


class _ShortCircuit(Exception):
    pass


class TestClientIdStored(unittest.TestCase):
    @patch('ka9q.control.RadiodControl._connect')
    def test_client_id_default_none(self, _c):
        ctrl = RadiodControl("radiod.local")
        self.assertIsNone(ctrl.client_id)

    @patch('ka9q.control.RadiodControl._connect')
    def test_client_id_stored(self, _c):
        ctrl = RadiodControl("radiod.local", client_id="psk-recorder")
        self.assertEqual(ctrl.client_id, "psk-recorder")


class TestDestinationPrecedence(unittest.TestCase):
    """ensure_channel's destination= resolution order:
        explicit > derived (from client_id+status) > None.
    """

    @patch('ka9q.control.RadiodControl._connect')
    def test_explicit_destination_wins_over_client_id(self, _c):
        ctrl = RadiodControl("radiod.local", client_id="psk-recorder")
        dest = _ensure_dest(ctrl, destination="239.1.2.3")
        self.assertEqual(dest, "239.1.2.3")

    @patch('ka9q.control.RadiodControl._connect')
    def test_no_client_id_keeps_destination_none(self, _c):
        """Pre-3.14 behavior preserved: no client_id, no explicit
        destination -> destination=None flows through so radiod uses
        its config-file default."""
        ctrl = RadiodControl("radiod.local")
        dest = _ensure_dest(ctrl, destination=None)
        self.assertIsNone(dest)

    @patch('ka9q.control.RadiodControl._connect')
    def test_client_id_derives_destination(self, _c):
        ctrl = RadiodControl("bee1-status.local", client_id="psk-recorder")
        expected = generate_multicast_ip(
            "psk-recorder", radiod_host="bee1-status.local",
        )
        dest = _ensure_dest(ctrl, destination=None)
        self.assertEqual(dest, expected)
        self.assertTrue(dest.startswith("239."))


class TestDestinationUniqueness(unittest.TestCase):
    """The two invariants the operator-facing design promises:
      (a) same client on two radiods -> two distinct destinations
      (b) two clients on one radiod -> two distinct destinations
    """

    @patch('ka9q.control.RadiodControl._connect')
    def test_same_client_different_radiods(self, _c):
        a = RadiodControl("bee1-status.local", client_id="psk-recorder")
        b = RadiodControl("bee3-status.local", client_id="psk-recorder")
        self.assertNotEqual(_ensure_dest(a), _ensure_dest(b),
                            "psk-recorder on radiod-0 vs radiod-1 must use "
                            "distinct multicast groups")

    @patch('ka9q.control.RadiodControl._connect')
    def test_different_clients_same_radiod(self, _c):
        a = RadiodControl("bee1-status.local", client_id="psk-recorder")
        b = RadiodControl("bee1-status.local", client_id="wspr-recorder")
        self.assertNotEqual(_ensure_dest(a), _ensure_dest(b),
                            "psk-recorder and wspr-recorder on one radiod "
                            "must use distinct multicast groups")

    @patch('ka9q.control.RadiodControl._connect')
    def test_same_client_same_radiod_repeatable(self, _c):
        """Restart must bind to the same multicast group."""
        a = RadiodControl("bee1-status.local", client_id="psk-recorder")
        b = RadiodControl("bee1-status.local", client_id="psk-recorder")
        self.assertEqual(_ensure_dest(a), _ensure_dest(b))


class TestDestinationParticipatesInSsrc(unittest.TestCase):
    """allocate_ssrc already hashes destination into the SSRC, so two
    clients with derived destinations must produce different SSRCs
    even for identical channel parameters."""

    @patch('ka9q.control.RadiodControl._connect')
    def test_ssrcs_diverge_per_client(self, _c):
        from ka9q.control import allocate_ssrc

        params = dict(frequency_hz=14_074_000.0, preset="iq",
                      sample_rate=16000, agc=False, gain=0.0)
        psk_dest = generate_multicast_ip("psk-recorder",
                                          radiod_host="bee1-status.local")
        wspr_dest = generate_multicast_ip("wspr-recorder",
                                           radiod_host="bee1-status.local")
        psk_ssrc = allocate_ssrc(**params, destination=psk_dest,
                                  radiod_host="bee1-status.local")
        wspr_ssrc = allocate_ssrc(**params, destination=wspr_dest,
                                   radiod_host="bee1-status.local")
        self.assertNotEqual(psk_ssrc, wspr_ssrc,
                            "Per-client destination must produce per-client "
                            "SSRC, so radiod's channel table separates them")


if __name__ == "__main__":
    unittest.main()
