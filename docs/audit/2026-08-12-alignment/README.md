# Upstream Alignment & Client-Compat Audit — 2026-08-12

Audit of ka9q-python's alignment with `ka9q-radio` upstream
(`14d780af624e821941708bd0d64fd895a0c80a2a` →
`cedec349f7b4212078de3e007d142b4c64d36546`, 149 commits) and its
sigmond-suite client contracts. Read `findings.md` first — it is the
document Checkpoint A reviews.

## Reports

| File | Task | Contents |
|---|---|---|
| [`upstream.md`](./upstream.md) | 2 | Drift-watcher verdict, full 149-commit classification |
| [`contract.md`](./contract.md) | 3 | Enum-level header diff, `sync_types.py` reconciliation, pin-advance verdict |
| [`control-surface.md`](./control-surface.md) | 4 | Every writable/readable radiod TLV mapped against `RadiodControl`/`status.py` |
| [`clients.md`](./clients.md) | 5 | Per-client symbol contract matrix + no-bypass sweep across all `/opt/git/sigmond` repos |
| [`idempotency.md`](./idempotency.md) | 6–7 | Five idempotency spec questions, code-level verdicts, and live probes against `bee1`/`bee2` radiod |
| [`findings.md`](./findings.md) | 8 | **This audit's output** — every finding above, ranked P0–P3 with evidence links and remediation |

## Executive summary

**Drift verdict:** `CONTRACT: CRITICAL-CHANGE — DO NOT ADVANCE` (Task 3).
The only client-visible enum change in range is `DemodType.N_DEMOD`
shifting 5→6 (upstream inserted `IDLE_DEMOD`); `check_upstream_drift.py`'s
`STREAM_CRITICAL_ENUMS` policy classifies this `fail` regardless of usage.
Zero live code anywhere references `N_DEMOD` by value, so nothing decodes
wrong today — the block is a mechanical policy trigger, not a demonstrated
break (see `findings.md` F2).

**Is the pin safe to advance?** Not yet, per the mechanical gate — but the
remediation is small and well-scoped: regenerate `types.py`
(`sync_types.py --apply`, plus a manual add for the negative-literal
`INVALID_DEMOD` the parser misses — F10), widen `control.py`'s hardcoded
demod-range check (F13), then re-run the watcher. None of this is an
emergency fix for a live bug; it's the normal Task 4/9 capability-exposure
step this project's process expects before a pin with a flagged enum
change advances.

**Findings:** 22 total — **1 P0, 9 P1, 4 P2, 8 P3**. The P0 (`F1`) is
`MultiStream`'s restore path silently changing a channel's SSRC identity
on recovery when a channel has non-default gain/AGC — the shared
recovery substrate every major sigmond client depends on. Several P1s are
environment/deployment findings from the live Task 7 probes (silent
`destination=` failure, a host-routing trap that swallows outbound control
commands with zero error, and a keepalive docstring claim that doesn't
hold empirically) — each is explicitly flagged with its own ambiguity
note rather than forced into a clean bucket, per the audit's ranking
rubric. Zero confirmed no-bypass violations exist among sigmond-authored
Python clients (`clients.md`); the one bypass found (`ka9q-web`, vendored
third-party C) is a policy-scope question, not a ka9q-python code fix.

**Artifact:** (URL added after publication)
