"""
SpectrumStream — Real-Time Spectrum Data Receiver

Receives spectrum (FFT) bin data from radiod via the status multicast channel.
Unlike audio streams which use RTP on port 5004, spectrum data arrives as
BIN_DATA or BIN_BYTE_DATA TLV vectors inside status packets on port 5006.

This class handles:
- Creating a spectrum channel on radiod (SPECT2_DEMOD by default)
- Periodic polling to keep the channel alive and request fresh data
- Decoding incoming spectrum status packets (bin vectors + metadata)
- Delivering decoded spectrum frames to a user callback

Usage:
    from ka9q import RadiodControl, SpectrumStream

    def on_spectrum(status):
        db = status.spectrum.bin_power_db
        print(f"{len(db)} bins, peak {db.max():.1f} dB")

    with RadiodControl("radiod.local") as ctl:
        stream = SpectrumStream(
            control=ctl,
            frequency_hz=14.1e6,
            bin_count=1024,
            resolution_bw=100.0,
            on_spectrum=on_spectrum,
        )
        stream.start()
        # ... runs until stopped ...
        stream.stop()
"""

import logging
import select
import socket
import struct
import secrets
import threading
import time
from typing import Callable, Optional

from .control import RadiodControl
from .status import ChannelStatus, decode_status_packet
from .types import DemodType

logger = logging.getLogger(__name__)

# Callback receives a fully-decoded ChannelStatus whose .spectrum fields
# (including .bin_data / .bin_byte_data / .bin_power_db) are populated.
SpectrumCallback = Callable[[ChannelStatus], None]


class SpectrumStream:
    """Receive real-time spectrum data from radiod.

    Spectrum data flows over the status multicast channel (port 5006), not
    RTP.  This class creates a spectrum-type channel on radiod, polls it
    periodically to keep it alive and trigger fresh FFT output, and decodes
    the resulting BIN_DATA / BIN_BYTE_DATA vectors from status packets.

    The ``on_spectrum`` callback is invoked on the receiver thread each time
    a status packet containing bin data arrives for the target SSRC.
    """

    def __init__(
        self,
        control: RadiodControl,
        frequency_hz: float,
        bin_count: int = 1024,
        resolution_bw: float = 100.0,
        *,
        demod_type: int = DemodType.SPECT2_DEMOD,
        window_type: Optional[int] = None,
        kaiser_beta: Optional[float] = None,
        averaging: Optional[int] = None,
        overlap: Optional[float] = None,
        poll_interval_sec: float = 0.1,
        on_spectrum: Optional[SpectrumCallback] = None,
    ):
        """
        Args:
            control: Connected RadiodControl instance.
            frequency_hz: Center frequency in Hz.
            bin_count: Number of FFT bins.
            resolution_bw: Bin bandwidth in Hz.
            demod_type: SPECT_DEMOD (3) or SPECT2_DEMOD (4, default).
            window_type: FFT window (see WindowType constants).
            kaiser_beta: Kaiser window beta parameter.
            averaging: Number of FFTs to average per response. Delegated
                validation (via ``RadiodControl.set_spectrum()``) requires
                averaging >= 1.
            overlap: Window overlap ratio. Delegated validation (via
                ``RadiodControl.set_spectrum()``) requires
                0.0 <= overlap < 1.0 (note the exclusive upper bound).
                An out-of-range averaging or overlap value now raises
                ValidationError when the spectrum command is first sent,
                rather than being forwarded to radiod as a raw float.
            poll_interval_sec: Seconds between spectrum poll commands.
            on_spectrum: Callback(ChannelStatus) invoked on each spectrum frame.
        """
        self._control = control
        self._frequency_hz = frequency_hz
        self._bin_count = bin_count
        self._resolution_bw = resolution_bw
        self._demod_type = demod_type
        self._window_type = window_type
        self._kaiser_beta = kaiser_beta
        self._averaging = averaging
        self._overlap = overlap
        self._poll_interval_sec = poll_interval_sec
        self._on_spectrum = on_spectrum

        # Allocated at start()
        self._ssrc: Optional[int] = None
        self._sock: Optional[socket.socket] = None
        self._running = False
        self._recv_thread: Optional[threading.Thread] = None
        self._poll_thread: Optional[threading.Thread] = None

        # Stats
        self._frames_received = 0
        self._polls_sent = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> int:
        """Create the spectrum channel on radiod and begin receiving.

        Returns:
            The SSRC of the spectrum channel.
        """
        if self._running:
            return self._ssrc

        # Allocate a random SSRC for the spectrum channel.
        # ka9q-web uses ssrc+1 (odd) for spectrum; we just pick a fresh one.
        self._ssrc = secrets.randbits(31)

        # Send the initial creation command
        self._send_spectrum_command()

        # Open a status listener socket
        self._sock = self._create_status_socket()

        self._running = True

        # Receiver thread — reads status packets, filters by SSRC, decodes
        self._recv_thread = threading.Thread(
            target=self._recv_loop,
            daemon=True,
            name="SpectrumStream-Recv",
        )
        self._recv_thread.start()

        # Poll thread — periodically re-sends the spectrum command to keep
        # the channel alive and trigger fresh FFT data
        self._poll_thread = threading.Thread(
            target=self._poll_loop,
            daemon=True,
            name="SpectrumStream-Poll",
        )
        self._poll_thread.start()

        logger.info(
            "SpectrumStream started: SSRC=%d, %.3f MHz, %d bins, %.1f Hz/bin",
            self._ssrc, self._frequency_hz / 1e6,
            self._bin_count, self._resolution_bw,
        )
        return self._ssrc

    def stop(self):
        """Stop receiving and release resources."""
        if not self._running:
            return
        self._running = False

        if self._recv_thread:
            self._recv_thread.join(timeout=3.0)
            self._recv_thread = None
        if self._poll_thread:
            self._poll_thread.join(timeout=3.0)
            self._poll_thread = None

        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

        # Tell radiod to remove the channel (set freq to 0)
        if self._ssrc is not None:
            try:
                self._control.remove_channel(self._ssrc)
            except Exception as exc:
                logger.debug("remove_channel on stop: %s", exc)

        logger.info(
            "SpectrumStream stopped: %d frames received, %d polls sent",
            self._frames_received, self._polls_sent,
        )

    @property
    def ssrc(self) -> Optional[int]:
        return self._ssrc

    @property
    def frames_received(self) -> int:
        return self._frames_received

    def set_frequency(self, frequency_hz: float):
        """Retune the spectrum channel to a new center frequency."""
        self._frequency_hz = frequency_hz
        if self._running:
            self._send_spectrum_command()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()
        return False

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _send_spectrum_command(self):
        """Request spectrum data via RadiodControl.set_spectrum().

        Formerly hand-encoded its own TLV buffer, which made it the only
        reachable write path for WINDOW_TYPE/SPECTRUM_AVG/SPECTRUM_OVERLAP;
        migrated to the public set_spectrum() so there is exactly one
        spectrum write path (audit finding F11).  Packet layout (DEMOD_TYPE
        and RADIO_FREQUENCY before the analyzer parameters, all in one
        command) is preserved by set_spectrum().
        """
        self._control.set_spectrum(
            self._ssrc,
            bin_bw_hz=self._resolution_bw,
            bin_count=self._bin_count,
            kaiser_beta=self._kaiser_beta,
            window_type=self._window_type,
            avg=self._averaging,
            overlap=self._overlap,
            demod_type=self._demod_type,
            frequency_hz=self._frequency_hz,
        )
        self._polls_sent += 1

    def _create_status_socket(self) -> socket.socket:
        """Create a UDP socket joined to radiod's status multicast group."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, 'SO_REUSEPORT'):
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass
        sock.bind(('0.0.0.0', 5006))

        interface_addr = self._control.interface or '0.0.0.0'
        mreq = struct.pack(
            '=4s4s',
            socket.inet_aton(self._control.status_mcast_addr),
            socket.inet_aton(interface_addr),
        )
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        sock.setblocking(False)
        # Spectrum packets with BIN_DATA can be large; ensure kernel buffer
        # is generous enough to avoid drops.
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 512 * 1024)
        except OSError:
            pass
        return sock

    def _recv_loop(self):
        """Receive and dispatch status packets containing spectrum data."""
        while self._running:
            ready = select.select([self._sock], [], [], 0.25)
            if not ready[0]:
                continue
            try:
                buf, _addr = self._sock.recvfrom(65536)
            except (socket.timeout, BlockingIOError):
                continue
            except OSError:
                if self._running:
                    logger.warning("SpectrumStream recv error", exc_info=True)
                break

            st = decode_status_packet(buf)
            if st is None or st.ssrc is None:
                continue
            if st.ssrc != self._ssrc:
                continue

            # Only deliver if we actually got bin data
            sp = st.spectrum
            if sp.bin_data is None and sp.bin_byte_data is None:
                continue

            self._frames_received += 1

            if self._on_spectrum:
                try:
                    self._on_spectrum(st)
                except Exception:
                    logger.warning("on_spectrum callback error", exc_info=True)

    def _poll_loop(self):
        """Periodically poll radiod to keep the spectrum channel alive."""
        while self._running:
            time.sleep(self._poll_interval_sec)
            if not self._running:
                break
            try:
                self._send_spectrum_command()
            except Exception:
                logger.warning("SpectrumStream poll error", exc_info=True)
