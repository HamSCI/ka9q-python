"""Escalating recovery from a radiod restart.

``MultiStream`` restores a slot whose stream merely stopped.  It cannot
help when radiod itself went away and came back, because the channel the
slot refers to no longer exists: radiod recreates SSRCs at Template
defaults with TTL=0 after a brief outage, the per-channel verify times
out, and the slot stays stale forever while the client goes on looking
healthy.

Recovering from that needs a second tier -- re-issue the channel
requests -- and a third when even that fails -- tear the source down and
reconnect.  wspr-recorder worked this out and kept it in its own
repository.  The cost of that showed on AC0G-B4 on 2026-08-30: after a
radiod outage, wspr re-provisioned 17 stale channels and recovered on
its own, while meteor-scatter, which has only the tier-1 callback, sat
dead for twenty-two hours until a human noticed.  psk-recorder has the
same gap.  One client in three solved it, and the other two failed
silently.

The POLICY belongs here, because recovering from a radiod restart is a
property of the clients<->radiod interface and that interface is what
this package is.  The MECHANISMS stay with the client: what it means to
re-provision depends on that client's own configuration, which this
package should not know.  So the ladder decides *what* should happen and
the caller decides *how*.

Typical use, from whatever loop the client already runs::

    ladder = RecoveryLadder()
    ...
    action = ladder.observe(healthy=(active == expected))
    if action is RecoveryAction.REPROVISION:
        rm.reprovision_stale()
    elif action is RecoveryAction.FULL_RESET:
        rm.full_reset()
    elif action is RecoveryAction.RESTART_SELF:
        log.critical("in-process repair exhausted after %d attempts; "
                     "exiting for systemd to replace us",
                     ladder.consecutive_degraded)
        raise SystemExit(1)          # Restart=always does the rest

One ladder per source.  A client with two radiods keeps two, so a fault
on one never escalates the other.
"""
from __future__ import annotations

import enum

__all__ = ["RecoveryAction", "RecoveryLadder"]


class RecoveryAction(enum.Enum):
    """What the ladder asks the caller to do about this observation."""

    NONE = "none"
    REPROVISION = "reprovision"
    FULL_RESET = "full_reset"
    #: In-process remedies are exhausted; the PROCESS must be replaced.
    #:
    #: ⛔ OPT-IN (`restart_after=None` by default).  An action a caller does
    #: not handle falls through its if/elif chain and does NOTHING, which is
    #: silent inaction — the exact failure this ladder exists to prevent.  So
    #: a caller asks for this rung only once it can act on it.
    RESTART_SELF = "restart_self"


class RecoveryLadder:
    """Consecutive-degraded counter with two escalation thresholds.

    The counter clears the instant a source returns to full health, so a
    single bad check followed by recovery never ratchets a later,
    unrelated incident toward a full reset.
    """

    DEFAULT_REPROVISION_AFTER = 1
    DEFAULT_FULL_RESET_AFTER = 3

    def __init__(
        self,
        reprovision_after: int = DEFAULT_REPROVISION_AFTER,
        full_reset_after: int = DEFAULT_FULL_RESET_AFTER,
        restart_after: int | None = None,
    ) -> None:
        if full_reset_after < reprovision_after:
            raise ValueError(
                f"full_reset_after ({full_reset_after}) must not precede "
                f"reprovision_after ({reprovision_after}): a ladder whose "
                f"top rung comes first is not a ladder"
            )
        if restart_after is not None and restart_after < full_reset_after:
            raise ValueError(
                f"restart_after ({restart_after}) must not precede "
                f"full_reset_after ({full_reset_after}): replacing the "
                f"process is the LAST resort, not an earlier one"
            )
        self.reprovision_after = int(reprovision_after)
        self.full_reset_after = int(full_reset_after)
        self.restart_after = (None if restart_after is None
                              else int(restart_after))
        self.consecutive_degraded = 0
        #: Totals since construction, so an operator can tell a station
        #: that is quietly self-healing from one that is merely quiet.
        self.reprovisions = 0
        self.full_resets = 0
        self.restarts_requested = 0

    @property
    def escalating(self) -> bool:
        """True while a fault is open (at least one degraded check)."""
        return self.consecutive_degraded > 0

    def observe(self, healthy: bool) -> RecoveryAction:
        """Record one health check and return the action it calls for."""
        if healthy:
            self.consecutive_degraded = 0
            return RecoveryAction.NONE

        self.consecutive_degraded += 1
        n = self.consecutive_degraded
        if self.restart_after is not None and n >= self.restart_after:
            # Checked before FULL_RESET because it is the higher rung: once
            # in-process repair has demonstrably failed this many times,
            # repeating it is the behaviour we are fixing.
            self.restarts_requested += 1
            return RecoveryAction.RESTART_SELF
        if n >= self.full_reset_after:
            # Stays here rather than giving up.  A client that cannot
            # recover must keep trying: going quiet is the exact failure
            # this ladder exists to prevent.
            self.full_resets += 1
            return RecoveryAction.FULL_RESET
        if n >= self.reprovision_after:
            self.reprovisions += 1
            return RecoveryAction.REPROVISION
        return RecoveryAction.NONE

    def reset(self) -> None:
        """Forget the current fault (not the totals)."""
        self.consecutive_degraded = 0
