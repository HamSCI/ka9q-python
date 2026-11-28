"""MultiStream's socket must receive only its OWN multicast group.

MultiStream binds ``0.0.0.0:<port>`` and joins one group.  On Linux the
default ``IP_MULTICAST_ALL=1`` then delivers every datagram for that port
whose group ANY socket on the host has joined.  On KFS-MAIN (2026-11-28)
each wspr-recorder socket thus received all ~14 000 datagrams/s sent to
port 5004 (hf-timestd IQ, codar, psk, the other radiod) when its own 17
channels make ~850/s.  The receive thread parsed and discarded the rest,
and while the decode workers held the GIL at a WSPR cycle boundary the
foreign traffic filled the 32 MB receive buffer in about a second: 5 313
datagrams dropped at 01:40:01-02Z, every one an RcvbufError.

These tests use real loopback multicast: a foreign sender on another group
of the same port must not reach the MultiStream socket.
"""

from __future__ import annotations

import socket
import struct
import time
import unittest
from unittest.mock import MagicMock

from ka9q.multi_stream import MultiStream

OWN_GROUP = "239.77.1.1"
FOREIGN_GROUP = "239.77.1.2"


def _free_udp_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("0.0.0.0", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _loopback_sender() -> socket.socket:
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    tx.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 0)
    tx.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
    tx.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                  socket.inet_aton("127.0.0.1"))
    return tx


def _join_on_lo(sock: socket.socket, group: str) -> None:
    sock.setsockopt(
        socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
        struct.pack("=4s4s", socket.inet_aton(group),
                    socket.inet_aton("127.0.0.1")),
    )


def _drain(sock: socket.socket, wait_s: float = 0.3) -> list:
    sock.settimeout(0.05)
    got = []
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        try:
            got.append(sock.recv(2048))
        except socket.timeout:
            continue
    return got


class TestMultiStreamReceivesOnlyItsGroup(unittest.TestCase):

    def setUp(self):
        self.port = _free_udp_port()
        # Another process's socket on the same port, joined to a different
        # group — e.g. hf-timestd or a second radiod's recorder.
        self.other = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.other.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.other.bind((FOREIGN_GROUP, self.port))
        try:
            _join_on_lo(self.other, FOREIGN_GROUP)
        except OSError as exc:  # pragma: no cover - host without lo multicast
            self.other.close()
            self.skipTest(f"loopback multicast unavailable: {exc}")

        ms = MultiStream(control=MagicMock())
        ms._multicast_address = OWN_GROUP
        ms._port = self.port
        self.sock = ms._create_socket()
        self.tx = _loopback_sender()

    def tearDown(self):
        for s in (getattr(self, "sock", None), getattr(self, "other", None),
                  getattr(self, "tx", None)):
            if s is not None:
                s.close()

    def test_own_group_is_delivered(self):
        self.tx.sendto(b"own", (OWN_GROUP, self.port))
        self.assertIn(b"own", _drain(self.sock))

    def test_foreign_group_on_same_port_is_not_delivered(self):
        # Sanity: the foreign group really is live on this host.
        self.tx.sendto(b"foreign", (FOREIGN_GROUP, self.port))
        self.assertIn(b"foreign", _drain(self.other))
        # ...and must not reach the MultiStream socket.
        self.tx.sendto(b"foreign", (FOREIGN_GROUP, self.port))
        self.assertNotIn(b"foreign", _drain(self.sock))


if __name__ == "__main__":
    unittest.main()
