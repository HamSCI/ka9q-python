# Idempotency Code Audit — pin..AUDIT_HEAD

**Audit date:** 2026-08-12
**Scope:** code-only (no live radiod contacted). ka9q-python at repo `HEAD` on
`main` (working tree at the start of this task). Cross-referenced against the
`ka9q-radio` C source at the audit's frozen pin, `14d780af624e821941708bd0d64fd895a0c80a2a`
(the only commit checked out in the local `~/../ka9q-radio` clone available to
this task — `AUDIT_HEAD` (`cedec3497...`) was not fetchable from this
sandbox, so the radiod-side claims below are pin-only; re-verifying them
against `AUDIT_HEAD`'s `src/radio_status.c`/`src/radio.c` is one line item on
the needs-empirical list).

This file answers the five spec questions from Task 6's brief, then reports a
sibling-bug scan for the `731ce5e` bug class (a maintenance path silently
dropping a creation-time setting).

---

## Step 1: Five spec questions

### Q1 — `create_channel` convergence: is re-creating an existing SSRC state-identical to first creation?

**Verdict: DEFECT** (partial, not atomic, for an existing SSRC)

Evidence, ka9q-python side — `ka9q/control.py:1275`-`1488` (`create_channel`):
the method assembles one TLV packet with `PRESET` (always),
`DEMOD_TYPE` (always, derived from preset), `RADIO_FREQUENCY` (always),
`OUTPUT_SAMPRATE` (**only if `sample_rate` truthy**, `control.py:1409-1410`:
`"if sample_rate: encode_int(cmdbuffer, StatusType.OUTPUT_SAMPRATE, sample_rate)"`),
`LOW_EDGE`/`HIGH_EDGE`/`KAISER_BETA` (**only if not `None`**, `control.py:1417-1425`),
`AGC_ENABLE` (always), `GAIN` (always), `OUTPUT_DATA_DEST_SOCKET` (**only if
`destination is not None`**, `control.py:1442-1457`), `OUTPUT_SSRC` +
`COMMAND_TAG` (always), `LIFETIME` (**only if not `None`**,
`control.py:1462-1464`), then sends it (`control.py:1468`: `self.send_command(cmdbuffer)`).
A **second**, separate UDP packet carries `OUTPUT_ENCODING`, and only if
`encoding > 0` (`control.py:1436-1439` comment: `"Radiod requires
OUTPUT_ENCODING to be sent in a separate command after creation"`;
`control.py:1471`: `"if encoding > 0:"`).

Evidence, radiod side (pin `14d780af`) — `src/radio_status.c:69-105`
(`radio_status()`'s command dispatch): a CMD packet for an SSRC that
`lookup_chan()` finds is **not** routed through `create_chan()` at all — it
is queued for the channel's own demod thread to run through
`decode_radio_commands()` (`radio_status.c:74`: `"// Channel already exists;
queue the command for it to execute"`). Only the **not-found** branch calls
`create_chan()` (`radio_status.c:93`), which does `*chan = Template` — a full
struct reset — before applying the packet (`src/radio.c:1013`: `"*chan =
Template; // Template.inuse is already set"`). `create_chan()` itself refuses
outright if the SSRC is already in use (`src/radio.c:993-997`: `"if(Channel_list[i].inuse
&& Channel_list[i].output.rtp.ssrc == ssrc){ ... return NULL; // sorry,
already taken"`).

Net effect: calling `create_channel()` a second time on an SSRC that
already exists on radiod does **not** get a template reset. It becomes a
**delta update** — `decode_radio_commands()` (`src/radio_status.c:133-172`)
only mutates the fields whose TLV is present in *this* packet; a TLV a
client chose not to include this time round (because the corresponding
Python arg was `None`/falsy: `sample_rate`, `low_edge`, `high_edge`,
`kaiser_beta`, `destination`, `lifetime`, and `encoding<=0`) leaves the
**existing radiod-side value untouched**, not reset to a template/preset
default. `PRESET` is one partial mitigant — its handler calls `loadpreset()`
(`src/modes.c:294`), which resets `chan->filter.min_IF`/`max_IF`, `samprate`,
etc. **only for keys the preset's config-file stanza actually defines**
(e.g. `src/modes.c:325-331`: `"if(low != NULL) chan->filter.min_IF =
parse_frequency(low,false);"` — a `None`/absent ini key leaves the prior
value). Whether a given preset's ini stanza defines `low`/`high` is a
runtime config fact, not visible from ka9q-python's source, so the practical
blast radius of the gap is `needs-empirical` even though the code-level
mechanism (delta-not-reset) is fully confirmed by source.

Concretely: `create_channel(freq, preset="usb")` on an SSRC that already
exists with a custom `sample_rate=48000` set by a prior call leaves that
48000 Hz sample rate in place (the new call omits `OUTPUT_SAMPRATE` since
`sample_rate=None`) — the docstring's claim that `sample_rate: ... uses
radiod's default if not set` (`control.py:1304`) is true only for a *fresh*
SSRC, not a re-create.

### Q2 — `allocate_ssrc` determinism: same inputs ⇒ same SSRC across processes/restarts? Collision behavior?

**Verdict: VERIFIED-BY-CODE** (deterministic); collision handling is a
separate, correctly-delegated concern, not a gap in `allocate_ssrc` itself.

Evidence — `ka9q/control.py:68-130` (`allocate_ssrc`): the function is a
pure function of its arguments. It builds a stable pipe-delimited string
(`control.py:113-122`: `f"{round(frequency_hz)}|{preset.lower()}|..."`),
hashes it with SHA-256 (`control.py:125`: `h = hashlib.sha256(key_str.encode()).digest()`),
and masks the first 4 bytes to 31 bits (`control.py:129-130`:
`"ssrc_full = int.from_bytes(h[:4], byteorder='big'); return ssrc_full &
0x7FFFFFFF"`). No randomness, clock, PID, or other process-local state is
consulted — same 8 inputs (`frequency_hz`, `preset`, `sample_rate`, `agc`,
`gain`, `destination`, `encoding`, `radiod_host`) always hash to the same
31-bit value, in any process, any restart, any machine (SHA-256 is
platform-independent). `ka9q/addressing.py:60` (`generate_multicast_ip`)
uses the same SHA-256-of-stable-string pattern for multicast address
derivation and is likewise pure.

Collision behavior: `allocate_ssrc` has **no** collision detection or
retry — by design (birthday-bound ~2^15.5 for a 50% chance across a 2^31
space; the intended "collision" *is* two callers converging on the same
params, which is the point — "enabling stream sharing", `control.py:82-83`).
An accidental collision between two callers with *different* semantic
intent but a colliding hash is handled downstream by radiod, not by
`allocate_ssrc`: `create_chan()` refuses a second create for a live SSRC
(`src/radio.c:993-997`, `"return NULL; // sorry, already taken"`), radiod
logs and drops the request without a response
(`src/radio_status.c:93-95`: `"if((chan = create_chan(ssrc)) == NULL){
fprintf(stderr,\"Dynamic create of ssrc %'u failed...\")"`), and
`ensure_channel()`'s subsequent `poll_channel(ssrc, expected_freq=...)`
(`control.py:1889-1897`) will see the *other* channel's frequency, fail the
tolerance check, and the caller gets a `TimeoutError` — a loud failure, not
a silent wrong-channel takeover. This is sound but is itself untested
against a live collision (needs-empirical, low priority given the odds).

### Q3 — Recovery equivalence: `managed_stream.py` / `monitor.py` vs `ManagedStream.__init__`'s parameter list

**Verdict: VERIFIED-BY-CODE for `ManagedStream` itself** (start and restore
send an identical parameter set); **DEFECT found in the sibling `MultiStream`
restore path** — see Step 2, table row 6.

Evidence — `ManagedStream.__init__` (`ka9q/managed_stream.py:117-136`) stores
7 channel-identity params: `frequency_hz`, `preset`, `sample_rate`,
`agc_enable`, `gain`, `destination`, `encoding` (plus non-channel config:
callbacks, timeouts, stream-plumbing knobs). `start()`
(`managed_stream.py:231-239`) calls
`self._control.ensure_channel(frequency_hz=self._frequency_hz,
preset=self._preset, sample_rate=self._sample_rate,
agc_enable=self._agc_enable, gain=self._gain,
destination=self._destination, encoding=self._encoding)` — exactly those 7.
`_attempt_restore()` (`managed_stream.py:414-423`) calls `ensure_channel()`
with the **same 7 fields**, byte-for-byte identical kwargs (only adding a
`timeout=` runtime knob absent from `start()`). So `ManagedStream`'s own
recovery path is symmetric with its own initial-start path — no field is
dropped on recreation.

Caveat (design gap, not an asymmetry): `ManagedStream.__init__` never
exposes `ssrc`, `lifetime`, `low_edge`, `high_edge`, or `kaiser_beta` at
all — `create_channel`'s/`ensure_channel`'s full parameter surface is a
strict superset of what `ManagedStream` can request. This is a coverage
limit on `ManagedStream`, consistently applied to both `start()` and
restore, so it does not fit the "silently drops on recovery only" bug
class — flagged as `needs-empirical` (does any current client need
`ManagedStream`-level filter/lifetime control, or do they use `MultiStream`
for that?).

`ka9q/monitor.py` (`ChannelMonitor`): `monitor_channel(**kwargs)`
(`monitor.py:75-103`) stores the **exact `kwargs` dict** passed by the
caller (minus `timeout`, `monitor.py:96-98`) into
`self._monitored_channels[ssrc]`, and `_check_and_recover()`
(`monitor.py:142-150`) replays it verbatim: `self.control.ensure_channel(**params)`
(`monitor.py:147`). This is a complete, generic pass-through — whatever the
caller originally supplied to `ensure_channel` (including `low_edge`,
`kaiser_beta`, `lifetime`, etc. if the caller passed them) is preserved
identically on recovery. **Verified-by-code, no gap.**

### Q4 — Keepalive preservation: does any keepalive/refresh path send fewer parameters than creation did?

**Verdict: VERIFIED-BY-CODE for `set_channel_lifetime` as designed**
(intentionally minimal; the one prior gap — encoding — was closed by
`731ce5e` and the fix does not reproduce against the pin); **DEFECT found**
in a related path not covered by that fix — see Step 2, table row 3
(`tune()` doesn't update the encoding-memory dict `731ce5e` introduced).

Evidence — `set_channel_lifetime()` (`ka9q/control.py:1960-2023`) sends
exactly: `OUTPUT_SSRC`, `COMMAND_TAG`, `LIFETIME` (`control.py:2018-2020`),
and, since `731ce5e`, `OUTPUT_ENCODING` **iff** a nonzero value is on file
in `self._requested_encoding.get(ssrc)` or passed explicitly
(`control.py:2014, 2021-2022`: `"enc = self._requested_encoding.get(ssrc) if
encoding is None else encoding" / "if enc: encode_int(cmdbuffer,
StatusType.OUTPUT_ENCODING, enc)"`). Every other creation-time field
(`PRESET`, `DEMOD_TYPE`, `RADIO_FREQUENCY`, `OUTPUT_SAMPRATE`, `LOW_EDGE`,
`HIGH_EDGE`, `KAISER_BETA`, `AGC_ENABLE`, `GAIN`,
`OUTPUT_DATA_DEST_SOCKET`) is **omitted**, by design — this is meant to be
a minimal keepalive, not a resend.

Radiod-side check for whether that omission is safe on the pin: the
`LIFETIME` case in `decode_radio_commands()` (`src/radio_status.c:675-680`)
is a pure two-line assignment — `"int x = decode_int(cp,optlen);
chan->lifestart = chan->lifetime = x;"` — with **no side effects on any
other channel field**, and no unconditional post-loop reset touches
encoding either (`radio_status.c:681-698`: only `restart_needed`/
`new_filter_needed` branches run, both false for a LIFETIME-only packet).
This confirms, at the source level, `731ce5e`'s own empirical finding
("F32 survived 10 keepalives... the revert does NOT reproduce" on
`14d780af`) is not a fluke — on *this* pin a LIFETIME-only poll genuinely
cannot reset any other channel field. The re-assertion the commit added is
therefore currently a no-op-but-safe defensive measure for *this* radiod
build; its stated purpose is protecting against *other* builds
(`"some radiod builds reset a channel's output encoding..."`), which is
inherently `needs-empirical` (cannot be checked from this repo's source).

### Q5 — Resequencer continuity: under what loss/reorder conditions does delivered payload differ between two identical runs?

**Verdict: NEEDS-EMPIRICAL** (the algorithm itself is a deterministic
function of the packet-arrival trace; two live runs are not guaranteed to
see the same trace, so byte-identical delivery across two *live* runs is
not a code-level guarantee).

Evidence — `ka9q/resequencer.py`, `PacketResequencer`: every state
transition is driven purely by (a) which packets are currently in
`self.buffer` (a bounded `deque`, `resequencer.py:112`) and (b) counting/
comparing `sequence`/`timestamp` fields already on those packets — no
wall-clock or timing input feeds the resequencing *decision* logic. The one
skip-ahead heuristic, `"if len(self.buffer) >= self.buffer_size // 2:"`
(`resequencer.py:207`, triggering `_handle_lost_packet()`), is a function
of buffer **occupancy**, not elapsed time. Gap-size math uses Phil Karn's
signed-32-bit-wrap technique (`resequencer.py:257-262`), and gap fills are
zero-padding sized from the RTP timestamp delta (`resequencer.py:222-224`,
`331-333`), both pure functions of the two timestamps involved.
`timestamp_utc=datetime.now(timezone.utc).isoformat()` (`resequencer.py:289`,
`326`) is metadata written into `GapEvent`, not sample data, so it doesn't
affect the delivered sample payload.

So: **given an identical sequence of `process_packet()` calls (same packets,
same order, same losses)**, two `PacketResequencer` instances produce
byte-identical output — the logic contains no hidden nondeterminism. But
two *live* runs consuming the same multicast RTP stream over UDP on
different sockets/hosts do not share a receive queue; independent network
jitter and independent packet loss mean the actual call sequence into
`process_packet()` can differ between runs, which (through the buffer-size//2
skip-ahead threshold and the gap-detection branch) can produce different
delivered payloads for the two runs even though the algorithm is
deterministic. Verifying whether this matters in practice (e.g. bounded
divergence, or matches client tolerance) requires live traffic — hence
`needs-empirical`, not `defect`: nothing in the code is wrong, the
proposition ("delivered payload is identical between two runs") depends on
network conditions the code cannot control.

Post-recovery continuity note (relevant context, not a new question): each
`RadiodStream.start()` constructs a **fresh** `PacketResequencer`
(`ka9q/stream.py:283`) and/or calls `.reset()` (`stream.py:334`), so a
`ManagedStream`/`MultiStream` restore does not carry stale
sequence/timestamp expectations across the gap — the resequencer
re-`_initialize()`s from the first post-recovery packet
(`resequencer.py:166-172`) with no gap-fill for the outage itself (that
outage is tracked separately, at the `ManagedStream`/`MultiStream` level,
via `StreamQuality`/`GapEvent`, not by `PacketResequencer`).

---

## Step 2: Sibling-bug scan for the `731ce5e` class

`731ce5e`'s bug shape: a maintenance path (`set_channel_lifetime`, the
keepalive every long-lived client calls) silently dropped a creation-time
setting (`OUTPUT_ENCODING`) that `create_channel` had established. The scan
below enumerates every code path in ka9q-python that re-sends state for an
*already-existing* channel — keepalive, reuse, restore, retune — and diffs
its parameter set against `create_channel`'s full set:

`{PRESET, DEMOD_TYPE, RADIO_FREQUENCY, OUTPUT_SAMPRATE, LOW_EDGE, HIGH_EDGE,
KAISER_BETA, AGC_ENABLE, GAIN, OUTPUT_DATA_DEST_SOCKET, OUTPUT_SSRC,
COMMAND_TAG, LIFETIME(opt), OUTPUT_ENCODING(opt, 2nd packet)}`

| # | Path | File:line | Fields sent | Fields silently omitted vs. `create_channel` | Verdict |
|---|------|-----------|-------------|-----------------------------------------------|---------|
| 1 | `create_channel` (baseline) | `control.py:1275-1488` | all, conditionally per arg (see Q1) | n/a — baseline | n/a |
| 2 | `set_channel_lifetime` (keepalive) | `control.py:1960-2023` | `OUTPUT_SSRC`, `COMMAND_TAG`, `LIFETIME`, `OUTPUT_ENCODING` (if tracked/passed) | `PRESET`, `DEMOD_TYPE`, `RADIO_FREQUENCY`, `OUTPUT_SAMPRATE`, `LOW_EDGE`, `HIGH_EDGE`, `KAISER_BETA`, `AGC_ENABLE`, `GAIN`, `OUTPUT_DATA_DEST_SOCKET` | Intentional (minimal keepalive); confirmed harmless on pin (Q4). Encoding gap already fixed by `731ce5e`. **No new defect**, but see row 3. |
| 3 | `tune()` | `control.py:2098-2231` | whichever of `PRESET`, `OUTPUT_SAMPRATE`, `LOW_EDGE`, `HIGH_EDGE`, `RADIO_FREQUENCY`, `GAIN`/`AGC_ENABLE`, `OUTPUT_ENCODING`, `RF_GAIN`, `RF_ATTEN`, `OUTPUT_DATA_DEST_SOCKET`, `LIFETIME` the caller passed non-`None` | by design, a targeted delta-setter (mirrors `tune.c`) — not meant to be a full resend, so field omission itself is not a defect | **DEFECT**: `tune(ssrc, encoding=X)` sends `OUTPUT_ENCODING` (`control.py:2195-2196`) but — unlike `create_channel` (`control.py:1484`) and `set_output_encoding` (`control.py:2896`) — **never writes `self._requested_encoding[ssrc] = X`**. `grep -n "_requested_encoding" control.py` shows exactly 2 write sites (1484, 2896) and this is not one of them. Any channel whose encoding was last changed via `tune()` has a stale (or absent) entry in `_requested_encoding`, so (a) the keepalive's `731ce5e` re-assertion (row 2) re-sends the *wrong* (or no) encoding on the next `set_channel_lifetime()` call, and (b) `verify_channel()`'s default `expected_encoding` (`control.py:1533`, defaults from the same dict) checks against the wrong value too. This silently defeats `731ce5e`'s fix for any client whose retune path is `tune()` rather than `create_channel`/`set_output_encoding`. |
| 4 | `ensure_channel` — reuse branch | `control.py:1794-1849` | `LIFETIME` (if requested, `1832-1833`), `LOW_EDGE`/`HIGH_EDGE`/`KAISER_BETA` via `set_filter` (if any requested, `1840-1848`) | `AGC_ENABLE`, `GAIN` — not re-sent on reuse, and not checked as a mismatch trigger | **NOT A DEFECT — docstring imprecision only.** Corrected after review: the match test at `1801-1822` indeed only compares `frequency`, `sample_rate`, `destination`, `encoding`, omitting `gain`/`agc_enable`. But the "stale gain/AGC on an already-matching channel" scenario this implies is **unreachable through the normal `ensure_channel` API**: the lookup SSRC itself is `allocate_ssrc(..., agc=bool(agc_enable), gain=gain, ...)` (`control.py:1782-1791`) — `gain` and `agc_enable` are inputs to the SSRC hash. Two `ensure_channel()` calls that differ in `gain`/`agc_enable` therefore compute **different SSRCs** and poll **different channels** (`control.py:1797-1800`); there is no way for a caller to land on an existing channel whose `gain`/`agc_enable` differ from what it just asked for, short of a ~2^-31 SHA-256 collision (already covered, low-priority, in Q2) or an explicit `ssrc=` override passed directly to `create_channel` (bypassing `ensure_channel` entirely, a different call path not audited here). The only real issue is the docstring's wording (`control.py:1721-1722`: `"...last-writer-wins, same model as gain/AGC"`) — it analogizes to gain/AGC as if they were reconfigured like filter edges on a reuse, which is misleading/inaccurate phrasing since gain/AGC can never actually observe a "reuse with different value" state to reconfigure *from*. Recommend rewording that docstring sentence to stop implying a reuse-time reconciliation happens for gain/AGC; no functional fix needed. |
| 5 | `ensure_channel` — create/reconfigure branch | `control.py:1865-1880` | delegates to `create_channel` with the full set incl. `ssrc=`, `lifetime=`, `low_edge=`, `high_edge=`, `kaiser_beta=` | none vs. `create_channel` (full pass-through) | Verified-by-code, matches baseline. Inherits Q1's existing-SSRC delta-update caveat from radiod. |
| 6 | `MultiStream._attempt_restore` | `multi_stream.py:687-724` | `ensure_channel(frequency_hz=slot.frequency_hz, preset=slot.preset, sample_rate=slot.sample_rate, encoding=slot.encoding, lifetime=slot.lifetime)` | `agc_enable`, `gain`, `low_edge`, `high_edge`, `kaiser_beta` — all silently dropped | **DEFECT (highest-confidence finding of this audit)**: `MultiStream.add_channel()` (`multi_stream.py:150-211`) accepts and forwards `agc_enable`, `gain`, `low_edge`, `high_edge`, `kaiser_beta` to the *initial* `ensure_channel()` call (`multi_stream.py:199-211`), but `_ChannelSlot` (`multi_stream.py:66-99`) **does not have fields to store any of them** — only `frequency_hz`, `preset`, `sample_rate`, `encoding`, `lifetime` are persisted per-slot. `_attempt_restore()` can therefore only ever pass those 5 back into `ensure_channel()`, which supplies its own defaults for the rest (`agc_enable=0, gain=0.0, low_edge=None, high_edge=None, kaiser_beta=None`, `control.py:1660-1669`). The `lifetime` field's own docstring shows the team already solved this exact problem for one field (`multi_stream.py:176-183`: `"The value is stored per-slot so the drop/restore path re-applies it: a channel that radiod self-destructs and we then restore won't silently lose its lifetime"`) — and `encoding` is likewise stored and reapplied — but `agc_enable`/`gain`/`low_edge`/`high_edge`/`kaiser_beta` were not given the same treatment. On any restore that reaches the reconfigure branch of `ensure_channel` (Q1: happens whenever freq/rate/dest/encoding don't already match, which is the common post-radiod-restart case since the channel doesn't exist at all), `create_channel` is called with `agc_enable=0, gain=0.0` and no filter edges — **unconditionally resetting AGC/gain to defaults and any custom passband to the preset default**, exactly the "keepalive/restore silently drops a creation-time setting" bug class `731ce5e` fixed for encoding, reproduced for five more fields in the sibling restore path. **Severity is worse than field-omission, though: it is identity-changing, not just value-dropping.** `agc_enable` and `gain` are inputs to `allocate_ssrc`'s hash (`control.py:1782-1791`, same fact that clears row 4 above), and `_attempt_restore()` never had the original values to pass — so when the original channel was created with a non-default `agc_enable`/`gain`, the restore's `ensure_channel()` call computes a **different SSRC** than the one being restored. `multi_stream.py` itself already has to handle this: `new_ssrc = channel_info.ssrc; if new_ssrc != ssrc: del self._slots[ssrc]; self._slots[new_ssrc] = slot` (`multi_stream.py:696-699`) exists precisely because a restore can land on a different SSRC — confirming the divergence is a reachable, anticipated runtime path, not a hypothetical. The restored stream is therefore not just misconfigured (wrong gain/AGC/passband); it can be a **structurally different channel** (new SSRC, new RTP stream identity) from the one the caller believes it is still receiving, with any downstream code keying off the original SSRC (logs, external correlation, another client's targeted `set_*` calls) silently pointed at a channel that no longer exists. |
| 7 | `ChannelMonitor._check_and_recover` | `monitor.py:122-150` | `ensure_channel(**params)` where `params` is the original `monitor_channel(**kwargs)` dict, minus `timeout` | none — full generic pass-through | Verified-by-code, no gap (see Q3). |
| 8 | `ManagedStream.start` / `_attempt_restore` | `managed_stream.py:231-239`, `414-423` | identical 7-field set both times | none between the two call sites (see Q3) | Verified-by-code for internal symmetry; coverage-limited relative to `create_channel`'s full surface (design gap, not this bug class). |

### Related failure mode: `set_lock()` — a command radiod never processes

Not a `731ce5e`-class bug (nothing is dropped from a *resend* — the field is
never honored on *any* send, first or subsequent), but material to an
idempotency audit for the same reason: it is another way a client's model
of channel state silently diverges from radiod's actual state, with no
error surfaced.

`ka9q/control.py:3046-3056` (`set_lock`) builds and sends a TLV packet
carrying `StatusType.LOCK` (`control.py:3051`: `"encode_int(cmdbuffer,
StatusType.LOCK, 1 if lock else 0)"`), `OUTPUT_SSRC`, and `COMMAND_TAG` —
the encode side is correct and `status.h` documents `LOCK` as a settable
field ("Tuner is locked, will ignore retune commands (boolean)"), per
Task 4's control-surface audit. But `decode_radio_commands()`
(`src/radio_status.c:133-681`) has **no `case LOCK:`** in its switch
statement at either the pin (`14d780af`, confirmed here — `grep -n "case
LOCK" src/radio_status.c` returns nothing) or `AUDIT_HEAD` (per Task 4's
`control-surface.md`, which found the same gap there). The `LOCK` TLV
falls through to `default: break;` and is silently discarded — radiod
never sets `chan->lock`, never acts on it, and never signals failure back
to the caller. `set_lock()` returns normally either way; nothing in
ka9q-python or the wire protocol distinguishes "lock applied" from "lock
silently ignored".

This does not intersect the keepalive/recreate paths audited in Step 2
(no other method reads or re-sends `LOCK`, so there is no "partial resend"
of it to compare), but it is the same class of harm this whole audit
cares about: a caller that calls `set_lock(ssrc, True)` believes the
tuner is now protected from retune, and it is not — every subsequent
`tune()`/`create_channel()` call against that SSRC from *any* client
still takes effect. Task 4's `control-surface.md` already recommends
ka9q-python document or warn that `set_lock()` currently has no effect
against real radiod; this audit concurs and notes it belongs in the same
remediation pass as the Step 2 findings above, since both are instances
of "the client's belief about channel state and radiod's actual state can
silently diverge."

---

## `encode_int` sign-extension cross-reference (Task 2 sibling-bug instruction)

**Result: no defect — the C bug class does not reproduce in Python.**

Upstream bug (Task 2, commits `f825316f`/`fc7afa01`/`ce4fcdd9`): C's
`encode_int()` took a plain (signed 32-bit) `int` and implicitly
sign-extended it to 64 bits before calling `encode_int64()`, so an SSRC
with bit 31 set (e.g. `>= 0x80000000`, which as a signed 32-bit `int` is
negative) got encoded as an 8-byte value instead of 4 — a wire-format
mismatch for high-valued SSRCs.

`ka9q/control.py:303-318` (`encode_int`) is a thin alias for
`encode_int64` (`control.py:260-300`), which:
1. **Rejects negative input outright** (`control.py:275-276`: `"if x < 0:
   raise ValidationError(f\"Cannot encode negative integer: {x}\")"`) —
   there is no implicit-widening step for it to go wrong in, because Python
   ints are arbitrary-precision and the function demands the caller already
   pass an unsigned value.
2. Encodes the **minimum number of big-endian bytes** needed for the
   magnitude, by stripping leading zero bytes from an 8-byte
   representation (`control.py:288-298`).

Verified numerically for the brief's example SSRC, `3999900001` =
`0xEE69A161` (bit 31 set):
```
x.to_bytes(8, 'big').hex()  -> 00000000ee69a161
strip leading zero bytes    -> ee69a161  (4 bytes, length byte = 4)
```
This is exactly the 4-byte encoding the upstream fix (`encode_int32`) was
restoring — Python's `encode_int` produces the *correct* width natively,
with no sign-extension step to introduce the bug. Confirmed by grepping
every `encode_int(` call site in `control.py` for `OUTPUT_SSRC` and
`COMMAND_TAG` (the two fields upstream's bug hit): all pass Python
`int`s that are validated non-negative before or at the call
(`_validate_ssrc`, `control.py:134-139`, enforces `0 <= ssrc <=
0xFFFFFFFF`; `COMMAND_TAG` is always `secrets.randbits(31)`, inherently
non-negative and `< 2**31`). A broader sweep of all non-SSRC/tag
`encode_int(` call sites (`control.py`, ~25 sites: `AGC_ENABLE`,
`DEMOD_TYPE`, `OUTPUT_SAMPRATE`, `LIFETIME`, `OUTPUT_ENCODING`,
`PLL_ENABLE`, `OUTPUT_CHANNELS`, opus params, etc.) found no site passing
a value that could be negative or exceed 32 bits unexpectedly.

---

## Needs-empirical list for Task 7

1. **Q1 / row 5** — Whether a given preset's ini stanza (on the fleet's
   actual radiod config) defines `low`/`high`/`samprate` keys, which
   determines how much of `create_channel`'s existing-SSRC delta-update gap
   (Q1) is masked by `loadpreset()`'s per-key reset. Needs a live radiod
   config inspection, not just source.
2. **Q2** — Confirm no live SSRC collision has occurred in production (the
   ~2^31-space birthday bound is comfortable but unverified against actual
   fleet SSRC population; also confirm the `TimeoutError` failure mode is
   what fleet clients actually observe, not a hang or a wrong-channel
   takeover).
3. **Q4** — Whether any radiod build actually in the fleet (vs. the pinned
   `14d780af`) resets encoding — or possibly other fields — on a
   LIFETIME-only poll, as `731ce5e`'s commit message asserts for an
   unspecified "HamSCI-fork build". This can't be checked from this repo;
   requires either that fork's source or live testing against it.
4. **Q5** — Whether real network loss/reorder patterns on the fleet's
   multicast paths cause materially different delivered payloads between
   redundant/duplicate `PacketResequencer` consumers of the same stream (two
   receivers on one host, or hf-timestd-style dual-anchor consumers) — the
   algorithm is confirmed deterministic per-trace, but trace divergence
   under live conditions is unmeasured.
5. **Row 6 (MultiStream restore)** — Once Task 8 fixes the `_ChannelSlot`
   gap, confirm empirically (live radiod restart) that AGC/gain/filter
   edges actually get reset to defaults on restore today (this report's
   claim is fully code-derived from `_ChannelSlot`'s field list and
   `create_channel`'s unconditional `AGC_ENABLE`/`GAIN` sends; a live
   reproduction would strengthen it from "defect by code inspection" to
   "confirmed regression" for the fix's test plan).
6. **AUDIT_HEAD parity** — All radiod-side claims in this report
   (`create_chan`, `decode_radio_commands`, the `LIFETIME` case, `loadpreset`)
   were checked against the pinned commit `14d780af` only, because
   `AUDIT_HEAD` (`cedec3497...`) wasn't present in the local clone available
   to this task. Task 2/9's frozen `AUDIT_HEAD` diff should be checked for
   any changes to `src/radio_status.c` / `src/radio.c` / `src/modes.c`
   between the two that would alter these conclusions.

---

## Summary

| Q | Topic | Verdict |
|---|-------|---------|
| 1 | `create_channel` convergence | **DEFECT** — delta-update, not atomic reset, on an existing SSRC (radiod-side mechanism, not a Python bug per se, but the docstring overclaims) |
| 2 | `allocate_ssrc` determinism | **VERIFIED-BY-CODE** |
| 3 | Recovery equivalence (`ManagedStream`/`ChannelMonitor`) | **VERIFIED-BY-CODE** (both symmetric); sibling `MultiStream` restore is a separate **DEFECT**, filed under Step 2 |
| 4 | Keepalive preservation (`set_channel_lifetime`) | **VERIFIED-BY-CODE** as designed; sibling `tune()` encoding-memory gap is a separate **DEFECT**, filed under Step 2 |
| 5 | Resequencer continuity | **NEEDS-EMPIRICAL** |

Sibling-bug scan defect count (Step 2, distinct from the five verdicts
above where they overlap): **2** —
(a) `tune()` doesn't update `_requested_encoding` (row 3),
(b) `MultiStream._attempt_restore` drops `agc_enable`/`gain`/`low_edge`/
`high_edge`/`kaiser_beta` because `_ChannelSlot` never stores them, and —
since `gain`/`agc_enable` are inputs to `allocate_ssrc`'s hash — the
restore can silently target a *different SSRC* than the one being restored
(row 6, highest-confidence finding).
Row 4 (`ensure_channel`'s reuse branch and `gain`/`agc_enable`) was
reclassified from defect to docstring-imprecision-only after review — see
row 4's corrected entry above; the scenario it originally described is
unreachable through the normal `ensure_channel` API because `gain`/
`agc_enable` participate in SSRC derivation, so a mismatch on either
routes to a different channel rather than a stale reuse.

Related but distinct failure mode: see the **`set_lock()` — a command
radiod never processes** subsection below, which is a state-consistency
defect of a different shape (a write that is universally a no-op on the
wire, not a maintenance path that partially forgets what it once sent).

---

## Empirical results (Task 7) — Round 1 (blocked at write path, superseded)

**Status: BLOCKED at the write path in Round 1 — unblocked in Round 2,
see the "Round 2" section after this one for the real pass/fail
evidence.** This Round-1 section is kept in full because its root-cause
diagnosis (the `239.0.0.0/8 dev lo` routing entry) is exactly what
Round 2 fixed with `RadiodControl(..., interface=...)`, and because
Round 1's negative results are still the correct record of what a naive
first attempt produces on a sandbox configured this way.

Both permitted hosts (`bee1-status.local`, `bee2-status.local`) were
read-reachable in Round 1 — live STATUS multicast from real,
already-running radiod channels arrived normally and
`discover_channels()`/`poll_channel()` decoded it correctly — but every
outbound control command sent from this sandbox (`create_channel`,
`remove_channel`, and `ensure_channel`'s internal create, exercised via
`ManagedStream.start()`) silently failed to reach either radiod. This
was an environment-level networking fact of the sandbox this task ran
in, not a finding about ka9q-python or radiod itself, and it blocked all
four Round-1 probes below from producing usable pass/fail evidence for
Task 6's verdicts. Full diagnosis, all four probes' real (negative)
output, and the leftover-check are recorded below per the "do NOT fake
results" rule — none of the exit codes/CONVERGES/PRESERVED strings below
should be read as validating or refuting Task 6 without the caveat in
every subsection (Round 2 supersedes this for actual evidence).

### Root cause (confirmed, not just suspected)

1. `discover_channels("bee1-status.local", listen_duration=5)` returns 45
   real channels immediately (live production/monitoring traffic
   correctly received on `ens18`) — read path works.
2. `create_channel(...)` sends without a socket error (`send_command`
   returns normally, hex dump confirms correct TLV wire encoding,
   verified separately for `encode_int(SSRC=3999900001)` against the
   `0xEE69A161` bit-31-set case Task 6 flagged) — but the created SSRC
   never subsequently appears in `discover_channels()` (checked with
   listen windows up to 20s) or in a direct `poll_channel()` (checked
   with 5 sequential 3s-spaced polls). Reproduced with both an explicit
   in-range SSRC (`3999900050`, `3999900051`) and an auto-allocated
   31-bit SSRC (`1929612220`) — not specific to the audit's SSRC range or
   to the bit-31 case.
3. Reproduced identically on `bee2-status.local` (45 pre-existing
   channels; SSRC `3999900060` never appears after creation) — not
   host-specific.
4. `ip route get 239.205.73.40` (bee1's resolved status/control group)
   returns `multicast 239.205.73.40 dev lo src 192.168.1.176` — this
   sandbox's kernel routing table has a `239.0.0.0/8 dev lo` entry
   (`ip route show`) that routes **all locally-originated** multicast
   sends to any `239.x.x.x` destination to loopback, regardless of
   destination or the real interface (`ens18`) that inbound traffic from
   the actual radiod arrives on.
5. Confirmed by packet capture during a live `create_channel` +
   `remove_channel` call: `sudo tcpdump -i ens18 'udp and dst net
   239.0.0.0/8 and dst port 5006'` captured **zero** packets;
   `sudo tcpdump -i lo` on the same filter captured all 5 (create +
   encoding + remove-related) outbound packets, sourced from
   `127.0.0.1`. The command genuinely never leaves this host.
6. `ensure_channel` (exercised indirectly via `ManagedStream.start()` in
   `probe_recovery_equivalence.py`) fails the same way, from the inside:
   `TimeoutError: Channel SSRC 1913996770 not verified within 5.0s.` —
   consistent with the same root cause, not a separate bug in
   `ensure_channel`.

Net effect: this sandbox can **observe** the real fleet (useful for
read-only audit work) but cannot **control** it — every write this task's
probes depend on is discarded by the kernel before it reaches the
network. This was not caught by the brief's suggested `ping` reachability
check because `bee1-status.local`/`bee2-status.local` resolve to
multicast addresses, not unicast host IPs, so ICMP echo doesn't test the
relevant path either way; `discover_channels()` looked like a positive
reachability signal but only exercises the read path.

No attempt was made to work around the `dev lo` route (e.g. adding a
more-specific route via `ens18`, or `SO_BINDTODEVICE`) — that would mean
editing this sandbox's system routing table, which is out of scope for a
docs-only audit task and indistinguishable at this remove from an
intentional guardrail keeping an agent from being able to send live
control commands to real, shared radiod hardware. Fixing this (if it is
in fact just an environment misconfiguration rather than a deliberate
boundary) is a prerequisite for Task 6's `needs-empirical` items to ever
be closed empirically, and is called out as a blocker for whoever picks
this up next.

### Adaptations made (verified against source before running, per the brief)

- `ChannelInfo.encoding` (ka9q/discovery.py) is a plain `int` field, not
  an `Encoding` enum instance; `Encoding.F32LE` is itself a bare int
  constant (`4`), so the brief's `after.encoding == Encoding.F32LE`
  comparison needed no attribute-name change.
- `ManagedStream.__init__` has no `ssrc=` parameter — the SSRC is always
  derived deterministically inside `ensure_channel` ->
  `allocate_ssrc(...)`. There is no supported way to force it into the
  `3999900000-3999900999` range. `probe_recovery_equivalence.py` reads
  the real SSRC back from `stream.start()`'s returned `ChannelInfo.ssrc`
  and explicitly removes that actual SSRC in `finally` (since
  `ManagedStream.stop()` intentionally does not call `remove_channel` —
  channels are meant to be shareable). This is a documented deviation
  from the SSRC-range rule for this one probe; see the probe file's
  docstring for the full reasoning. In practice `ensure_channel` never
  got far enough to allocate/verify a channel (see root cause above), so
  no channel outside the reserved range was ever actually created on
  radiod.
- `ManagedStream.stop()` method name and `ManagedStreamStats` return type
  confirmed by reading `ka9q/managed_stream.py` directly; used as in the
  brief.
- Added a fourth probe, `probe_create_twice_variant.py`, per the
  brief's interpretation note ("create with different params second
  time"): first call sets `sample_rate=48000`, second call on the same
  SSRC omits `sample_rate` (falls back to `None`, so `OUTPUT_SAMPRATE` is
  never sent the second time). This is the concrete scenario Q1's DEFECT
  verdict describes in idempotency.md's Q1 section ("a first create sets
  a non-default sample_rate; a second create ... leaves that value in
  place"). Blocked at the write path along with the other three, same
  root cause.
- `discover_channels()` returns a `Dict[int, ChannelInfo]` keyed by SSRC,
  not a list of `ChannelInfo` (the brief's Step-3 leftover-check snippet
  iterates `for ch in discover_channels(...)` and reads `ch.ssrc`, which
  would iterate dict *keys* — already-int SSRCs — and then fail on
  `ch.ssrc`). Adjusted to `for ssrc, ch in discover_channels(...).items()`.

### Probe 1 — `probe_create_twice.py`

```
$ uv run python docs/audit/2026-08-12-alignment/probes/probe_create_twice.py
first:  {'frequency': None, 'sample_rate': None, 'preset': None, 'encoding': None}
second: {'frequency': None, 'sample_rate': None, 'preset': None, 'encoding': None}
CONVERGES
exit=0
```
Both `poll_channel()` calls returned `None` (channel never created; see
root cause) and `snap(None)` reads every field as `None` via
`getattr(info, f, None)` on a `None` object short-circuiting to the
default. `first == second` is trivially true because both are the same
all-`None` dict — **this is not evidence of convergence**, real or
otherwise. **NOT USABLE as evidence for Q1.**

### Probe 1b — `probe_create_twice_variant.py`

```
$ uv run python docs/audit/2026-08-12-alignment/probes/probe_create_twice_variant.py
first:  {'frequency': None, 'sample_rate': None, 'preset': None, 'encoding': None}
second: {'frequency': None, 'sample_rate': None, 'preset': None, 'encoding': None}
SAMPLE_RATE RESET (contradicts DEFECT verdict)
exit=1
```
Same failure mode: `second["sample_rate"]` is `None`, not `48000`, so the
probe's own preservation check fails — but only because nothing was ever
created, not because radiod reset anything. **NOT USABLE as evidence for
Q1.**

### Probe 2 — `probe_keepalive_settings.py`

```
$ uv run python docs/audit/2026-08-12-alignment/probes/probe_keepalive_settings.py
encoding before/after: None None
LOST
exit=1
```
`before`/`after` both `None` for the same reason. **NOT USABLE as
evidence for Q4.**

### Probe 3 — `probe_recovery_equivalence.py`

```
$ uv run python docs/audit/2026-08-12-alignment/probes/probe_recovery_equivalence.py
Traceback (most recent call last):
  File ".../probe_recovery_equivalence.py", line 62, in <module>
    started = stream.start()
  File ".../ka9q/managed_stream.py", line 231, in start
    self._channel = self._control.ensure_channel(
  File ".../ka9q/control.py", line 1894, in ensure_channel
    raise TimeoutError(
TimeoutError: Channel SSRC 1913996770 not verified within 5.0s. Requested: 7.940 MHz, usb, 12000 Hz
exit=1
```
`ManagedStream.start()` never gets past the initial `ensure_channel`
call, so the saboteur/recovery portion of the probe never runs. This *is*
informative in one narrow sense: it confirms `ensure_channel`'s
`TimeoutError` failure mode (documented in its docstring) triggers
correctly and with a clear message when a channel genuinely can't be
verified — but it says nothing about recovery equivalence (Q3), which
needs a channel to exist first. **NOT USABLE as evidence for Q3.**

### Step 3 — Leftover check

```
$ uv run python -c "... discover_channels(host, listen_duration=6) for host in (bee1, bee2) ..."
bee1-status.local total: 45 leftovers in range: []
bee2-status.local total: 45 leftovers in range: []
```
No leftovers on either host (expected, since no test channel was ever
actually created — every probe's `remove_channel()` in its `finally`
block ran against an SSRC radiod never had). Channel count on both hosts
(45) matched the pre-probe baseline taken before any probe ran.

### How this reconciles with Task 6

None of Task 6's five verdicts are confirmed or refuted by this session.
The `needs-empirical` list in idempotency.md is unchanged:
- **Q1** (`create_channel` convergence) — still DEFECT by code, still
  needs-empirical for live confirmation.
- **Q3** (recovery equivalence) — still VERIFIED-BY-CODE, still
  needs-empirical for a live `ManagedStream` restore.
- **Q4** (keepalive preservation) — still VERIFIED-BY-CODE for
  `set_channel_lifetime`, still needs-empirical.
- **Q5** (resequencer continuity) — still NEEDS-EMPIRICAL; not attempted
  this session (would need RTP data flowing, which requires the same
  broken write path to establish a channel in the first place).

**New item for the needs-empirical list**: before any future empirical
session can produce real evidence, the executing sandbox's outbound
route for `239.0.0.0/8` must reach the real interface carrying traffic to
`bee1-status.local`/`bee2-status.local` (currently pinned to `dev lo` in
this session's environment) — verify with `ip route get <resolved
status-group-IP>` before running probes, not just a reachability
ping/discover check, since discovery alone cannot distinguish a working
write path from a one-way read-only relay.

---

## Empirical results (Task 7) — Round 2 (unblocked)

**Status: UNBLOCKED and DONE.** `RadiodControl` already supports
multihomed hosts via a documented `interface: Optional[str]` constructor
argument (control.py:853) — passing this sandbox's LAN IP
(`192.168.1.176` on `ens18`) makes `RadiodControl` call
`setsockopt(IP_MULTICAST_IF, ...)` on the send socket (control.py:940-943),
which overrides the `239.0.0.0/8 -> dev lo` kernel route Round 1
diagnosed for *locally-originated* multicast sends specifically. No
system routing changes were made or needed. This alone got commands onto
the wire (re-verified with the same tcpdump method as Round 1 — see
below) but was **not sufficient by itself**: a second, independent
requirement was found and is the more interesting result of this round.

### Second requirement found: `destination=` must be explicit and must name an address radiod already uses

With `interface=` set, outbound packets correctly left `ens18` addressed
to the right group:port (confirmed by tcpdump), but `create_channel()`
calls with no `destination=` argument (relying on the docstring's claim
"If not specified, uses radiod's config-file default") still never
produced a visible channel — `discover_channels()` stayed at 45 for up
to 20s, repeatedly, across many SSRC values. A controlled back-to-back
A/B test (identical `interface=`, identical timing, two SSRCs created
30ms apart from the same script) isolated the cause:

```
ssrc_a = 3999900033; create_channel(..., ssrc=ssrc_a)                      # no destination
ssrc_b = 3999900034; create_channel(..., ssrc=ssrc_b, destination="239.139.172.41")  # explicit, pre-existing group
# 10s later:
A (no destination) present: False
B (explicit destination) present: True
```

A synthetic/unused destination (`239.253.99.99:5004`, chosen to not
collide with any live channel) was also tried and also failed to
produce a visible channel — so the requirement is not "any explicit
destination," specifically "a destination radiod is already configured
to accept output on." `239.139.172.41` (read from a live channel's
`ChannelInfo.multicast_address` via `discover_channels()` immediately
before use) was confirmed to work reproducibly across three separate
create calls in this round. `discover_channels()` shows **4** distinct
data-destination groups in use across the 45 real channels on b1
(`239.28.203.44`, `239.139.172.41`, `239.246.80.65`, `239.189.131.197`),
so this radiod deployment is not a single-group deployment — it appears
to restrict channel *creation* to a fixed, pre-configured set of output
destinations rather than accepting an arbitrary one from the client, and
(separately) has no config-file default destination that omitting
`destination=` can fall back to, contradicting the `create_channel`
docstring's claim for this specific deployment. This is a real,
reproducible finding about this radiod's configuration/behavior, not an
artifact of the sandbox — it persisted after the routing fix and across
repeated, controlled tries. Every Round 2 probe uses
`destination="239.139.172.41"` (an already-shared, many-SSRCs-per-group
address, which is exactly the `MultiStream` architecture's design
assumption per CLAUDE.md — "radiod publishes many bands into one
multicast group" — so injecting one more test SSRC's RTP output into
that same group does not disturb any of the real channels sharing it,
each already being demultiplexed by SSRC on the receive side).

Delivery re-confirmed by tcpdump (same method as Round 1): during a live
`create_channel`+`remove_channel` call with `interface="192.168.1.176"`,
`sudo tcpdump -i ens18 'udp and dst net 239.0.0.0/8 and dst port 5006'`
captured the outbound packets (`192.168.1.176.44285 > 239.205.73.40.5006`),
confirming they now leave the host — this is the "commands onto the
wire" check the coordinator asked for before proceeding, and it passed.

### Cheap end-to-end check (run before the full probe set, per instructions)

```
$ uv run python -c "create_channel(7_040_000.0, preset='usb', sample_rate=12000,
    ssrc=3999900001, destination='239.139.172.41'); poll_channel(...)"
ChannelInfo(ssrc=3999900001, preset='usb', sample_rate=12000, frequency=7040000.0,
            snr=-inf, multicast_address='239.139.172.41', port=5004, encoding=2, ...)
```
Real channel, real SSRC, real poll response. Proceeded to the full probe
set.

### Probes and adaptations (Round 2)

Every probe adds `interface="192.168.1.176"` to each `RadiodControl(...)`
construction and `destination="239.139.172.41"` to each `create_channel`/
`ManagedStream(...)` call, with each probe's docstring updated to explain
why. `RadiodStream`'s RTP receive path (`ka9q/stream.py`) needed no
interface override — it already calls `join_multicast_all_interfaces`
(stream.py ~421-457), joining the data group on every local IPv4
interface, so once channel creation succeeded, sample reception worked
without further changes. One additional probe,
`probe_keepalive_settings_variant.py`, was added in this round (see
below) because the original `probe_keepalive_settings.py`, now that it
could actually create a channel, exposed a probe-design problem worth
fixing rather than reporting as the final word on Q4.

### Probe 1 — `probe_create_twice.py` (SSRC 3999900001)

```
first:  {'frequency': 7040000.0, 'sample_rate': 12000, 'preset': 'usb', 'encoding': 2}
second: {'frequency': 7040000.0, 'sample_rate': 12000, 'preset': 'usb', 'encoding': 2}
CONVERGES
exit=0
```
Real result: two identical `create_channel()` calls against the same
SSRC produce identical polled state. This matches Task 6 Q1's own
prediction for the *identical-params* case (the delta-update mechanism
is invisible when nothing differs between the two calls — see Q1's text
and `probe_create_twice.py`'s docstring). `encoding=2` (S16BE) even
though neither call requested an encoding — this is this radiod's
preset/template default output encoding for `usb`, not a bug.

### Probe 1b — `probe_create_twice_variant.py` (SSRC 3999900004)

```
first:  {'frequency': 7040000.0, 'sample_rate': 48000, 'preset': 'usb', 'encoding': 2}
second: {'frequency': 7040000.0, 'sample_rate': 12000, 'preset': 'usb', 'encoding': 2}
SAMPLE_RATE RESET (contradicts DEFECT verdict)
exit=1
```
**This is the most important real result of Round 2, and it needs a
careful reading, not just the probe's own printed verdict.** The second
`create_channel()` call omitted `sample_rate` entirely (no
`OUTPUT_SAMPRATE` TLV sent), yet the channel's sample rate read back as
`12000`, not the first call's `48000`. A naive reading says this
*contradicts* Q1's DEFECT verdict (delta-update, value preserved) — but
`12000` is not an arbitrary/random value: it is `usb`'s **preset
default** sample rate on this radiod's `presets.conf`. Re-read against
Q1's own text in this file (see "Q1" section above): *"Whether a given
preset's ini stanza defines low/high keys is a runtime config fact... the
practical blast radius of the gap is needs-empirical even though the
code-level mechanism (delta-not-reset) is fully confirmed by source."*
This probe found exactly that runtime fact for `sample_rate` specifically
on this radiod: the `usb` preset's config stanza apparently *does* define
a default samprate, and `PRESET`'s handler (`loadpreset()`,
`src/modes.c:294`, cited in Q1) reapplies it on every create carrying a
`PRESET` tag — regardless of whether the current command also carries an
explicit `OUTPUT_SAMPRATE` override. The first call's `48000` almost
certainly worked because the explicit `OUTPUT_SAMPRATE` TLV in that same
packet is applied *after* (or independently of) `loadpreset()`'s
preset-default write, overriding it within that one packet — but with no
`OUTPUT_SAMPRATE` TLV in the second packet, only the preset default
survives. **Net conclusion: the delta-update mechanism Q1 describes is
real (this is not a template/struct reset — `frequency` and `preset`
persisted unchanged across both calls, and the channel's identity/PID
etc. were not reinitialized), but it is not blanket "everything an
earlier call set persists forever" either — any field the current
preset's config stanza defines gets reasserted to that preset's default
on every `PRESET`-bearing create, independent of delta-update semantics
for that specific field.** This refines Q1's DEFECT verdict rather than
overturning it: the risk Q1 describes (a stale custom value silently
surviving a re-create) is real for fields the preset doesn't define, and
does *not* apply to fields the preset does define (those get
preset-defaulted every time, which is arguably safer but has its own
surprise: a caller who set a custom `sample_rate` once, then later calls
`create_channel()` again without repeating it, silently *loses* the
custom value back to the preset default — the opposite failure mode from
what Q1's text emphasizes, but still an instance of "the docstring's
'uses default if not set' claim is only true for a fresh SSRC" being an
incomplete picture of what actually happens on a re-create).

### Probe 2 — `probe_keepalive_settings.py` (SSRC 3999900002, brief's original design, `lifetime=20`)

```
encoding before/after: 2 None
LOST
exit=1
```
Real result, but see the important caveat: `lifetime=20` is 20 radiod
**frames** (control.py:1313-1322: "~1000 frames ≈ 20s" at the default
20ms blocktime), i.e. **~0.4 seconds** of protected time, not 20 seconds
— almost certainly a units slip in the brief's original probe design (it
reused `lifetime=20` assuming it meant "20 of something ≈ the sleep
duration"). A single poll at t=0, then a flat 30s sleep with zero further
activity, gives a ~0.4s-lifetime channel essentially no chance to survive
regardless of any keepalive mechanism. The "LOST" result mixes two
different things (expired vs. encoding-changed-while-alive) into one
bit — see Probe 2b below for the corrected, two-phase test that actually
answers Q4.

### Probe 2b — `probe_keepalive_settings_variant.py` (SSRCs 3999900008, 3999900009; `lifetime=1000` ≈ 20s; added this round)

```
Phase A (plain poll_channel() every 5s, no set_channel_lifetime()):
  t+0s encoding: 2      # raced the separate OUTPUT_ENCODING packet -- see note
  t+5s encoding: 4
  t+10s encoding: 4
  t+15s encoding: 4
  t+20s encoding: None  # channel expired on schedule
  t+25s encoding: None
  t+30s encoding: None

Phase B (explicit set_channel_lifetime() refresh every 5s):
  t+0s .. t+30s encoding: 4  4  4  4  4  4  4   # survived, unchanged

Phase A survived to t+30s (bare poll IS a keepalive): False
Phase B survived to t+30s with encoding preserved throughout: True
exit=0
```
Two clean, real, reproducible findings:
1. **A bare `poll_channel()` query does NOT extend a channel's LIFETIME
   on this radiod build.** Phase A's channel died right on the raw
   `lifetime=1000`-frame (~20s) schedule despite four polls before that
   point. This contradicts `set_channel_lifetime`'s own docstring
   (control.py:1968-1970: *"Polling auto-extends a non-zero lifetime to
   at least ~20s, so a client... should call this method **(or any other
   poll)** periodically as a keep-alive"*) — the parenthetical "(or any
   other poll)" claim does not hold empirically for a plain status query
   on this build. This is a **new defect candidate for Task 8/9**: either
   the docstring overclaims (fix: narrow the claim to
   `set_channel_lifetime`/any LIFETIME-tag-bearing command specifically),
   or radiod's own idle-timeout-floor logic doesn't cover a bare CMD
   query the way the comment assumes and the assumption needs
   cross-checking against `AUDIT_HEAD`'s `src/radio_status.c`.
2. **`set_channel_lifetime()` itself works exactly as designed and as
   Q4's VERIFIED-BY-CODE verdict predicted**: Phase B's channel survived
   the full 30s window with encoding reading back as `F32LE` (`4`) at
   every single 5s check, confirming both the lifetime-refresh and the
   encoding-re-assertion halves of that method's contract
   (control.py:1990-2000's "Note" about `731ce5e`'s
   HamSCI/ka9q-python#3 fix).

(The `t+0s encoding: 2` in Phase A is a minor, separately-explainable
observation, not part of either finding above: `create_channel` sends
the main creation packet and the `OUTPUT_ENCODING` packet as two
separate UDP sends — control.py:1468 then :1471-1487 — so a poll that
lands in the brief window between them can observe the preset's default
encoding (`2`, S16BE) before the second packet is processed. By t+5s it
had settled to `4` as expected.)

### Probe 3 — `probe_recovery_equivalence.py` (real SSRC allocated: `1517029203`)

```
Stream drop detected: No packets for 3.1s (timeout: 3.0s)
actual_ssrc (hash-derived, not test-range): 1517029203
before: {'frequency': 7940003.0, 'sample_rate': 12000, 'preset': 'usb', 'encoding': 2}
after:  {'frequency': 7940003.0, 'sample_rate': 12000, 'preset': 'usb', 'encoding': 2}
EQUIVALENT
exit=0
```
Full end-to-end success: `ManagedStream.start()` created a real channel,
received real RTP samples, detected the simulated drop (`saboteur.
remove_channel()`) within its configured `drop_timeout_sec=3.0` window
("No packets for 3.1s"), and the background health-monitor thread
recreated the channel via `ensure_channel()` (which reuses
`self._destination`, confirmed by reading `_attempt_restore()` at
managed_stream.py:396-440) — all without any code changes, exactly as
`ManagedStream`'s design intends. `before == after` on
`frequency`/`sample_rate`/`preset`/`encoding` **confirms Task 6 Q3's
VERIFIED-BY-CODE verdict with a live radiod restart-recovery cycle** —
the strongest, cleanest positive result of this round.

Adaptation reminder: `actual_ssrc` (`1517029203`) is hash-derived by
`allocate_ssrc` from the probe's frequency/preset/params — it is **not**
in the `3999900000-3999900999` range, because `ManagedStream` has no
`ssrc=` override (see the probe's docstring for the full reasoning, this
was also true and documented in Round 1). It was explicitly removed via
`c.remove_channel(actual_ssrc)` in `finally` and confirmed absent by a
follow-up `discover_channels()` check.

### Step 3 — Leftover check (Round 2, after all probes)

```
bee1-status.local total: 45 leftovers in range: []
bee2-status.local total: 45 leftovers in range: []
```
Zero leftovers in the reserved SSRC range on either host — channel count
back to the 45-channel baseline on both. The recovery probe's
out-of-range SSRC (`1517029203`) was separately confirmed absent via a
follow-up `discover_channels()` call (`1517029203 present: False`). All
other SSRCs created during this round's diagnosis (interface/destination
troubleshooting: `3999900002`, `3999900003`, `3999900005`-`3999900010`,
`3999900020`, `3999900021`, `3999900030`-`3999900034`, plus two
auto-allocated diagnostic SSRCs `1929612220`/`1483579970` that were never
actually created because they predated the `destination=` fix) were each
explicitly removed by their diagnostic scripts and are covered by the
same clean 45-channel/empty-leftover result above.

### How Round 2 reconciles with Task 6

- **Q1** (`create_channel` convergence): **DEFECT confirmed live**, with
  an important refinement from Probe 1b — see that probe's writeup
  above. The delta-update mechanism is real and reproducible, but its
  actual effect on a given field on a given re-create depends on whether
  the requested preset's config stanza defines that field: fields the
  preset defines get preset-defaulted on every `PRESET`-bearing create
  (which is its own, different footgun — silent loss of a previously-set
  custom value on a "harmless" re-create); fields the preset doesn't
  define behave exactly as Q1's original text describes (stale value
  silently persists). Recommend Task 8 update idempotency.md's Q1
  section (or a linked erratum) with this empirical refinement, and
  update the `create_channel` docstring's "uses radiod's default if not
  set" claim to note it can mean either "radiod's preset default" or
  "whatever was already there," depending on whether the field is
  preset-controlled — the current wording implies the former
  unconditionally.
- **Q3** (recovery equivalence): **VERIFIED-BY-CODE claim now also
  VERIFIED LIVE** for `ManagedStream` — Probe 3 is a clean, real
  drop-detect-recover cycle producing identical polled state.
  `MultiStream`'s separate restore defect (Step 2's row 6) was not
  re-tested this round (out of this task's three-probe scope; still
  code-derived only).
- **Q4** (keepalive preservation): **VERIFIED-BY-CODE claim now also
  VERIFIED LIVE** for `set_channel_lifetime` specifically (Probe 2b,
  Phase B) — encoding survives 30s of repeated explicit refresh calls
  intact. **New finding, not previously flagged by Task 6**: a bare
  `poll_channel()` query does *not* extend LIFETIME despite
  `set_channel_lifetime`'s docstring claiming "(or any other poll)"
  does — Probe 2b, Phase A. Flagging this as a candidate defect/erratum
  for Task 8 (either fix the docstring's overclaim, or — if this is
  radiod-build-specific rather than universal — scope the claim to
  builds with the relevant idle-timeout behavior).
- **Q5** (resequencer continuity): still NEEDS-EMPIRICAL; genuinely out
  of this task's scope (would need a dedicated dual-receiver probe
  comparing two live `PacketResequencer` consumers of the same real
  stream, not attempted this round).

### Concerns / follow-ups for Task 8

1. Update idempotency.md's Q1 verdict text (or add a cross-reference) to
   incorporate Probe 1b's refinement about preset-controlled fields.
2. Investigate/fix the `set_channel_lifetime` docstring's "(or any other
   poll)" claim (control.py:1968-1970) — Probe 2b's Phase A contradicts
   it on this radiod build. Check whether this is a radiod-build/version
   difference (worth a Task 9/AUDIT_HEAD cross-check of the idle-timeout
   logic in `src/radio_status.c`) or simply an inaccurate docstring that
   should be narrowed to "call `set_channel_lifetime()` (or any command
   that includes a LIFETIME tag) periodically."
3. `create_channel`'s destination-default docstring claim ("If not
   specified, uses radiod's config-file default") does not hold on
   `bee1-status.local`/`bee2-status.local` — omitting `destination=`
   silently fails to create the channel rather than falling back to a
   config default. Worth deciding whether this is deployment-specific
   (some radiod configs have no default destination configured) and, if
   so, whether `ensure_channel`/`create_channel` should surface a
   clearer error instead of the current silent no-op-looking failure
   (the command is accepted at the transport level with no error raised
   to the caller — the only symptom is a subsequent `poll_channel()`
   timeout or `ensure_channel`'s `TimeoutError`, which does not point at
   `destination` as the cause).
