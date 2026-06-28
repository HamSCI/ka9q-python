"""SlotClock — epoch-aligned slot boundaries in GPS-true RTP-timestamp space.

The canonical, drift-immune timing primitive shared by every sigmond slot/
period recorder (psk-recorder FT8/FT4, wspr-recorder WSPR/FST4W, hfdl, …).
It exists because clients kept re-implementing slot timing on top of a
*delivered-sample-count* projection, which silently drifts: if the receive
path ever over- or under-counts samples relative to real time (gap-fill
accounting, a late anchor, a dropped burst the resequencer mis-sizes), the
projected UTC runs ahead of or behind the actual RF, the WAV gets a label
that doesn't match its content, and decode_ft8/wsprd align to the wrong grid
point → zero decodes on perfectly good audio.  A self-check built from the
same delivered-count can't catch this — both sides move together.

The fix, and this class's whole premise: **drive boundaries from the RTP
timestamp radiod stamps on every packet.**  radiod's RTP counter is
GPS/PPS-disciplined and advances by exactly one per output sample of real
time, regardless of what the client's delivery bookkeeping does.  So:

  * Anchor ONCE: map a single RTP timestamp to UTC via ``rtp_to_wallclock``
    (the §18/METROLOGY RTP-reference rule — the only wall-clock-ish read).
  * Every slot boundary is an epoch-aligned UTC instant (a multiple of the
    cadence: FT8 :00/:15/:30/:45, FT4 every 7.5 s, WSPR every 120 s) whose
    RTP timestamp is computed by integer arithmetic from the anchor.
  * A slot is *complete* once the stream's latest RTP timestamp has passed
    the slot's end (plus a small settle).  The sample window to extract is
    expressed in RTP units, so it's immune to delivered-count drift.
  * ``cadence_sec * sample_rate`` must be an integer number of samples
    (true for every real mode: 15·12000, 7.5·12000, 120·12000, …), so
    boundary-to-boundary stepping is exact integer arithmetic — no float
    accumulation.

RTP timestamps are unsigned 32-bit and wrap (~99 h at 12 kHz, ~16 h at
64 kHz IQ).  All differences use Phil Karn's signed-32 technique so a wrap
is just normal arithmetic; absolute RTP positions are tracked as an
unwrapped 64-bit count off the anchor.

This class is pure timing logic — it owns no socket, ring, or thread.  The
caller feeds it RTP timestamps and asks which slots are now complete.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import List, Optional

logger = logging.getLogger(__name__)


def rtp_diff(a: int, b: int) -> int:
    """Signed 32-bit RTP timestamp difference ``a - b`` (Karn's technique).

    Returns a value in [-2**31, 2**31) so timestamp wraps are handled as
    ordinary arithmetic: positive => a is ahead of b.
    """
    d = (a - b) & 0xFFFFFFFF
    if d >= 0x80000000:
        d -= 0x100000000
    return d


@dataclass(frozen=True)
class Slot:
    """One completed, epoch-aligned slot.

    index               monotonic slot counter from the anchor (k)
    start_rtp           RTP timestamp of the first sample of the slot
    start_utc           UTC of that first sample (epoch-aligned: multiple
                        of cadence_sec to within <1 sample)
    n_samples           number of samples in the slot (== cadence_samples)
    """

    index: int
    start_rtp: int
    start_utc: float
    n_samples: int


class SlotClock:
    """Epoch-aligned slot boundaries tracked in RTP-timestamp space.

    Usage::

        clk = SlotClock(cadence_sec=15.0, sample_rate=12000)
        clk.anchor(rtp_timestamp=first_rtp, utc=rtp_to_wallclock(first_rtp, ci))
        ...
        # as packets arrive, advance the high-water mark and harvest slots:
        for slot in clk.advance(latest_rtp_timestamp):
            samples = ring.extract_rtp(slot.start_rtp, slot.n_samples)
            ...
    """

    def __init__(
        self,
        cadence_sec: float,
        sample_rate: int,
        settle_sec: float = 1.5,
    ) -> None:
        cadence_samples = cadence_sec * sample_rate
        if abs(cadence_samples - round(cadence_samples)) > 1e-6:
            raise ValueError(
                f"cadence_sec * sample_rate must be an integer sample count; "
                f"got {cadence_sec} * {sample_rate} = {cadence_samples}"
            )
        self.cadence_sec = float(cadence_sec)
        self.sample_rate = int(sample_rate)
        self.cadence_samples = int(round(cadence_samples))
        self.settle_samples = int(round(settle_sec * sample_rate))

        self._anchor_rtp: Optional[int] = None
        self._anchor_utc: Optional[float] = None
        # Absolute (unwrapped) RTP position of the most recent boundary we
        # have already emitted, as an offset in samples from the anchor.
        self._next_boundary_off: Optional[int] = None
        self._next_index: int = 0

    # ── anchoring ────────────────────────────────────────────────────

    @property
    def anchored(self) -> bool:
        return self._anchor_rtp is not None

    def reset(self) -> None:
        """Drop the anchor so the next ``anchor()`` re-establishes the grid.

        The monotonic slot index is preserved.  Callers that key a buffer by
        ``offset_of_rtp`` MUST also discard that buffer, since offsets are
        relative to the (now-cleared) anchor.
        """
        self._anchor_rtp = None
        self._anchor_utc = None
        self._next_boundary_off = None

    def anchor(self, rtp_timestamp: int, utc: float) -> None:
        """Pin (rtp_timestamp -> utc).  Call once; call again to re-anchor.

        ``utc`` should come from ``ka9q.rtp_to_wallclock(rtp_timestamp, ci)``
        so the whole grid is RTP/GPS-referenced.  Re-anchoring preserves the
        monotonic slot index but recomputes the first upcoming boundary, so a
        corrected anchor takes effect on the next clean boundary.
        """
        self._anchor_rtp = int(rtp_timestamp) & 0xFFFFFFFF
        self._anchor_utc = float(utc)
        # First epoch-aligned boundary at or after the anchor instant.
        t0 = math.ceil(self._anchor_utc / self.cadence_sec) * self.cadence_sec
        self._next_boundary_off = int(round((t0 - self._anchor_utc) * self.sample_rate))
        logger.info(
            "SlotClock(cadence=%.3fs, sr=%d): anchored rtp=%d utc=%.3f; "
            "first boundary at utc=%.3f (+%d samples)",
            self.cadence_sec, self.sample_rate, self._anchor_rtp,
            self._anchor_utc, t0, self._next_boundary_off,
        )

    # ── projection helpers ───────────────────────────────────────────

    def utc_of_offset(self, sample_off: int) -> float:
        """UTC of the sample ``sample_off`` samples after the anchor."""
        assert self._anchor_utc is not None
        return self._anchor_utc + sample_off / self.sample_rate

    def rtp_of_offset(self, sample_off: int) -> int:
        """Wrapped 32-bit RTP timestamp ``sample_off`` samples after anchor."""
        assert self._anchor_rtp is not None
        return (self._anchor_rtp + sample_off) & 0xFFFFFFFF

    def offset_of_rtp(self, rtp_timestamp: int) -> int:
        """Unwrapped sample offset from the anchor for a wrapped RTP ts."""
        assert self._anchor_rtp is not None
        return rtp_diff(rtp_timestamp, self._anchor_rtp)

    # ── slot harvesting ──────────────────────────────────────────────

    def advance(self, latest_rtp_timestamp: int) -> List[Slot]:
        """Return every slot that has fully arrived as of ``latest_rtp``.

        ``latest_rtp_timestamp`` is the RTP timestamp just past the newest
        sample the caller holds (i.e. first_rtp + samples_buffered).  A slot
        is complete once latest_rtp has passed slot_end + settle.  Boundaries
        step by exact integer ``cadence_samples`` so no drift accumulates.
        """
        if self._anchor_rtp is None or self._next_boundary_off is None:
            return []
        latest_off = self.offset_of_rtp(latest_rtp_timestamp)
        out: List[Slot] = []
        while latest_off >= (
            self._next_boundary_off + self.cadence_samples + self.settle_samples
        ):
            start_off = self._next_boundary_off
            out.append(
                Slot(
                    index=self._next_index,
                    start_rtp=self.rtp_of_offset(start_off),
                    start_utc=self.utc_of_offset(start_off),
                    n_samples=self.cadence_samples,
                )
            )
            self._next_boundary_off = start_off + self.cadence_samples
            self._next_index += 1
        return out

    # ── RTP-reference re-validation ──────────────────────────────────

    def divergence_sec(self, channel_info, rtp_to_wallclock) -> Optional[float]:
        """Grid-vs-GPS divergence at the next boundary, in seconds.

        Recomputes the next boundary's true UTC straight from radiod's
        (StatusListener-refreshed) ``channel_info`` via ``rtp_to_wallclock``
        and compares it to the grid projection.  A sustained nonzero result
        means the anchor is stale/wrong — the caller should ``anchor()``
        again off the fresh reference.  Returns None if it can't be computed.
        """
        if self._anchor_rtp is None or self._next_boundary_off is None:
            return None
        boundary_rtp = self.rtp_of_offset(self._next_boundary_off)
        projected = self.utc_of_offset(self._next_boundary_off)
        try:
            ref = rtp_to_wallclock(
                boundary_rtp, channel_info, wallclock_hint_sec=projected,
            )
        except Exception as exc:  # noqa: BLE001 — detection must not crash audio
            logger.debug("SlotClock divergence check raised: %s", exc)
            return None
        if ref is None:
            return None
        return projected - ref
