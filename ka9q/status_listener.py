"""
Continuous STATUS multicast listener for ka9q-radio.

Background thread that subscribes to radiod's STATUS multicast (port 5006)
and updates a per-SSRC ChannelInfo registry on every broadcast.  Lets
consumers keep their timing anchor (``gps_time``/``rtp_timesnap``) fresh
without polling ``discover_channels`` on a timer.

Why this exists
---------------
Before this module, ``ChannelInfo.gps_time`` and ``rtp_timesnap`` were
captured **once** at SSRC discovery and frozen for the lifetime of the
recorder.  ``rtp_to_wallclock`` projects forward as
``host_clock_at_anchor + RTP_elapsed_at_GPSDO_rate`` — so labels drift
from GPSDO truth at the host-clock slew rate (~3.8 µs/s on a typical
chrony-disciplined host).  Over a day that's ~330 ms.

Radiod emits per-channel STATUS broadcasts at sub-second cadence
(verified empirically on bee1: ~450 ms / SSRC at default settings).
This listener consumes those broadcasts and refreshes the anchor pair
atomically (single attribute assignment under the GIL), bounding
labeling drift to a few µs.

Architectural notes
-------------------
* Independent socket from :py:meth:`RadiodControl._get_or_create_status_listener`.
  Uses ``SO_REUSEPORT`` to share the STATUS port with RadiodControl's
  own socket — Linux delivers each multicast packet to every joined
  socket, so tune/discover responses are not stolen.
* In-place mutation of ChannelInfo.gps_time and ``.rtp_timesnap`` so
  every consumer holding a reference (including ``_t6_channel_info``
  caches) sees the fresh anchor immediately.  The two-field update is
  not strictly atomic, but the torn-read error is negligible: both
  fields change by the same wall-clock delta, so the worst case is one
  µs-scale outlier sample.
* Uses :func:`ka9q.control.decode_status_dict` directly — no
  ``RadiodControl`` instance is created, so no control socket is
  opened.
* Subscribers can register per-SSRC or wildcard callbacks for explicit
  notification (e.g. to refresh an authority snapshot store).
"""

from __future__ import annotations

import logging
import time as _time_mod
import select
import socket
import struct
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from .discovery import ChannelInfo
from .utils import resolve_multicast_address

logger = logging.getLogger(__name__)


StatusCallback = Callable[[ChannelInfo], None]


# Default radiod STATUS multicast port (= DEFAULT_STAT_PORT in ka9q-radio
# rtp.h).  Override via the ``status_port`` constructor argument if your
# radiod uses a non-default port.
DEFAULT_STATUS_PORT = 5006


# Lower bound on how long to back off after a socket error before retrying.
_BACKOFF_INITIAL_SEC = 1.0
_BACKOFF_MAX_SEC = 60.0


@dataclass
class StatusListenerStats:
    """Lightweight metrics for the listener thread.

    Counters are best-effort approximations — `+=` is two bytecodes
    in CPython so concurrent updates can lose a single count.  Useful
    for trend monitoring, not for exact accounting.
    """

    packets_received: int = 0
    status_packets_decoded: int = 0
    decode_errors: int = 0
    updates_applied: int = 0
    callbacks_fired: int = 0
    callback_errors: int = 0
    socket_errors: int = 0
    started_at: float = 0.0
    last_packet_at: float = 0.0


class StatusListener:
    """Continuously listen for radiod STATUS broadcasts.

    Maintains a per-SSRC :py:class:`ChannelInfo` registry.  For every
    SSRC registered via :py:meth:`register_channel`, the listener
    mutates ``channel.gps_time`` and ``channel.rtp_timesnap`` in place
    on each STATUS arrival so that any code holding a reference to that
    :py:class:`ChannelInfo` sees the refreshed anchor.

    Usage
    -----
    Standalone::

        listener = StatusListener("radiod.local")
        listener.start()
        listener.register_channel(channel_info)
        listener.add_callback(channel_info.ssrc, my_callback)
        # ... channel_info.gps_time/rtp_timesnap now refresh continuously ...
        listener.stop()

    Via :py:class:`RadiodControl`::

        with RadiodControl("radiod.local") as ctrl:
            ctrl.start_status_listener()
            ci = ctrl.ensure_channel(...)
            ctrl.status_listener.register_channel(ci)
            # ci.gps_time refreshes on every STATUS broadcast

    The listener is idempotent — calling :py:meth:`start` while already
    running is a no-op.  Closes its socket on :py:meth:`stop` or via
    ``__del__``.
    """

    def __init__(
        self,
        status_address: str,
        interface: Optional[str] = None,
        status_port: int = DEFAULT_STATUS_PORT,
        socket_timeout: float = 0.5,
    ):
        """
        Args:
            status_address: mDNS name or IP of radiod's status multicast group.
            interface: IP address of network interface for multicast join.
                ``None`` uses INADDR_ANY (single-homed default).
            status_port: UDP port radiod uses for STATUS broadcasts
                (default 5006 — matches DEFAULT_STAT_PORT in ka9q-radio
                ``src/rtp.h``).  Override only if your radiod is
                configured for a non-default port.
            socket_timeout: select() timeout in seconds.  Smaller values
                give faster ``stop()`` response at slightly higher CPU.
        """
        self.status_address = status_address
        self.interface = interface
        self.status_port = status_port
        self.socket_timeout = socket_timeout

        self._channels: Dict[int, ChannelInfo] = {}
        self._callbacks: Dict[int, List[StatusCallback]] = {}
        self._wildcard_callbacks: List[StatusCallback] = []
        self._lock = threading.RLock()

        self._socket: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False

        self.stats = StatusListenerStats()

    # ── lifecycle ──────────────────────────────────────────────────

    def start(self) -> None:
        """Start the background listener thread (idempotent)."""
        # Quick check under the lock — but build the socket OUTSIDE the
        # lock so we don't stall ``register_channel`` etc. through the
        # mDNS resolution and multicast join (up to ~5 s on cold mDNS).
        with self._lock:
            if self._running:
                return
        sock = self._create_socket()
        with self._lock:
            if self._running:
                # Another thread won the race; tear down our spare socket.
                try:
                    sock.close()
                except Exception:
                    pass
                return
            self._socket = sock
            self._running = True
            self.stats.started_at = time.time()
            self._thread = threading.Thread(
                target=self._receive_loop,
                name=f"ka9q-status-listener-{self.status_address}",
                daemon=True,
            )
            self._thread.start()
        logger.info(
            f"StatusListener started on {self.status_address}:{self.status_port}"
        )

    def stop(self, timeout: float = 2.0) -> None:
        """Stop the listener thread and close its socket."""
        with self._lock:
            if not self._running:
                return
            self._running = False
            thread = self._thread
            self._thread = None
        if thread is not None:
            thread.join(timeout=timeout)
        with self._lock:
            if self._socket is not None:
                try:
                    self._socket.close()
                except Exception:
                    pass
                self._socket = None
        logger.info("StatusListener stopped")

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()
        return False

    def __del__(self):
        try:
            self.stop()
        except Exception:
            pass

    @property
    def running(self) -> bool:
        return self._running

    # ── registration / subscription ────────────────────────────────

    def register_channel(self, channel_info: ChannelInfo) -> None:
        """Register a ChannelInfo to be refreshed on every STATUS broadcast.

        The listener stores the reference and mutates ``gps_time`` and
        ``rtp_timesnap`` in place whenever a STATUS for ``channel_info.ssrc``
        arrives.  Re-registering the same SSRC replaces the prior
        reference (use this when an :py:class:`RtpRecorder` reconnects
        with a new :py:class:`ChannelInfo`).
        """
        with self._lock:
            self._channels[channel_info.ssrc] = channel_info

    def unregister_channel(self, ssrc: int) -> None:
        """Stop tracking an SSRC."""
        with self._lock:
            self._channels.pop(ssrc, None)
            self._callbacks.pop(ssrc, None)

    def add_callback(self, ssrc: int, callback: StatusCallback) -> None:
        """Register a callback that fires after each STATUS for ``ssrc``.

        The callback receives the (refreshed) :py:class:`ChannelInfo`.
        Exceptions in callbacks are logged and counted but do not stop
        the listener.
        """
        with self._lock:
            self._callbacks.setdefault(ssrc, []).append(callback)

    def add_wildcard_callback(self, callback: StatusCallback) -> None:
        """Register a callback that fires on every STATUS broadcast."""
        with self._lock:
            self._wildcard_callbacks.append(callback)

    def remove_callback(self, ssrc: int, callback: StatusCallback) -> None:
        with self._lock:
            cbs = self._callbacks.get(ssrc, [])
            try:
                cbs.remove(callback)
            except ValueError:
                pass

    def get_channel_info(self, ssrc: int) -> Optional[ChannelInfo]:
        """Return the tracked :py:class:`ChannelInfo` for ``ssrc``, or None."""
        with self._lock:
            return self._channels.get(ssrc)

    # ── internals ──────────────────────────────────────────────────

    def _create_socket(self) -> socket.socket:
        mcast_addr = resolve_multicast_address(self.status_address, timeout=5.0)

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                # Not fatal — RadiodControl may not have a coexisting
                # status socket on this host.
                pass

        sock.bind(("0.0.0.0", self.status_port))

        interface_addr = self.interface if self.interface else "0.0.0.0"
        mreq = struct.pack(
            "=4s4s",
            socket.inet_aton(mcast_addr),
            socket.inet_aton(interface_addr),
        )
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        # Match select() timeout so a manual recvfrom never blocks past
        # the loop's poll cadence.
        sock.settimeout(self.socket_timeout)
        logger.debug(
            f"StatusListener joined {mcast_addr}:{self.status_port} "
            f"on interface {interface_addr}"
        )
        return sock

    def _receive_loop(self) -> None:
        # Late import: keeps module load time clean and avoids any chance
        # of a circular import at start-of-day.
        from .control import decode_status_dict

        sock = self._socket
        assert sock is not None

        backoff = _BACKOFF_INITIAL_SEC

        while self._running:
            try:
                ready, _, _ = select.select([sock], [], [], self.socket_timeout)
                if not ready:
                    continue
                buffer, _addr = sock.recvfrom(8192)
                # Successful read — reset backoff on the OSError path.
                backoff = _BACKOFF_INITIAL_SEC
            except OSError as e:
                if not self._running:
                    break
                self.stats.socket_errors += 1
                logger.warning(
                    f"StatusListener socket error (sleeping {backoff:.1f}s): {e}"
                )
                # Exponential backoff capped at _BACKOFF_MAX_SEC; respects
                # _running so stop() unblocks promptly.
                slept = 0.0
                while slept < backoff and self._running:
                    time.sleep(min(0.5, backoff - slept))
                    slept += 0.5
                backoff = min(backoff * 2, _BACKOFF_MAX_SEC)
                continue
            except Exception as e:
                if self._running:
                    self.stats.socket_errors += 1
                    logger.warning(f"StatusListener recv error: {e}")
                continue

            self.stats.packets_received += 1
            self.stats.last_packet_at = time.time()

            # STATUS packets begin with type byte 0; skip COMMAND (1) and
            # non-TLV packets.
            if not buffer or buffer[0] != 0:
                continue

            try:
                status = decode_status_dict(buffer)
            except Exception:
                self.stats.decode_errors += 1
                continue

            if not status:
                continue

            self.stats.status_packets_decoded += 1

            ssrc = status.get("ssrc")
            if not ssrc:
                continue

            gps_time = status.get("gps_time")
            rtp_timesnap = status.get("rtp_timesnap")
            if gps_time is None or rtp_timesnap is None:
                # STATUS packet with no timing fields — uninteresting.
                continue

            self._apply_update(ssrc, status, gps_time, rtp_timesnap)

    def _apply_update(
        self,
        ssrc: int,
        status: dict,
        gps_time: int,
        rtp_timesnap: int,
    ) -> None:
        with self._lock:
            ci = self._channels.get(ssrc)
            per_ssrc_callbacks = list(self._callbacks.get(ssrc, ()))
            wildcard_callbacks = list(self._wildcard_callbacks)

        if ci is None:
            # We don't auto-create ChannelInfo for unknown SSRCs — that
            # would require building the multicast_address/port from the
            # destination TLV and creates ambiguity about which channels
            # to track.  Callers register channels explicitly via
            # register_channel() (or ensure_channel + register).
            return

        # Atomic anchor swap (v3.16.0+ via ChannelInfo.update_anchor):
        # writes a single tuple ``_anchor_pair = (gps_time, rtp_timesnap)``
        # in one GIL-atomic attribute assignment.  Readers using
        # :py:meth:`ChannelInfo.get_anchor` see either the old pair or
        # the new pair — never a torn combination off by ~one listener-
        # cadence (~450 ms).  Legacy ``ci.gps_time`` / ``ci.rtp_timesnap``
        # are also updated for backward compatibility with diagnostic
        # readers, but ``rtp_to_wallclock`` and other transactional
        # consumers go through ``get_anchor`` to dodge the torn-read
        # window entirely.
        # ---- anchor-pair self-consistency audit (hf-timestd#7) ----
        # Every sub-second timestamp downstream -- rtp_to_utc, the BPSK
        # chain delay, the chrony SHM reference time -- rests on this
        # (gps_time, rtp_timesnap) pair.  Nothing has ever checked that
        # consecutive pairs describe the SAME clock relationship.
        #
        # They must: the ADC is GPSDO-disciplined, so advancing
        # rtp_timesnap by n samples must advance gps_time by n/sample_rate
        # seconds, to within GPSDO drift (parts in 1e11 -- nanoseconds
        # over this cadence).  Predict the new gps_time from the previous
        # pair and report the disagreement.
        #
        # Motivation: AC0G-B4 2026-08-08 shows an 80.000 ms chain-delay
        # step -- exactly 4 radiod blocks of 20 ms.  Not in the RF (the
        # correlation surface is a single clean triangle to +-250 ms),
        # not in the matched filter (0.12 us repeatable), not in sample
        # labelling (drift bounded, non-accumulating).  This pair is what
        # is left.  If consecutive pairs disagree in whole blocks, this
        # audit shows it.
        try:
            _sr = int(getattr(ci, 'sample_rate', 0) or 0)
            _aud = getattr(self, '_anchor_audit', None)
            if _aud is None:
                _aud = self._anchor_audit = {
                    'prev': {}, 'n': 0, 'hist': {}, 'max_ns': 0, 'last_log': 0.0,
                }
            _prev = _aud['prev'].get(ssrc)
            if _prev is not None and _sr:
                _pg, _ps = _prev
                _dsnap = (int(rtp_timesnap) - int(_ps)) & 0xFFFFFFFF
                if _dsnap >= (1 << 31):
                    _dsnap -= (1 << 32)
                _implied = int(_pg) + int(round(_dsnap * 1e9 / _sr))
                _delta = int(gps_time) - _implied
                _aud['n'] += 1
                if abs(_delta) > abs(_aud['max_ns']):
                    _aud['max_ns'] = _delta
                # Bucket by whole milliseconds; a 20 ms-quantised pattern
                # is the signature we are testing for.
                _ms = int(round(_delta / 1e6))
                _aud['hist'][_ms] = _aud['hist'].get(_ms, 0) + 1
                _now = _time_mod.monotonic()
                if (_now - _aud['last_log']) > 60.0:
                    _aud['last_log'] = _now
                    _top = sorted(_aud['hist'].items(),
                                  key=lambda kv: -kv[1])[:8]
                    logger.warning(
                        "ANCHOR PAIR AUDIT: updates=%d max_disagreement=%+d ns "
                        "(%+.3f ms) | delta_ms histogram=%s",
                        _aud['n'], _aud['max_ns'], _aud['max_ns'] / 1e6, _top,
                    )
            _aud['prev'][ssrc] = (int(gps_time), int(rtp_timesnap))
        except Exception:
            pass
        # ---- end anchor-pair audit ----
        ci.update_anchor(gps_time, rtp_timesnap)

        # Opportunistic refresh of encoding (radiod can grant a different
        # encoding than requested).  Other fields (frequency, sample_rate)
        # are intentionally left alone — if radiod retuned mid-stream we
        # don't want to silently flip the recorder's interpretation.
        enc = status.get("encoding")
        if enc is not None:
            ci.encoding = enc

        # Cumulative per-channel output block drops.  Refreshed here so a
        # long-running client can watch radiod's own loss counter without
        # opening a second status socket -- differencing it across two
        # reads is the honest measurement; the level alone is meaningless
        # because it accumulates from radiod start.
        drops = status.get("filter_drops")
        if drops is not None:
            ci.filter_drops = drops

        self.stats.updates_applied += 1

        for cb in per_ssrc_callbacks:
            try:
                cb(ci)
                self.stats.callbacks_fired += 1
            except Exception:
                self.stats.callback_errors += 1
                logger.exception(
                    f"StatusListener: per-SSRC callback failed for SSRC {ssrc}"
                )
        for cb in wildcard_callbacks:
            try:
                cb(ci)
                self.stats.callbacks_fired += 1
            except Exception:
                self.stats.callback_errors += 1
                logger.exception("StatusListener: wildcard callback failed")
