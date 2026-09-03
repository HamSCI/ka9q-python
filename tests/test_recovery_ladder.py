"""Tests for the radiod recovery ladder.

Every client that reads RTP from radiod faces the same three failures when
radiod restarts: its dynamically-created channels are gone, its cached
RTP<->UTC anchor is stale, and its status subscription has a hole.
MultiStream's per-slot auto-restore covers a transient stream drop, but
wspr-recorder documented where that stops:

    "on the recurring storm pattern (radiod recreates SSRCs at Template
     defaults with TTL=0 after a brief outage) MultiStream's per-channel
     5 s verify times out and the channel stays stale."

wspr-recorder then built an escalation ladder above it and kept it to
itself.  On AC0G-B4, 2026-08-30, that asymmetry cost 22 hours: psk and
meteor-scatter have only the tier-1 callback, and meteor-scatter sat dead
from the 03:09Z radiod outage until a human noticed.  One of three clients
solved it; the other two failed silently.

The policy belongs here because the failure is a property of the
clients<->radiod interface, which is what this package IS.  The MECHANISMS
stay with the client -- how to re-create a channel depends on that
client's own configuration -- so the ladder takes them as callables and
owns only the escalation rule.
"""
from __future__ import annotations

import pytest

from ka9q.recovery_ladder import RecoveryAction, RecoveryLadder


class TestHealthyIsQuiet:
    def test_a_healthy_source_asks_for_nothing(self):
        lad = RecoveryLadder()
        for _ in range(50):
            assert lad.observe(healthy=True) is RecoveryAction.NONE

    def test_recovery_clears_the_count_immediately(self):
        """A single bad check followed by recovery must not ratchet a
        later incident toward a full reset."""
        lad = RecoveryLadder()
        lad.observe(healthy=False)          # 1 -> REPROVISION
        assert lad.observe(healthy=True) is RecoveryAction.NONE
        # A fresh incident starts at the bottom of the ladder again.
        assert lad.observe(healthy=False) is RecoveryAction.REPROVISION


class TestEscalation:
    def test_the_ladder_climbs_in_order(self):
        lad = RecoveryLadder()
        assert lad.observe(healthy=False) is RecoveryAction.REPROVISION
        assert lad.observe(healthy=False) is RecoveryAction.REPROVISION
        assert lad.observe(healthy=False) is RecoveryAction.FULL_RESET

    def test_it_stays_at_full_reset_rather_than_giving_up(self):
        """A client that cannot recover must keep trying.  Silence here is
        the failure mode this ladder exists to prevent."""
        lad = RecoveryLadder()
        for _ in range(3):
            lad.observe(healthy=False)
        for _ in range(20):
            assert lad.observe(healthy=False) is RecoveryAction.FULL_RESET

    def test_thresholds_are_configurable(self):
        lad = RecoveryLadder(reprovision_after=2, full_reset_after=4)
        assert lad.observe(healthy=False) is RecoveryAction.NONE
        assert lad.observe(healthy=False) is RecoveryAction.REPROVISION
        assert lad.observe(healthy=False) is RecoveryAction.REPROVISION
        assert lad.observe(healthy=False) is RecoveryAction.FULL_RESET

    def test_a_reset_threshold_below_reprovision_is_refused(self):
        with pytest.raises(ValueError):
            RecoveryLadder(reprovision_after=3, full_reset_after=2)


class TestObservability:
    def test_it_reports_where_it_stands(self):
        lad = RecoveryLadder()
        assert lad.consecutive_degraded == 0
        lad.observe(healthy=False)
        lad.observe(healthy=False)
        assert lad.consecutive_degraded == 2
        assert lad.escalating is True
        lad.observe(healthy=True)
        assert lad.consecutive_degraded == 0
        assert lad.escalating is False

    def test_it_counts_what_it_has_asked_for(self):
        """An operator asking 'is this station self-healing or merely
        quiet?' needs the totals, not just the current state."""
        lad = RecoveryLadder()
        for _ in range(4):
            lad.observe(healthy=False)
        assert lad.reprovisions == 2
        assert lad.full_resets == 2

class TestRestartSelfRung:
    """The rung that repair cannot express: replace me.

    ⛔ AC0G-ND, 2026-09-03.  wspr-recorder's dt-guard caught a slot anchor
    eight minutes off true UTC and performed its deepest in-process repair —
    reset all 17 band recorders, re-seed each from fresh channel_info.  The
    fault returned two cycles later.  Repair, fault, repair, fault, and it
    would have run all night, because the guard counts strikes toward FIRING
    and never counts fires: nothing concluded that repairing was not working.

    Every liveness check stayed green throughout.  The recorder ingested RTP,
    completed 120 s slots at "100% complete", wrote WAVs and ran decoders —
    working and WRONG, which WatchdogSec cannot see by construction.  One
    human restart fixed it, because a poisoned anchor lives in process memory.
    """

    def test_restart_is_opt_in(self):
        # ⛔ An action a caller cannot handle falls through its if/elif and
        # does nothing at all — silent inaction, the failure this ladder
        # exists to prevent.  An existing caller must never receive it.
        ladder = RecoveryLadder()
        assert ladder.restart_after is None
        actions = [ladder.observe(healthy=False) for _ in range(12)]
        assert RecoveryAction.RESTART_SELF not in actions
        assert actions[-1] is RecoveryAction.FULL_RESET

    def test_it_escalates_once_repair_has_demonstrably_failed(self):
        ladder = RecoveryLadder(reprovision_after=1, full_reset_after=2,
                                restart_after=3)
        assert ladder.observe(healthy=False) is RecoveryAction.REPROVISION
        assert ladder.observe(healthy=False) is RecoveryAction.FULL_RESET
        assert ladder.observe(healthy=False) is RecoveryAction.RESTART_SELF
        assert ladder.restarts_requested == 1

    def test_it_keeps_asking_rather_than_going_quiet(self):
        ladder = RecoveryLadder(full_reset_after=2, restart_after=3)
        for _ in range(3):
            last = ladder.observe(healthy=False)
        assert last is RecoveryAction.RESTART_SELF
        assert ladder.observe(healthy=False) is RecoveryAction.RESTART_SELF
        assert ladder.restarts_requested == 2

    def test_one_good_cycle_forecloses_the_restart(self):
        # ⛔ The safety property that matters.  A recorder that recovers on
        # its own must not be killed by a stale count: restarting a working
        # station is worse than the fault it was reacting to.
        ladder = RecoveryLadder(full_reset_after=2, restart_after=3)
        ladder.observe(healthy=False)
        ladder.observe(healthy=False)
        assert ladder.observe(healthy=True) is RecoveryAction.NONE
        assert ladder.consecutive_degraded == 0
        assert ladder.observe(healthy=False) is RecoveryAction.REPROVISION

    def test_restart_cannot_be_configured_before_full_reset(self):
        # Replacing the process is the LAST resort; a ladder that reaches for
        # it before trying repair is not a ladder.
        with pytest.raises(ValueError):
            RecoveryLadder(reprovision_after=1, full_reset_after=3,
                           restart_after=2)

    def test_the_wspr_shape_self_repairs_in_two_cycles_of_futility(self):
        # WSPR's dt-guard fires every 2 cycles of 120 s. Reaching the restart
        # in three observations is ~4 minutes of self-repair instead of never.
        ladder = RecoveryLadder(reprovision_after=1, full_reset_after=2,
                                restart_after=3)
        assert [ladder.observe(healthy=False) for _ in range(3)] == [
            RecoveryAction.REPROVISION,
            RecoveryAction.FULL_RESET,
            RecoveryAction.RESTART_SELF,
        ]
