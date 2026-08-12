# Ranked Findings — 2026-08-12 Upstream Alignment & Client-Compat Audit

**Audit range:** pin `14d780af624e821941708bd0d64fd895a0c80a2a` → AUDIT_HEAD
`cedec349f7b4212078de3e007d142b4c64d36546` (149 upstream commits).
**Source reports:** `upstream.md` (Task 2), `contract.md` (Task 3),
`control-surface.md` (Task 4), `clients.md` (Task 5), `idempotency.md`
(Tasks 6–7).

Every finding below traces to one or more of those five reports. IDs are
assigned in rank order (`F1` most severe) and are stable — the Checkpoint-A
follow-up plan should reference these IDs directly rather than re-deriving
them.

**Ranking rubric** (from the task brief):
- **P0** — breaks a client today.
- **P1** — violates a spec guarantee (idempotency, no-bypass, mandatory-path
  completeness), whether or not it has visibly broken a client yet.
- **P2** — capability gap, no current consumer.
- **P3** — doc/hygiene.

Several findings straddle categories (mechanically-triggered gates with no
live usage; environment-dependent breakage). Where that's true, the entry
says so explicitly rather than forcing a clean bucket.

**Counts:** P0 = 1, P1 = 9, P2 = 4, P3 = 8. **Total = 22.**

---

## Summary table

| ID | Priority | Claim |
|---|---|---|
| F1 | P0 | `MultiStream` restore drops agc/gain/filter-edges and can silently land on a *different SSRC* |
| F2 | P1 | `DemodType.N_DEMOD` value-shifted 5→6 upstream; drift watcher says DO-NOT-ADVANCE, zero live usage |
| F3 | P1 | `create_channel()` on an existing SSRC is a delta-update, not an atomic reset |
| F4 | P1 | `tune()` never updates `_requested_encoding`, defeating the 731ce5e keepalive-encoding fix for that path |
| F5 | P1 | `create_channel()` without `destination=` silently creates nothing on real deployments |
| F6 | P1 | `set_lock()` sends correct bytes but radiod has no handler — silent no-op |
| F7 | P1 | Bare `poll_channel()` does not extend LIFETIME, contradicting the docstring |
| F8 | P1 | Host multicast routing can silently swallow every outbound control command |
| F9 | P1 | `ka9q-web` (vendored C) sends control commands directly, bypassing ka9q-python |
| F10 | P1 | `sync_types.py`'s enum parser is blind to negative literals (e.g. `INVALID_DEMOD=-1`) |
| F11 | P2 | Five spectrum-mode TLVs unreachable (or only reachable via a private hack) through `RadiodControl` |
| F12 | P2 | `SETOPTS` status field is emitted by radiod but never decoded |
| F13 | P2 | `set_demod_type()`'s range check hardcodes `<=4`, stale once `IDLE_DEMOD` ships |
| F14 | P2 | `resolve_multicast_address` is public but not re-exported from `ka9q/__init__.py` |
| F15 | P3 | hf-timestd hand-encodes filter edges instead of calling the existing `set_filter()` |
| F16 | P3 | `rtp_to_wallclock` deprecated alias emits no `DeprecationWarning` |
| F17 | P3 | `StreamQuality` field-access style differs by client (loud vs. silent on rename) |
| F18 | P3 | `SlotClock.offset_of_rtp()` called unguarded in psk-recorder/meteor-scatter |
| F19 | P3 | meteor-scatter never wires up `StatusListener`; slide-follow is inert |
| F20 | P3 | hf-timestd hand-rolls an RTP-only multicast join instead of using `RadiodStream` |
| F21 | P3 | Two dead `ka9q` imports in hf-timestd |
| F22 | P3 | Resequencer cross-run delivery consistency (Q5) remains unresolved needs-empirical |

---

## P0

### F1 — `MultiStream` restore can silently change channel identity

**Claim:** On radiod-restart recovery, `MultiStream._attempt_restore()` calls
`ensure_channel()` with only 5 of the 10 identity/config fields it was given
at creation — `agc_enable`, `gain`, `low_edge`, `high_edge`, `kaiser_beta`
are dropped because `_ChannelSlot` never stores them. Since `gain`/
`agc_enable` are inputs to `allocate_ssrc()`'s hash, a restore of a channel
that was created with non-default gain/AGC computes a **different SSRC**
than the one being restored — not just a misconfigured channel, but a
structurally different one, silently.

**Evidence:** `idempotency.md` § Step 2 sibling-bug scan, table row 6
(`MultiStream._attempt_restore`, `multi_stream.py:687-724` vs.
`_ChannelSlot`, `multi_stream.py:66-99`); cross-referenced against
`multi_stream.py:696-699`'s own SSRC-rekey handling, which the report reads
as proof the divergence is a reachable, anticipated runtime path, not
hypothetical. `MultiStream` is confirmed (CLAUDE.md, `clients.md` Step 1)
to be the shared substrate for hf-timestd, wspr-recorder, psk-recorder,
meteor-scatter, hfdl-recorder, codar-sounder, hf-tec, superdarn-sounder.

**Why P0, with an honest caveat:** the source report calls this its
"highest-confidence finding." The failure precondition (radiod restart +
non-default `agc_enable`/`gain` on a `MultiStream`-managed channel) is
exactly the scenario `MultiStream`'s self-healing exists to handle, so this
is not a corner case — it is the recovery path every major client relies
on. It was **not** empirically triggered this audit (Task 7's live probes
covered `ManagedStream` recovery, not `MultiStream`'s, and no client's
fleet config was checked for non-default gain/AGC), so "breaks a client
today" is asserted from code + design analysis, not a captured live
failure. Recommend verifying against current fleet channel configs as the
first step of remediation triage, in parallel with the code fix.

**Remediation:** Add `agc_enable`, `gain`, `low_edge`, `high_edge`,
`kaiser_beta` to `_ChannelSlot` and thread them through
`add_channel()`→`_attempt_restore()`, the same way `lifetime`/`encoding`
were already fixed for this exact bug class in `731ce5e`.

---

## P1

### F2 — `DemodType.N_DEMOD` value shift blocks pin advance (drift watcher: DO-NOT-ADVANCE)

**Claim:** Upstream commit `654fda5e` inserts `IDLE_DEMOD` before the
terminal `N_DEMOD` sentinel in `enum demod_type` (`src/radio.h`), shifting
`N_DEMOD` from `5` to `6` and adding `INVALID_DEMOD = -1`. This is the only
client-visible enum change in the 149-commit range. `scripts/check_upstream_drift.py`
places the whole `DemodType` enum in `STREAM_CRITICAL_ENUMS`, so any
value-shift in it — including this one, on a sentinel never itself placed
on the wire — is mechanically classified `fail`, giving Task 3's verdict
`CONTRACT: CRITICAL-CHANGE — DO NOT ADVANCE`.

**Evidence:** `contract.md` §§ Step 1–4 and Verdict (full justification
chain); `upstream.md` § Expansions, commit `654fda5e`.

**Ambiguity, stated explicitly:** the usage disjunct is **FALSE** — zero
references to `DemodType.N_DEMOD` by value exist anywhere in `ka9q/` or any
of six sigmond-suite client repos checked; the five wire-transmitted demod
codes (`LINEAR_DEMOD`…`SPECT2_DEMOD`) are unchanged. Nothing decodes wrong
today. The `fail` verdict is triggered by the allowlist's blanket policy
alone ("any shift in this enum is critical, regardless of member"), not by
a demonstrated live break. It is ranked P1 rather than P2 because the drift
watcher's DO-NOT-ADVANCE gate is itself a spec guarantee this project
enforces mechanically (mandatory-path completeness of the pin-advance
process), and Checkpoint A is explicitly gated on this verdict.

**Remediation:** Run `scripts/sync_types.py --apply` to add `IDLE_DEMOD=5`
and bump `N_DEMOD` to `6` in `types.py` (plus a manual add for
`INVALID_DEMOD=-1`, see F10); widen `control.py:2854`'s hardcoded bound
(see F13); then re-run the drift watcher to confirm PASS before advancing
the pin.

### F3 — `create_channel()` on an existing SSRC is a delta-update, not an atomic reset

**Claim:** Calling `create_channel()` again for an SSRC that already exists
on radiod does not reset the channel to a template/preset baseline.
`decode_radio_commands()` only mutates fields whose TLV is present in the
new packet; anything the caller's arguments left as `None`/falsy (e.g.
`sample_rate`, `low_edge`, `destination`, `lifetime`) keeps its prior
radiod-side value. Confirmed **live** in Task 7, with an important
refinement: fields the active preset's config stanza *does* define get
reset to the preset default on every `PRESET`-bearing create regardless of
delta-update semantics, while fields the preset doesn't define keep
whatever was set by an earlier call. Either way, the docstring's "uses
radiod's default if not set" claim is true only for a fresh SSRC.

**Evidence:** `idempotency.md` § Q1 (code-level verdict: DEFECT); § Round 2,
Probe 1b (`probe_create_twice_variant.py`, live confirmation and the
preset-default refinement).

**Remediation:** Update the `create_channel`/`ensure_channel` docstrings to
state the actual re-create semantics precisely (preset-defined fields
reset to preset default; preset-undefined fields persist stale values); if
atomic-reset semantics are wanted for re-create, that requires either a new
radiod-side flag or ka9q-python always sending every field explicitly on
every create (never omitting a TLV for a `None` value on a known-existing
SSRC).

### F4 — `tune()` never updates `_requested_encoding`, defeating the keepalive-encoding fix for that path

**Claim:** `create_channel()` and `set_output_encoding()` both write
`self._requested_encoding[ssrc]`, which `set_channel_lifetime()`'s
`731ce5e` fix and `verify_channel()`'s default `expected_encoding` both
read. `tune(ssrc, encoding=X)` sends the `OUTPUT_ENCODING` TLV but is not
one of the two write sites for that dict — confirmed by grep (exactly 2
write sites, `control.py:1484` and `:2896`; `tune()` is not among them).
Any channel last retuned via `tune()` has a stale or absent
`_requested_encoding` entry, so subsequent keepalives re-assert the wrong
encoding and `verify_channel()`'s default check compares against the wrong
value.

**Evidence:** `idempotency.md` § Step 2 table row 3
(`control.py:2098-2231`).

**Ambiguity:** hf-timestd's `channel_manager.py` calls
`.tune(ssrc, frequency_hz, preset, sample_rate, encoding=int)` in
production (`clients.md` § hf-timestd symbol matrix) — this is a plausible,
not confirmed, live-impact path; whether hf-timestd's specific call
sequence (does it call `set_channel_lifetime`/`verify_channel` afterward
against the same SSRC?) actually triggers the divergence was not traced
end-to-end this audit. Flagged for empirical/trace verification as part of
remediation, not asserted as a confirmed P0.

**Remediation:** Add `self._requested_encoding[ssrc] = encoding` to
`tune()` whenever `encoding` is not `None`, mirroring `create_channel`'s
and `set_output_encoding`'s existing write.

### F5 — `create_channel()` without `destination=` silently creates nothing

**Claim:** `create_channel`'s docstring claims omitting `destination=`
"uses radiod's config-file default." On both `bee1-status.local` and
`bee2-status.local`, omitting `destination=` produces no error and no
visible channel — `discover_channels()` never shows the SSRC. Explicit
`destination=` only succeeds when it names a multicast group radiod is
*already* configured to accept output on (a synthetic/unused address also
silently failed); the deployment restricts creation to a fixed set of
pre-configured output destinations and has no config-file default to fall
back to. No error is raised at the transport level — the only symptom is a
later `poll_channel()`/`ensure_channel()` timeout, which doesn't point at
`destination` as the cause.

**Evidence:** `idempotency.md` § "Empirical results (Task 7) — Round 2",
"Second requirement found" subsection; § "Concerns / follow-ups for
Task 8" item 3.

**Ambiguity:** whether this is (a) a deployment-specific radiod
configuration choice (some radiods do have a config-file default) or (b) a
client-side gap (ka9q-python could implement the discovery-based fallback
its own docstring promises) was not resolved. No current client in
`clients.md`'s symbol matrix was confirmed to omit `destination=` in
production, so this was not observed as an active break — but it is a
silent-failure mode with zero diagnostic signal, which is severe if any
caller (current or future) relies on the documented fallback.

**Remediation:** Either implement the promised fallback (resolve a default
destination via discovery or config query before create) or make the
omission fail loudly (raise `ValidationError` when `destination=None` and
no fallback is available) instead of silently sending an incomplete
command; update the docstring either way.

### F6 — `set_lock()` sends correct bytes but radiod silently ignores them

**Claim:** `set_lock()` builds and sends a well-formed `LOCK` TLV, and
`status.h` documents `LOCK` as settable — but `decode_radio_commands()`
has no `case LOCK:` at either the pin or AUDIT_HEAD; the TLV falls through
to `default: break;` and is discarded. `set_lock()` returns normally
either way, so a caller believes the tuner is protected from retune when
it is not, with nothing in the wire protocol or ka9q-python distinguishing
"applied" from "silently ignored."

**Evidence:** `control-surface.md` § Gaps, "Bonus finding: a write that
radiod silently ignores"; `idempotency.md` § "Related failure mode:
`set_lock()`".

**Ambiguity:** no current sigmond-suite client was found calling
`set_lock()` (not present in any client's symbol matrix in `clients.md`),
so this has not broken anyone today; it is a live trap for any future
caller, and the bytes ka9q-python sends are already correct — this is an
upstream radiod omission, not a ka9q-python encoding bug, so no code fix
on the ka9q-python side can make locking actually work.

**Remediation:** Document prominently (docstring + possibly a runtime
warning) that `set_lock()` currently has no effect against real radiod
until upstream adds a `case LOCK:` handler; consider raising or logging
rather than returning silently.

### F7 — Bare `poll_channel()` does not extend channel LIFETIME

**Claim:** `set_channel_lifetime()`'s docstring claims "Polling
auto-extends a non-zero lifetime to at least ~20s, so a client... should
call this method **(or any other poll)** periodically as a keep-alive."
Live Task 7 testing (Probe 2b, Phase A) shows a channel with `lifetime=1000`
frames (~20s) expired on schedule despite four `poll_channel()` calls
before expiry; only explicit `set_channel_lifetime()` calls (Phase B) kept
the channel and its encoding alive across 30s.

**Evidence:** `idempotency.md` § "Empirical results (Task 7) — Round 2",
Probe 2b; § "Concerns / follow-ups for Task 8" item 2.

**Ambiguity:** unclear whether this is universal or specific to this
radiod build/version — the report explicitly flags it as needing an
AUDIT_HEAD `src/radio_status.c` cross-check. No current client's
lifetime-management code was confirmed to rely on bare polling alone (the
audited clients appear to call `set_channel_lifetime` explicitly for
"lifetime management"), so not confirmed as an active break — but the
docstring is demonstrably false on at least one real deployment, and a
client written to the docstring's letter would silently lose channels.

**Remediation:** Narrow the docstring to "call `set_channel_lifetime()` (or
any command that includes a LIFETIME tag) periodically" and drop the "(or
any other poll)" claim, unless/until the AUDIT_HEAD cross-check shows a
radiod version where it holds.

### F8 — Host multicast routing can silently swallow every outbound control command

**Claim:** The audit sandbox routes all locally-originated `239.0.0.0/8`
sends via `dev lo` (confirmed with `ip route get`/`tcpdump`), so every
`create_channel`/`remove_channel`/`ensure_channel` call sent without an
explicit `interface=` silently never reaches the network — no socket
error, no exception, just a command that vanishes. `discover_channels()`
looks like a positive reachability signal but only exercises the read
path (inbound multicast from the real interface), masking the write-path
failure entirely. `RadiodControl(interface=<NIC IP>)` fixes it by forcing
`IP_MULTICAST_IF`.

**Evidence:** `idempotency.md` § "Empirical results (Task 7) — Round 1",
"Root cause" subsection (full tcpdump-verified diagnosis); § Round 2 intro
(the fix and its mechanism).

**Ambiguity, stated explicitly per the task brief:** this straddles
environment/infra and library-defect categories. It is not a ka9q-python
code bug — `interface=` already exists and works — but (a) nothing detects
or warns when a client is in this state (silent total control-plane
failure with only downstream symptoms like `ensure_channel` timeouts), and
(b) the SDD ledger explicitly flags this as a "possible root cause of past
mysteries" for any production host sharing this routing configuration, not
just the audit sandbox. Whether any current production deployment has
this routing quirk is unknown/unverified. Ranked P1 (mandatory-path
completeness: a sent command should reach radiod or the caller should be
told it didn't) rather than P0 (no confirmed production host affected) or
P3 (severity is too high, and the "past mysteries" hint too suggestive, to
file as pure hygiene).

**Remediation:** Document the `interface=` requirement and this failure
mode prominently (a short troubleshooting section: "if `discover_channels`
works but created channels never appear, check `ip route get
<multicast-group>`"); consider a lightweight self-check (e.g. a
loopback-echo probe RadiodControl could optionally run on construction, or
surfaced as a documented `verify_write_path()` helper) since generic
send-side ACK is not otherwise available in this protocol.

### F9 — `ka9q-web` (vendored C) sends control commands directly, bypassing ka9q-python

**Claim:** `ka9q-web`, a vendored third-party C web dashboard living under
`/opt/git/sigmond`, hand-decodes status TLVs and calls
`control_set_frequency()` to send real tune/frequency commands directly to
radiod's control socket. This is genuine direct control-plane traffic
bypassing ka9q-python — the only such case found across a 20-repo sweep.

**Evidence:** `clients.md` § "Special case: `ka9q-web` and `onion`".

**Ambiguity:** literally meets the P1 "violates no-bypass" criterion, but
it is not a sigmond-authored Python client and structurally cannot import
a Python library — it predates ka9q-python and is maintained upstream by a
different author. No confirmed bypass exists among any sigmond-authored
Python repo (`clients.md` § "Confirmed bypass list": NONE). This is
recorded as a policy-scope question ("should `ka9q-web` be excluded from
the no-bypass policy's repo list the way `ka9q-radio` already is?") rather
than a ka9q-python remediation item — no code change here can fix it.

**Remediation:** Not a ka9q-python code fix. Decide, at the policy-owner
level, whether `ka9q-web` is in-scope for the no-bypass policy at all; if
it is, track it as a distinct migration/wrapper effort (e.g. a thin Python
shim), separate from this audit's Task 9/10 remediation scope.

### F10 — `sync_types.py`'s enum parser is blind to negative literals

**Claim:** `parse_c_enum()`'s regex (shared by both `sync_types.py` and
`check_upstream_drift.py`) requires `(\d+)` for a member's value — no sign
handling. Against `INVALID_DEMOD = -1, // used as sentinel`, the regex
fails to match the whole line, which is silently `continue`d past. Both
tools are therefore blind to `INVALID_DEMOD` in this diff; `sync_types.py
--apply` would silently omit it even though it's a real member of the C
enum.

**Evidence:** `contract.md` § Step 3 "Reconciliation with Step 2's table".

**Ambiguity:** this did not change Task 3's verdict here — `INVALID_DEMOD`
is an `added` member, which is always `warn`-severity regardless of
whether the tooling sees it, so the miss happened to be inconsequential
this time. It is filed P1 rather than P2/P3 because the watcher's entire
purpose is to be a complete safety net for future upstream drift, and a
parser that silently drops a class of enum member (any negative sentinel)
is a real gap in that completeness guarantee — the next negative-valued
member added upstream might not be similarly harmless. CLAUDE.md frames
this tooling as "a repo-level dev tool concern, not API surface," which
argues for a lower priority; the safety-net-completeness argument argues
for P1. Both readings are defensible — recorded here rather than silently
picking one.

**Remediation:** Extend `parse_c_enum()`'s regex to accept an optional
leading `-` in the value-capture group; add a regression test using
`INVALID_DEMOD = -1` (or an equivalent fixture) so this class of bug can't
regress silently again.

---

## P2

### F11 — Five spectrum-mode TLVs unreachable through `RadiodControl`'s public API

**Claim:** `set_spectrum()` covers `RESOLUTION_BW`, `BIN_COUNT`,
`CROSSOVER`, `SPECTRUM_SHAPE` only. `WINDOW_TYPE`, `SPECTRUM_AVG`, and
`SPECTRUM_OVERLAP` are writable by radiod but reachable only through
`SpectrumStream._send_spectrum_command()`'s hand-rolled raw buffer
(bypassing `RadiodControl`'s public surface entirely) — a caller using
`RadiodControl` directly has no way to set them. `SPECTRUM_BASE` and
`SPECTRUM_STEP` have **zero reachability anywhere** in ka9q-python,
including `SpectrumStream`.

**Evidence:** `control-surface.md` § Gaps, "Write-side" (all five bullet
items and the consolidating parenthetical).

**Remediation:** Add `window_type`, `avg`, `overlap`, `base`, `step`
parameters to `set_spectrum()`; have `SpectrumStream` call the new
`set_spectrum()` instead of hand-encoding its own buffer, collapsing two
write paths into one.

### F12 — `SETOPTS` status field is emitted by radiod but never decoded

**Claim:** `radiod` unconditionally encodes `chan->options` under the
`SETOPTS` tag on every status packet. `decode_status_packet` has no branch
for it and `ChannelStatus` has no `options` field — the bitmask radiod is
actually running with is silently dropped every packet. A client that
calls `set_options()` has no way to confirm the option took, or to
discover options set by another client, a preset, or a radiod default.

**Evidence:** `control-surface.md` § Gaps, "Read-side".

**Remediation:** Add an `options` field to `ChannelStatus` and a decode
branch for `SETOPTS` in `decode_status_packet`.

### F13 — `set_demod_type()`'s range check hardcodes `<= 4`, stale once `IDLE_DEMOD` ships

**Claim:** `control.py:2854` rejects any `demod_type` outside `0..4` via a
magic-number literal, not derived from `DemodType.N_DEMOD`. This is
correct against the pin but will incorrectly reject `IDLE_DEMOD=5` once
`types.py` is regenerated for AUDIT_HEAD (F2's remediation).

**Evidence:** `contract.md` § Step 4 "Related but distinct finding";
`control-surface.md` § "`DEMOD_TYPE` note".

**Remediation:** Derive the bound from `DemodType.N_DEMOD - 1` (or widen
to `<= 5`) in the same change that regenerates `types.py` for F2, so the
two can't drift apart again.

### F14 — `resolve_multicast_address` is public but not re-exported from `ka9q/__init__.py`

**Claim:** `ka9q.utils.resolve_multicast_address` is a legitimate,
non-underscore-prefixed public function (raise-on-unreachable address
probe) that was simply never pulled into `ka9q/__init__.py`'s re-export
list, unlike its conceptual sibling `generate_multicast_ip`. hf-timestd
already reaches into `ka9q.utils` directly for it — this is one of only
two true internals reaches found across all four+ named client repos (the
other, F15, has a different existing public equivalent).

**Evidence:** `clients.md` § Step 2, item 2 ("`resolve_multicast_address`").

**Remediation:** Either re-export `resolve_multicast_address` from
`ka9q/__init__.py`, or (the report's preferred option) add a typed
reachability/health-check method on `RadiodControl` (e.g. `is_reachable()`)
so callers don't need a raise-on-failure utility function with no typed
exception class.

---

## P3

### F15 — hf-timestd hand-encodes filter edges instead of calling `set_filter()`

**Claim:** hf-timestd's `_set_filter_edges()` imports `encode_int`,
`encode_double`, `encode_eol`, `CMD` from `ka9q.control` directly and
hand-builds a TLV packet for `LOW_EDGE`/`HIGH_EDGE` — but
`RadiodControl.set_filter(ssrc, low_edge=, high_edge=, kaiser_beta=)`
already sends the identical fields (in a different, TLV-decoder-irrelevant
order) and additionally supports `KAISER_BETA`, which the hand-rolled path
never sends at all. This looks like a reimplementation that predates
`set_filter()`'s addition and was never migrated, not evidence of a
missing capability.

**Evidence:** `clients.md` § Step 2, item 1.

**Remediation:** hf-timestd should call `self._control.set_filter(ssrc,
low_edge=low, high_edge=high)` instead of hand-encoding; this is a client
migration, not a ka9q-python change (the public equivalent already
exists).

### F16 — `rtp_to_wallclock` deprecated alias emits no `DeprecationWarning`

**Claim:** `rtp_to_wallclock = rtp_to_utc` was renamed 2026-06-27
(`ka9q/rtp_recorder.py:247`) with no `DeprecationWarning` ever raised.
hf-timestd and wspr-recorder both still use the old name exclusively;
psk-recorder/meteor-scatter already use the new name.

**Evidence:** `clients.md` § "Cross-client observations", first bullet.

**Remediation:** Add `warnings.warn(..., DeprecationWarning)` to the alias
so the two lagging clients get a visible signal to migrate, without
breaking them.

### F17 — `StreamQuality` field-access style differs by client (loud vs. silent on rename)

**Claim:** hf-timestd and wspr-recorder read `StreamQuality` fields via
direct attribute access (a rename raises `AttributeError`); psk-recorder
and meteor-scatter use `getattr(quality, "field", None)` (a rename
silently degrades — batches just stop anchoring, no error logged). There
is no documented field-stability contract for `StreamQuality` that would
tell a maintainer which behavior to expect or preserve.

**Evidence:** `clients.md` § "Cross-client observations", second bullet.

**Remediation:** Document `StreamQuality`'s fields as a stable public
contract (which fields are guaranteed present vs. optional) so future
changes can be made with the right blast-radius awareness; no code change
required in ka9q-python itself.

### F18 — `SlotClock.offset_of_rtp()` called unguarded in psk-recorder and meteor-scatter

**Claim:** Both clients call `SlotClock.offset_of_rtp()` directly from
their receive-thread callback without the `try/except
SlotClockDesyncError` that `SlotClock.advance()` applies internally to the
same exception. A genuine desync propagates uncaught up through the
`MultiStream` callback. This is a client-side robustness gap, not a
ka9q-python contract violation — found while reading call sites for the
symbol matrix, not the bypass sweep — but it's exactly the kind of latent
bug this audit exists to surface.

**Evidence:** `clients.md` § psk-recorder row (`slot.py:31`) and §
meteor-scatter row.

**Remediation:** Not a ka9q-python fix. Flag to psk-recorder/meteor-scatter
maintainers; consider whether `ka9q.slot_clock`'s docstring for
`offset_of_rtp()` should explicitly warn that, unlike `advance()`, it does
not catch `SlotClockDesyncError` internally.

### F19 — meteor-scatter never wires up `StatusListener`; slide-follow is inert

**Claim:** Unlike its sibling psk-recorder, meteor-scatter's
`receiver_manager.py` never imports or starts
`ka9q.status_listener.StatusListener`. Its slide-follow hook recomputes
nearly the same offset every tick instead of tracking radiod's live
RTP↔UTC drift, because nothing mutates `channel_info.gps_time`/
`.rtp_timesnap` in place. `ka9q-python` already supports the intended
behavior; meteor-scatter simply never wires it up.

**Evidence:** `clients.md` § meteor-scatter, "Genuine behavioral gap
found".

**Remediation:** Not a ka9q-python fix. Flag to meteor-scatter
maintainers to wire up `StatusListener` the way psk-recorder does.

### F20 — hf-timestd hand-rolls an RTP-only multicast join instead of using `RadiodStream`

**Claim:** `hf_timestd/audio_streamer.py`'s `AudioStreamer.start()` opens
its own `SOCK_DGRAM` socket, does `IP_ADD_MEMBERSHIP`, and binds directly
to re-stream already-provisioned RTP audio over HTTP. It never provisions
a channel and never sends a control-plane packet, so per the brief's
explicit carve-out ("RTP-only consumption without control is compliant")
this is compliant, not a bypass — but it duplicates functionality
`ka9q.RadiodStream`/`MultiStream` already provides.

**Evidence:** `clients.md` § "One RTP-only hand-rolled consumer".

**Remediation:** Optional consolidation candidate for hf-timestd; no
ka9q-python capability gap exists (the library already does this).

### F21 — Two dead `ka9q` imports in hf-timestd

**Claim:** `core_recorder_v2.py:1444` imports `StatusType` but never
references it; `scripts/inspect_channels_full.py:2` imports `Encoding` but
never uses it (only prints the raw `.encoding` int). Zero risk, noted for
completeness.

**Evidence:** `clients.md` § hf-timestd symbol matrix, "Two dead imports
noted in passing".

**Remediation:** Not a ka9q-python issue; a one-line cleanup for
hf-timestd whenever convenient.

### F22 — Resequencer cross-run delivery consistency (Q5) remains unresolved needs-empirical

**Claim:** `PacketResequencer` is a deterministic function of the exact
sequence of `process_packet()` calls it receives — verified by code
reading (no wall-clock or timing input feeds the resequencing decision
logic). But two live runs consuming the same multicast stream on
independent sockets don't share a receive queue, so independent network
jitter/loss can drive different call sequences into two otherwise-identical
resequencer instances, which — through the buffer-occupancy skip-ahead
threshold — can produce different delivered payloads. This was flagged
`needs-empirical` in Task 6 and explicitly **not attempted** in either
Task 7 round (both rounds' probes covered Q1/Q3/Q4 only; Q5 would need a
dedicated dual-receiver live test).

**Evidence:** `idempotency.md` § Q5; § "Empirical results (Task 7) — Round
2", "How Round 2 reconciles with Task 6" (Q5 bullet, explicitly deferred).

**Remediation:** Design and run a dedicated dual-receiver probe (two
`PacketResequencer` consumers of the same live stream) as a follow-up
task, if bounding this divergence matters for any current consumer (e.g.
hf-timestd's dual-anchor T5/T6 cross-checks, which the report calls out as
the scenario where it would matter most).

---

## Consolidation notes (relative to the five source reports)

- **F11** consolidates five separate write-side TLV gaps from
  `control-surface.md` (`WINDOW_TYPE`, `SPECTRUM_AVG`, `SPECTRUM_OVERLAP`,
  `SPECTRUM_BASE`, `SPECTRUM_STEP`) into one finding, since they share a
  single root cause and a single proposed remediation
  (`set_spectrum()` parameter list).
- **F6** merges the `set_lock()` write-with-no-effect finding that appears
  independently in both `control-surface.md` (§ Gaps, "Bonus finding") and
  `idempotency.md` (§ "Related failure mode") — same underlying defect,
  found twice by two different methodologies, reported once here.
- **F13** merges the `control.py:2854` stale-range finding that appears in
  both `contract.md` (Step 4) and `control-surface.md` (`DEMOD_TYPE` note)
  — same finding, two independent sightings.
- Q2 (`allocate_ssrc` determinism) and Q3 (`ManagedStream`/`ChannelMonitor`
  recovery equivalence) from `idempotency.md` are **not** listed as
  findings — both were verified-by-code and then verified live in Task 7
  with no defect found. They are closed, not open items.
- The `731ce5e` keepalive-encoding fix itself (already committed prior to
  this audit) is likewise not listed — Task 7's Probe 2b Phase B confirmed
  it works as designed; only its *sibling* gap in `tune()` (F4) and in
  `MultiStream` restore (F1) are open findings.
