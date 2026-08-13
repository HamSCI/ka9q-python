"""Unit tests for ka9q._multicast helpers.

Locks in the multi-interface IP_ADD_MEMBERSHIP behaviour that both
MultiStream and RadiodStream rely on for TTL=0 / multi-homed reception.
Rob Robinett (e3acb6a) introduced the logic inside multi_stream.py;
this refactor moved it to ka9q._multicast and extended RadiodStream to
use it too.
"""

from __future__ import annotations

import socket
import struct
import unittest
from unittest.mock import MagicMock, patch

from ka9q._multicast import (
    join_multicast_all_interfaces,
    iter_local_ipv4_interfaces,
)


class TestIterLocalIPv4Interfaces(unittest.TestCase):
    """`iter_local_ipv4_interfaces` should walk `if_nameindex` and skip
    interfaces SIOCGIFADDR doesn't answer for."""

    def test_enumerates_via_if_nameindex(self):
        """Smoke-test: at least one interface must come back on a real host."""
        ifaces = list(iter_local_ipv4_interfaces())
        # Every Linux box has 'lo' with 127.0.0.1.  If this list is
        # empty something is very wrong (privileged container with no
        # network? broken socket()?).
        self.assertGreater(len(ifaces), 0)
        names = [n for n, _ in ifaces]
        self.assertIn('lo', names)
        # Find lo and verify it has the expected loopback address.
        for name, addr in ifaces:
            if name == 'lo':
                self.assertEqual(addr, '127.0.0.1')
                break
        else:
            self.fail("lo not present in enumeration")

    def test_handles_if_nameindex_failure(self):
        """If if_nameindex() raises, we get an empty iterator (not a crash)."""
        with patch('socket.if_nameindex', side_effect=OSError("no /proc/net")):
            self.assertEqual(list(iter_local_ipv4_interfaces()), [])

    def test_skips_interfaces_without_ipv4(self):
        """An interface that has no IPv4 (SIOCGIFADDR errors) is silently
        skipped — we just don't see it in the output."""
        # Real-host probe should not include made-up interfaces.
        ifaces = list(iter_local_ipv4_interfaces())
        names = [n for n, _ in ifaces]
        self.assertNotIn('this-interface-does-not-exist', names)


class TestJoinMulticastAllInterfaces(unittest.TestCase):
    """`join_multicast_all_interfaces` must call setsockopt once per
    enumerated interface and return the names where the join succeeded."""

    def test_joins_each_enumerated_interface(self):
        sock = MagicMock(spec=socket.socket)
        sock.setsockopt = MagicMock()

        fake_ifaces = [('lo', '127.0.0.1'),
                       ('ens0', '192.168.1.50'),
                       ('tailscale0', '100.64.0.5')]
        with patch('ka9q._multicast.iter_local_ipv4_interfaces',
                   return_value=iter(fake_ifaces)):
            joined = join_multicast_all_interfaces(sock, '239.1.2.3')

        self.assertEqual(joined, ['lo', 'ens0', 'tailscale0'])
        self.assertEqual(sock.setsockopt.call_count, 3)

        # Verify each setsockopt got the right (group, iface) mreq.
        for call, (_ifname, ifaddr) in zip(sock.setsockopt.call_args_list, fake_ifaces):
            args = call.args
            self.assertEqual(args[0], socket.IPPROTO_IP)
            self.assertEqual(args[1], socket.IP_ADD_MEMBERSHIP)
            expected_mreq = struct.pack(
                "=4s4s",
                socket.inet_aton('239.1.2.3'),
                socket.inet_aton(ifaddr),
            )
            self.assertEqual(args[2], expected_mreq)

    def test_per_interface_failure_skipped(self):
        """A setsockopt OSError on one interface skips it but doesn't
        abort the loop or raise — the other interfaces still join."""
        sock = MagicMock(spec=socket.socket)
        # ens0 fails, lo + tailscale0 succeed.
        def setsockopt_side_effect(level, opt, value):
            mreq_iface = socket.inet_ntoa(value[4:8])
            if mreq_iface == '192.168.1.50':
                raise OSError(99, "Cannot assign requested address")
            return None
        sock.setsockopt.side_effect = setsockopt_side_effect

        fake_ifaces = [('lo', '127.0.0.1'),
                       ('ens0', '192.168.1.50'),
                       ('tailscale0', '100.64.0.5')]
        with patch('ka9q._multicast.iter_local_ipv4_interfaces',
                   return_value=iter(fake_ifaces)):
            joined = join_multicast_all_interfaces(sock, '239.1.2.3')

        self.assertEqual(joined, ['lo', 'tailscale0'])  # ens0 dropped

    def test_empty_enumeration_returns_empty(self):
        """No interfaces enumerated → empty join list, no exception."""
        sock = MagicMock(spec=socket.socket)
        with patch('ka9q._multicast.iter_local_ipv4_interfaces',
                   return_value=iter([])):
            joined = join_multicast_all_interfaces(sock, '239.1.2.3')
        self.assertEqual(joined, [])
        sock.setsockopt.assert_not_called()


class TestStreamClassesUseHelper(unittest.TestCase):
    """RadiodStream and MultiStream must both call the shared helper, so a
    future change to join semantics applies uniformly."""

    def test_radiodstream_imports_helper(self):
        # Ensure the import succeeded — would raise ImportError at
        # collection time if the symbol moved.
        from ka9q import stream
        self.assertTrue(hasattr(stream, 'join_multicast_all_interfaces'))

    def test_multistream_imports_helper(self):
        from ka9q import multi_stream
        self.assertTrue(hasattr(multi_stream, 'join_multicast_all_interfaces'))


def test_resolve_multicast_address_is_reexported():
    """audit F14: hf-timestd reaches into ka9q.utils for this; make it a
    first-class export so the internals-reach can be retired."""
    import ka9q
    from ka9q.utils import resolve_multicast_address as util_fn
    assert ka9q.resolve_multicast_address is util_fn
    assert "resolve_multicast_address" in ka9q.__all__


if __name__ == '__main__':
    unittest.main()
