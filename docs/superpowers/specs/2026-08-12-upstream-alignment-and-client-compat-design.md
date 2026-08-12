# Upstream Alignment & Client Compatibility — Design

**Date:** 2026-08-12
**Status:** Approved (approach A: audit-first, three phases)

## Problem

ka9q-python's ka9q-radio pin (`14d780af`) is 144 commits behind upstream
`origin/main` (`1c0a4231`, tagged 2026.08.10). The drift watcher classifies
the gap as warn-level: headers touched, no stream-critical TLV field removed
or value-shifted. Meanwhile, downstream sigmond-suite clients depend on
ka9q-python for two guarantees that have never been audited end-to-end:

1. **API/behavioral stability** — clients receive the same payload and the
   same interaction semantics across ka9q-python upgrades.
2. **Operational idempotency** — operations converge when repeated:
   re-creating an existing channel, deterministic SSRC allocation across
   restarts, recovery landing in an identical channel state, keepalives
   preserving settings.

ka9q-python is the **mandatory control path**: any client requiring a feed
from an RX888 goes through ka9q-python to control radiod. There is no
side-channel. Alignment therefore means more than covering current client
usage — the full radiod control surface must be reachable through
`RadiodControl`, or future clients will be blocked.

Known drift already found during scoping:

- CLAUDE.md's client roster is stale: hfdl-recorder and codar-sounder no
  longer import `ka9q`; **meteor-scatter** does (and is unlisted).
- CLAUDE.md names the test radiod `bee1-hf-status.local`; the actual status
  DNS names are `bee1-status.local` / `bee2-status.local`.
- Some clients reach into non-public internals:
  `ka9q.control.encode_int`, `encode_double`, `CMD`.

## Success criteria

1. A committed audit report answering: (a) is any client-relied behavior at
   risk from the 144-commit upstream delta; (b) which new radiod
   capabilities is ka9q-python not exposing; (c) which parts of the full
   radiod control surface are unreachable through `RadiodControl` (the
   mandatory-path completeness gap).
2. Pin advanced to current upstream; `types.py`, `ka9q_radio_compat`,
   `ka9q/compat.py` regenerated together; full pytest green.
3. A guardrail suite in-repo that fails **locally** before a change breaks
   hf-timestd, wspr-recorder, psk-recorder, or meteor-scatter in the field.

**Non-goals:** no changes to client repos (findings about them go in the
report); no contact with b3/b4 (dev/production).

## Test environment

- Integration hosts: **b1.local / b2.local** (status: `bee1-status.local`,
  `bee2-status.local`). Full integration allowed, including ephemeral
  channel create/destroy. Possibly older radiod builds.
- b3/b4: off-limits.
- New-capability integration tests must probe the radiod version and skip
  (with explicit reason) when the host is too old to support the feature.
- Ephemeral test channels use a dedicated SSRC range and are always
  destroyed in teardown, including on failure.

## Phase 1 — Audit (read-only)

Three tracks feeding one ranked report.

### 1a. Upstream review

- Clone ka9q-radio fresh into the session scratchpad (the
  `/opt/git/sigmond/ka9q-radio` checkout is owned by user `sigmond`, cannot
  be fetched by `hamsci`, and its refs are ~1 day stale; a fresh clone
  solves both without touching it).
- Walk `14d780af..HEAD`; classify every commit: payload/RTP-affecting,
  status-TLV contract, new capability worth exposing, or internal-only.
- Authoritative contract check: header diff (`status.h`, `rtp.h`,
  `multicast.h`) pinned-vs-current; re-run
  `scripts/check_upstream_drift.py` against the fresh clone.
- **Control-surface completeness check:** enumerate every settable/readable
  radiod parameter at current upstream (status.h TLVs and the command
  paths radiod actually honors) and map each to its `RadiodControl`
  equivalent. Since ka9q-python is the only control path for RX888-fed
  clients, any unmapped parameter is a capability gap finding — reported
  even if no current client uses it.

### 1b. Client contract matrix

From the source of the four client repos (hf-timestd, wspr-recorder,
psk-recorder, meteor-scatter): every imported `ka9q` symbol, how it is
called, which behaviors are load-bearing (payload format, timing,
`StatusType`/`Encoding` value reliance), plus all reaches into non-public
internals.

### 1c. Idempotency audit

Code-read, then empirical verification on b1/b2 for anything questionable:

- `create_channel` on an already-existing channel — converges or duplicates?
- `allocate_ssrc` determinism and collision behavior across restarts.
- `ManagedStream` / `ChannelMonitor` recovery — is the recovered channel
  state identical to the original?
- Keepalive setting preservation (the `731ce5e` encoding-loss bug class —
  are there siblings?).
- `PacketResequencer` payload continuity guarantees.

### Output

Ranked findings, committed as markdown in `docs/`, plus an artifact page
for reading. Each finding carries evidence (commit hash, file:line, or
observed behavior on b1/b2).

## Phase 2 — Remediation (strictly findings-driven)

- `scripts/sync_types.py --apply`; commit `types.py`, `ka9q_radio_compat`,
  `ka9q/compat.py` together per the CLAUDE.md workflow; full pytest after.
- Each audit finding lands as a small, separate commit. New capabilities
  are implemented TDD-first.
- Internals that clients reach into get a public, documented equivalent
  (or documented promotion) rather than silent breakage exposure.
- CLAUDE.md corrections: client roster, status hostnames.
- If (contrary to the drift tool's current read) an upstream removal hits a
  client-used field: pause and coordinate with downstream before advancing
  the pin, per the CLAUDE.md operator workflow.

## Phase 3 — Guardrails (durable)

- `scripts/gen_client_manifest.py` — scans `/opt/git/sigmond` for `ka9q`
  usage; writes checked-in `tests/client_usage_manifest.json`. Regenerable
  anytime; the checked-in copy keeps CI independent of this machine.
- `tests/test_client_contract.py` — every manifest symbol must import and
  match a signature snapshot; a breaking change fails with the affected
  client's name in the message.
- Idempotency integration tests: create-twice converges; restart recovery
  lands identical state; keepalive preserves encoding. Version-probed and
  skip-guarded for b1/b2's possibly-older radiod.
- Wire into the normal pytest run; document in CLAUDE.md.

## Risks

| Risk | Handling |
|------|----------|
| b1/b2 radiod too old for a new-capability test | Version probe → skip with explicit reason, never a false failure |
| Fresh clone reveals critical drift beyond local checkout's knowledge | Report finding, not a blocker |
| Test channels left behind on b1/b2 | Dedicated SSRC range + unconditional teardown |
| Upstream removal of a client-used field | Pause, coordinate downstream before pin advance |
