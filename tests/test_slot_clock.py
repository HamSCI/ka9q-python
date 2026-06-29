"""Unit tests for ka9q.slot_clock.SlotClock."""
import math

import pytest

from ka9q.slot_clock import (
    SlotClock,
    Slot,
    SlotClockDesyncError,
    rtp_diff,
    _SAFE_UNWRAP_SAMPLES,
)


SR = 12000


def test_rtp_diff_wrap():
    assert rtp_diff(10, 5) == 5
    assert rtp_diff(5, 10) == -5
    # wrap: 2 just past 0xFFFFFFFE
    assert rtp_diff(2, 0xFFFFFFFE) == 4
    assert rtp_diff(0xFFFFFFFE, 2) == -4


def test_rejects_non_integer_cadence():
    # 7.4 s * 12000 = 88800 ok; 7.5*12000=90000 ok; but 0.0001 off must fail
    SlotClock(7.5, SR)        # ok (90000 samples)
    SlotClock(15.0, SR)       # ok
    SlotClock(120.0, SR)      # ok
    with pytest.raises(ValueError):
        SlotClock(15.000001, SR)


def test_cadence_samples():
    assert SlotClock(15.0, SR).cadence_samples == 180000
    assert SlotClock(7.5, SR).cadence_samples == 90000
    assert SlotClock(120.0, SR).cadence_samples == 1440000


def test_boundaries_epoch_aligned():
    """First boundary lands on a cadence multiple of UTC, regardless of where
    the anchor falls within a slot."""
    clk = SlotClock(15.0, SR, settle_sec=0.0)
    # anchor at an awkward 7.3 s into a slot: utc = 1000.000 ... pick utc not
    # on a 15 s grid point.
    anchor_utc = 1_000_000_007.3
    clk.anchor(rtp_timestamp=0, utc=anchor_utc)
    # next boundary utc must be a multiple of 15
    boundary_utc = clk.utc_of_offset(clk._next_boundary_off)
    assert abs((boundary_utc % 15.0)) < 1e-3 or abs((boundary_utc % 15.0) - 15.0) < 1e-3


def test_advance_yields_aligned_slots_no_drift():
    clk = SlotClock(15.0, SR, settle_sec=1.5)
    # anchor exactly on a grid point for easy reasoning
    clk.anchor(rtp_timestamp=1000, utc=1_000_000_005.0)  # 005 -> next boundary 015
    # feed latest rtp far enough to complete several slots
    # boundary0 offset = (15-5)*12000 = 120000 ; +cadence+settle to complete
    slots = []
    # advance in steps to simulate streaming
    for secs in range(11, 80, 1):
        latest_rtp = (1000 + secs * SR) & 0xFFFFFFFF
        slots.extend(clk.advance(latest_rtp))
    assert len(slots) >= 3
    # every slot start_utc is a multiple of 15, and consecutive starts differ
    # by EXACTLY cadence_samples (no float drift)
    for s in slots:
        assert abs(s.start_utc % 15.0) < 1e-3 or abs(s.start_utc % 15.0 - 15.0) < 1e-3
        assert s.n_samples == 180000
    for a, b in zip(slots, slots[1:]):
        assert b.index == a.index + 1
        # start_utc advances by exactly 15 s
        assert abs((b.start_utc - a.start_utc) - 15.0) < 1e-9


def test_advance_handles_rtp_wrap():
    clk = SlotClock(15.0, SR, settle_sec=0.5)
    # anchor near the 32-bit wrap
    base = 0xFFFFFFFF - 5 * SR  # ~5 s before wrap
    clk.anchor(rtp_timestamp=base & 0xFFFFFFFF, utc=1_000_000_000.0)
    got = []
    for secs in range(1, 60):
        latest = (base + secs * SR) & 0xFFFFFFFF
        got.extend(clk.advance(latest))
    assert len(got) >= 2
    for a, b in zip(got, got[1:]):
        assert b.index == a.index + 1
        assert abs((b.start_utc - a.start_utc) - 15.0) < 1e-9


def test_offset_of_rtp_roundtrip():
    clk = SlotClock(7.5, SR)
    clk.anchor(rtp_timestamp=12345, utc=1_000.0)
    off = 90000 * 3 + 17
    rtp = clk.rtp_of_offset(off)
    assert clk.offset_of_rtp(rtp) == off


def test_long_run_past_signed32_window_keeps_harvesting():
    """Regression: a stream running past 2**31 samples from the anchor must
    keep harvesting slots.  A bare anchor-relative signed-32 diff aliases at
    ~49.7 h @ 12 kHz and silently stops `advance` (real RF, no slots -> 0
    decodes); the high-water unwrap must carry through it.

    Steps the leading edge in 1-hour jumps (43.2M samples << 2**31, so each
    individual unwrap is unambiguous) across the ~49.7 h boundary out to 55 h.
    """
    clk = SlotClock(15.0, SR, settle_sec=1.5)
    clk.anchor(rtp_timestamp=0, utc=900_000_000.0)  # multiple of 15 -> boundary0 at offset 0
    samples_per_hour = 3600 * SR  # 43,200,000  (< 2**31 = 2,147,483,648)
    boundary_hour = (2 ** 31) / samples_per_hour  # ~49.7 h
    last_index = -1
    crossed = False
    for hour in range(1, 56):
        latest_off = hour * samples_per_hour
        latest_rtp = latest_off & 0xFFFFFFFF
        for s in clk.advance(latest_rtp):
            # contiguous, monotonic indices and exact 15 s grid throughout
            assert s.index == last_index + 1
            assert abs(s.start_utc - (900_000_000.0 + s.index * 15.0)) < 1e-3
            last_index = s.index
        if hour > boundary_hour:
            crossed = True
            # still producing slots well past the old-bug cutoff
            assert last_index > int(boundary_hour * 3600 / 15)
    assert crossed
    # 55 h / 15 s ≈ 13200 slots; we should be near the end, not stalled early
    assert last_index > 13000


def test_offset_of_rtp_unwraps_past_window():
    """offset_of_rtp must return the true 64-bit offset past 2**31, not alias."""
    clk = SlotClock(15.0, SR)
    clk.anchor(rtp_timestamp=7, utc=900_000_000.0)
    # walk the high-water forward in sub-2**31 steps to 3 billion samples
    step = 2 ** 30  # 1,073,741,824
    off = 0
    for _ in range(3):
        off += step
        assert clk.offset_of_rtp((7 + off) & 0xFFFFFFFF) == off
    assert off > 2 ** 31  # we genuinely crossed the signed-32 boundary


def test_safe_unwrap_limit_allows_full_half_window_step():
    """A single step of exactly the safe limit (half the signed-32 window) is
    still unambiguous and must be accepted — the guard rejects only *past* it."""
    clk = SlotClock(15.0, SR)
    clk.anchor(rtp_timestamp=0, utc=900_000_000.0)
    off = _SAFE_UNWRAP_SAMPLES
    assert clk.offset_of_rtp(off & 0xFFFFFFFF) == off


def test_offset_of_rtp_raises_on_ambiguous_jump():
    """A single unwrap step past the safe half-window can alias a huge forward
    jump into a backward step; offset_of_rtp must raise rather than guess."""
    clk = SlotClock(15.0, SR)
    clk.anchor(rtp_timestamp=0, utc=900_000_000.0)
    with pytest.raises(SlotClockDesyncError):
        clk.offset_of_rtp((_SAFE_UNWRAP_SAMPLES + 1) & 0xFFFFFFFF)


def test_advance_big_jump_fails_loud_and_drops_anchor(caplog):
    """Regression: a single advance() forward jump > 2**31 from the anchor used
    to alias to a backward step and silently harvest 0 slots forever (live RF,
    zero decodes).  The guard must instead log loudly, drop the anchor, and
    return [] so the caller re-anchors — turning a silent stall into recovery."""
    clk = SlotClock(15.0, SR, settle_sec=1.5)
    clk.anchor(rtp_timestamp=0, utc=900_000_000.0)
    # 50 h in samples: 2,160,000,000 > 2**31 (2,147,483,648) in ONE call.
    big = 50 * 3600 * SR & 0xFFFFFFFF
    with caplog.at_level("ERROR"):
        slots = clk.advance(big)
    assert slots == []                 # no garbage slots
    assert not clk.anchored            # anchor dropped -> caller must re-anchor
    assert any("SlotClock" in r.message for r in caplog.records)
    # and recovery works: re-anchor near the real leading edge, harvest normally
    clk.anchor(rtp_timestamp=big, utc=900_180_000.0)  # multiple of 15
    out = clk.advance((big + 180000 + 24000 + 1) & 0xFFFFFFFF)
    assert len(out) == 1 and out[0].index == 0


def test_settle_delays_completion():
    clk = SlotClock(15.0, SR, settle_sec=2.0)
    clk.anchor(rtp_timestamp=0, utc=900_000_000.0)  # 900000000 % 15 == 0 -> boundary0 at offset 0
    # slot0 spans [0,180000); completes only after 180000 + settle(24000)
    assert clk.advance((180000 + 24000 - 1) & 0xFFFFFFFF) == []
    slots = clk.advance((180000 + 24000 + 1) & 0xFFFFFFFF)
    assert len(slots) == 1 and slots[0].index == 0
