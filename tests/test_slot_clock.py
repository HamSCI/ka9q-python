"""Unit tests for ka9q.slot_clock.SlotClock."""
import math

import pytest

from ka9q.slot_clock import SlotClock, Slot, rtp_diff


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


def test_settle_delays_completion():
    clk = SlotClock(15.0, SR, settle_sec=2.0)
    clk.anchor(rtp_timestamp=0, utc=900_000_000.0)  # 900000000 % 15 == 0 -> boundary0 at offset 0
    # slot0 spans [0,180000); completes only after 180000 + settle(24000)
    assert clk.advance((180000 + 24000 - 1) & 0xFFFFFFFF) == []
    slots = clk.advance((180000 + 24000 + 1) & 0xFFFFFFFF)
    assert len(slots) == 1 and slots[0].index == 0
