# Header Contract Diff — pin..AUDIT_HEAD

**Pin:** `14d780af624e821941708bd0d64fd895a0c80a2a` (2026.07.30-2-trixie1)
**AUDIT_HEAD:** `cedec349f7b4212078de3e007d142b4c64d36546` (2026.08.12-1-trixie1)
**Range:** `14d780af..cedec349` (149 commits, per Task 2)

This document is the definitive enum-level analysis of the wire/ABI
contract between the pin and `AUDIT_HEAD`, and the advance verdict Task 9
gates on.

---

## Step 1: Raw header diffs

Command run verbatim from the brief, in the read-only clone at
`~/audit/ka9q-radio` (no checkout performed — `git diff`/`git show` only):

```bash
cd ~/audit/ka9q-radio
for f in $(git ls-tree -r --name-only cedec349f7b4212078de3e007d142b4c64d36546 | grep -E '(^|/)(status|rtp|multicast)\.h$'); do
  git diff 14d780af..cedec349f7b4212078de3e007d142b4c64d36546 -- "$f"
done > /tmp/header-diff.txt
```

Matched files: `src/multicast.h`, `src/rtp.h`, `src/status.h`.

```diff
=== src/multicast.h ===
diff --git a/src/multicast.h b/src/multicast.h
index c56d5851..5bc39338 100644
--- a/src/multicast.h
+++ b/src/multicast.h
@@ -3,6 +3,7 @@
 
 #ifndef _MULTICAST_H
 #define _MULTICAST_H 1
+#include <stddef.h>
 #include <stdint.h>
 #include <stdbool.h>
 #include <sys/socket.h>
=== src/rtp.h ===
diff --git a/src/rtp.h b/src/rtp.h
index 8b1afda6..18ef43a1 100644
--- a/src/rtp.h
+++ b/src/rtp.h
@@ -3,6 +3,7 @@
 
 #include <stdint.h>
 #include <stddef.h>
+#include <stdbool.h>
 
 #define DEFAULT_MCAST_PORT ((uint16_t)5004)
 #define DEFAULT_RTP_PORT ((uint16_t)5004)
=== src/status.h ===
(no diff — status.h is byte-identical between pin and AUDIT_HEAD)
```

**Deviation from the brief's literal Step 1 command, disclosed:** the
brief's glob (`(status|rtp|multicast)\.h$`) does not match `src/radio.h`,
which is where Task 2 found the actual enum change (`enum demod_type`,
commit `654fda5e`). `radio.h` is one of the four headers `sync_types.py`
and `check_upstream_drift.py` track (`HEADER_FILES` maps it to
`DemodType`), so it is unambiguously in scope for "any other
client-visible enum" per Step 2. Its diff is included below for
completeness; `window.h` (the fourth tracked header) is also included and
has no enum change.

```diff
=== src/radio.h (enum-relevant hunk only; full diff also touches
    struct channel, function signatures — see below) ===
 enum demod_type {
+  INVALID_DEMOD = -1,   // used as sentinel
   LINEAR_DEMOD = 0,     // Linear demodulation, i.e., everything else: SSB, CW, DSB, CAM, IQ
   FM_DEMOD,             // Frequency/phase demodulation
   WFM_DEMOD,            // wideband frequency modulation (broadcast stereo)
   SPECT_DEMOD,          // Spectrum analysis pseudo-demod
   SPECT2_DEMOD,         // spectrum v2: 8-bit log bins, low-to-high order
+  IDLE_DEMOD,           // placeholder that just processes commands
   N_DEMOD,              // Dummy equal to number of valid entries
 };
```

(Full `radio.h` diff also includes: a `channel.inuse bool` →
`_Atomic enum {CHANNEL_IDLE,...} state` refactor, `struct channel` →
`chan_t` typedef, `create_chan`/`close_chan` → `lookup_or_create_chan`
signature changes, and new `advertise`/`use_dns` fields. None of these
are part of the wire TLV enums; they are internal radiod C structures not
reflected in `ka9q/types.py` or any TLV encode/decode path.)

```diff
=== src/window.h ===
diff --git a/src/window.h b/src/window.h
index 689f9c5a..49b2b182 100644
--- a/src/window.h
+++ b/src/window.h
@@ -1,6 +1,9 @@
 #ifndef _WINDOW_H
 #define _WINDOW_H 1
 
+#include <stddef.h>
+#include <stdbool.h>
+
 // Window functions
 int make_kaiser(double * const window,int const M,double const beta);
```
No `enum window_type` change.

**Other headers touched in range** (`avahi.h`, `ax25.h`, `filter.h`,
`iir.h`, `import.h`, `misc.h`, `monitor.h`, `osc.h`, `rx888.h`): checked
with `git diff ... -- src/<f> | grep -E '^\+.*enum|^-.*enum'` — zero
matches in all nine files. None define or modify a client-visible
protocol enum (`status_type`, `encoding`, `demod_type`, `window_type`);
they are radiod-internal driver/build/session-management headers not
tracked by `sync_types.py` and not part of the TLV wire surface consumed
by `ka9q/`.

**Conclusion of Step 1:** `enum demod_type` in `src/radio.h` is the
*only* client-visible enum that changed in `14d780af..cedec349`.
`enum status_type` (`status.h`), `enum encoding` (`rtp.h`), and
`enum window_type` (`window.h`) are byte-identical.

---

## Step 2: Enum-level analysis

| enum | member | pin value | head value | change | stream-critical? |
|---|---|---|---|---|---|
| `demod_type` (`DemodType`) | `INVALID_DEMOD` | — | `-1` | **added** | no — `_classify_change()` returns `"warn"` for any `added` member regardless of enum; also: **not detected** by `sync_types.py`/`check_upstream_drift.py` at all (see Step 3 reconciliation — negative-literal parser gap) |
| `demod_type` (`DemodType`) | `LINEAR_DEMOD` | `0` | `0` | unchanged | n/a |
| `demod_type` (`DemodType`) | `FM_DEMOD` | `1` | `1` | unchanged | n/a |
| `demod_type` (`DemodType`) | `WFM_DEMOD` | `2` | `2` | unchanged | n/a |
| `demod_type` (`DemodType`) | `SPECT_DEMOD` | `3` | `3` | unchanged | n/a |
| `demod_type` (`DemodType`) | `SPECT2_DEMOD` | `4` | `4` | unchanged | n/a |
| `demod_type` (`DemodType`) | `IDLE_DEMOD` | — | `5` | **added** | no — `added` kind is always `"warn"` (script output confirms: `reason: "new field upstream — review for client exposure"`) |
| `demod_type` (`DemodType`) | `N_DEMOD` | `5` | `6` | **value_changed** | **yes, per the allowlist's own coded rule** — `DemodType` is a member of `STREAM_CRITICAL_ENUMS = frozenset({"Encoding", "DemodType"})` in `scripts/check_upstream_drift.py`; that set's contract, per its own comment, is "Enums whose every value is stream-critical: any TLV-value shift means clients decode wrong format." `_classify_change()` implements this literally: `if enum_class in STREAM_CRITICAL_ENUMS: return ("fail", ...)` for *any* `value_changed`/`removed` member, independent of the member's name. Confirmed empirically (Step 3). |
| `status_type` (`StatusType`) | (all ~110 members) | — | — | **no changes** | n/a — `status.h` is byte-identical in this range |
| `encoding` (`Encoding`) | (all members) | — | — | **no changes** | n/a — `rtp.h`'s only change is an `#include <stdbool.h>` |
| `window_type` (`WindowType`) | (all members) | — | — | **no changes** | n/a — `window.h`'s only change is two `#include`s |

**Cross-check against `ka9q/types.py` (current, on disk):**

```python
class DemodType:
    LINEAR_DEMOD = 0
    FM_DEMOD = 1
    WFM_DEMOD = 2
    SPECT_DEMOD = 3
    SPECT2_DEMOD = 4
    N_DEMOD = 5   # pin value — stale vs. AUDIT_HEAD's N_DEMOD=6
```
`types.py` has no `INVALID_DEMOD` or `IDLE_DEMOD` entries (expected —
it mirrors the pin, not `AUDIT_HEAD`). `LINEAR_DEMOD`..`SPECT2_DEMOD`
values match the pin exactly and — critically — **also match
`AUDIT_HEAD`**, since the insertion of `IDLE_DEMOD`/`INVALID_DEMOD`
happened at the start (`INVALID_DEMOD=-1`, before `LINEAR_DEMOD=0`) and
the end (`IDLE_DEMOD` before `N_DEMOD`) of the enum, not in the middle.
None of the five wire-transmitted demod codes shifted.

---

## Step 3: `sync_types.py` dry-run and reconciliation

**Literal brief command, run first (against `~/audit/ka9q-radio`'s
working tree, which is checked out at the *pin*, `14d780af`, not
`AUDIT_HEAD` — per the global constraint, this repo is read-only and no
checkout was performed):**

```bash
cd /opt/git/sigmond/ka9q-python
python3 scripts/sync_types.py --ka9q-radio ~/audit/ka9q-radio --diff
```
```
types.py is in sync with ka9q-radio 14d780af624e
```
This is a trivial/expected result — it compares `types.py` against the
pin's own checked-out tree, so of course they match. It does **not**
exercise the pin→AUDIT_HEAD delta and is not sufficient for Step 3's
purpose on its own.

**To get a meaningful pin→AUDIT_HEAD diff without checking out the
read-only clone**, a throwaway mirror of the four tracked headers
(`status.h`, `rtp.h`, `radio.h`, `window.h`) was built via
`git show <AUDIT_HEAD>:src/<f>` (no `git checkout`, `git diff`/`git
show` only) into a scratch directory, `git init`'d there only so
`sync_types.py`'s `get_git_commit()` helper (which shells out to
`git rev-parse HEAD`) has a repo to resolve — this does not touch
`~/audit/ka9q-radio`:

```bash
mkdir -p /tmp/.../audit-head-mirror/src
cd ~/audit/ka9q-radio
for f in status.h rtp.h radio.h window.h; do
  git show cedec349f7b4212078de3e007d142b4c64d36546:src/$f \
    > /tmp/.../audit-head-mirror/src/$f
done
cd /tmp/.../audit-head-mirror && git init -q && git add -A && \
  git -c user.email=audit@local -c user.name=audit commit -q -m mirror

cd /opt/git/sigmond/ka9q-python
python3 scripts/sync_types.py --ka9q-radio /tmp/.../audit-head-mirror --diff
```
```
Protocol drift detected vs ka9q-radio 5c4477df4e95:

  DemodType: MISSING  IDLE_DEMOD = 5  // placeholder that just processes commands
  DemodType: VALUE MISMATCH  N_DEMOD: C=6, Python=5

  2 issue(s) found

Run 'python scripts/sync_types.py --apply' to synchronize.
```

**`check_upstream_drift.py`, run directly against `~/audit/ka9q-radio`
(read-only — `origin/main` in that clone already resolves to
`AUDIT_HEAD` exactly, so `--no-fetch` avoids any network/mutation):**

```bash
python3 scripts/check_upstream_drift.py --ka9q-radio ~/audit/ka9q-radio --no-fetch --json
```
Relevant excerpt (full JSON also lists all 149 commits with
`touches_headers`, matching Task 2's log):
```json
{
  "severity": "fail",
  "header_deltas": [
    {
      "header": "src/radio.h",
      "enum": "DemodType",
      "severity": "fail",
      "changes": [
        {"kind": "added", "name": "IDLE_DEMOD", "head": 5, "severity": "warn",
         "reason": "new field upstream — review for client exposure"},
        {"kind": "value_changed", "name": "N_DEMOD", "pin": 5, "head": 6,
         "severity": "fail", "reason": "DemodType value shift — clients decode wrong format"}
      ]
    }
  ],
  "summary": "1 stream-critical change — RTP delivery at risk"
}
```

### Reconciliation with Step 2's table

`sync_types.py --diff` and `check_upstream_drift.py` **agree with each
other** (2 issues: `IDLE_DEMOD` missing, `N_DEMOD` value mismatch) but
**disagree with the raw header diff**, which shows **three** changes
(`INVALID_DEMOD` added, `IDLE_DEMOD` added, `N_DEMOD` value-shifted).
Root cause, confirmed by reading `parse_c_enum()` in `sync_types.py`:

```python
m = re.match(
    r"([A-Z][A-Z0-9_]*)\s*(?:=\s*(\d+))?\s*,?\s*(?://\s*(.*))?\s*$",
    line,
)
```
The value-capture group is `(\d+)` — digits only, no sign. Against
`INVALID_DEMOD = -1,   // used as sentinel`, `\d+` cannot match `-1`
(the `-` isn't consumed), the optional `(?:=\s*(\d+))?` group therefore
fails to match at all, and the *remaining* required tail
(`\s*,?\s*(?://...)?\s*$`) then fails against the leftover
`= -1,   // used as sentinel` text — so the whole line-level regex
returns no match and the line is silently `continue`d past. **Both
`sync_types.py` and `check_upstream_drift.py` share this parser
(`check_upstream_drift.py` imports `parse_c_enum` from `sync_types`), so
both tools are blind to `INVALID_DEMOD` in this diff.** This is a
tooling gap (enum parser doesn't support negative literals), not a
disagreement about the underlying C source — verified directly against
`radio.h`'s text in both Step 1 and here. It does not change the
verdict (see Step 4: `INVALID_DEMOD` is an `added` member, which is
always `"warn"`-severity regardless of whether the tooling sees it or
not), but it is a real detection gap worth fixing in `sync_types.py`
before relying on `--check` in CI for a header that ever adds a
negative-valued sentinel again. Out of scope to fix here — only
`docs/audit/` files change in this task.

Net: Step 2's enum table (3 changes) is the ground truth (from the raw
diff); the tooling's 2-issue report is consistent with it modulo the
known negative-literal blind spot.

---

## Step 4: Usage checks

Brief's literal command form, run against all four named client repos
plus `ka9q/` (no hits for the (non-existent, since neither `StatusType`
nor `Encoding` changed) `N_DEMOD` name under those two classes — included
for completeness since the brief phrases the pattern generically):

```bash
grep -rn "StatusType\.N_DEMOD\|Encoding\.N_DEMOD" \
  ka9q-python/ka9q hf-timestd wspr-recorder psk-recorder meteor-scatter
```
→ **0 hits** (exit 1).

Applied to the enum that actually changed, `DemodType`:

```bash
grep -rn "DemodType\.N_DEMOD" \
  ka9q-python/ka9q hf-timestd wspr-recorder psk-recorder meteor-scatter
```
→ **0 hits** (exit 1).

```bash
grep -rn "IDLE_DEMOD\|INVALID_DEMOD" \
  ka9q-python/ka9q hf-timestd wspr-recorder psk-recorder meteor-scatter
```
→ **0 hits** (expected — these members don't exist in the pinned
`types.py` yet).

```bash
grep -rln "DemodType" ka9q-python/ka9q hf-timestd wspr-recorder psk-recorder meteor-scatter
```
→ **6 hits**, all within `ka9q-python/ka9q/`: `spectrum_stream.py`,
`__init__.py`, `types.py`, `tui.py`, `status.py`, `cli.py`. **0 hits**
in any of the four named client repos.

Extended (beyond the brief's named four, per `CLAUDE.md`'s fuller
sigmond-suite client list) for extra assurance:
```bash
grep -rln "DemodType" ka9q-python/ka9q codar-sounder hfdl-recorder
```
→ same 6 `ka9q-python/ka9q/` hits, **0** in `codar-sounder` or
`hfdl-recorder`.

**What the 6 `ka9q/` hits actually reference** (`grep -n
"DemodType\." ka9q/spectrum_stream.py ka9q/__init__.py ka9q/tui.py
ka9q/status.py ka9q/cli.py`):

```
spectrum_stream.py:73:  demod_type: int = DemodType.SPECT2_DEMOD,
cli.py:86:    if st.demod_type == DemodType.FM_DEMOD:
cli.py:92:    elif st.demod_type == DemodType.LINEAR_DEMOD:
cli.py:102:   elif st.demod_type in (DemodType.SPECT_DEMOD, DemodType.SPECT2_DEMOD):
status.py:313-317: {LINEAR_DEMOD: "Linear", FM_DEMOD: "FM", WFM_DEMOD: "WFM",
                    SPECT_DEMOD: "Spectrum", SPECT2_DEMOD: "Spectrum2"}
tui.py:198,206,217: FM_DEMOD / LINEAR_DEMOD / (SPECT_DEMOD, SPECT2_DEMOD)
```
Only the five **unchanged-value** members (`LINEAR_DEMOD`, `FM_DEMOD`,
`WFM_DEMOD`, `SPECT_DEMOD`, `SPECT2_DEMOD`) are referenced anywhere.
`N_DEMOD` itself:
```bash
grep -rn "N_DEMOD" --include=*.py .   # from ka9q-python repo root
```
→ **1 hit**, the definition itself (`ka9q/types.py:170`,
`N_DEMOD = 5  # Dummy equal to number of valid entries`). Zero
consumers anywhere in `ka9q-python` or any client repo depend on its
*value* by that name.

**Related but distinct finding (not a wire-format break, flagged for
Task 4/9 remediation scope):** `ka9q/control.py:2854` hardcodes the
valid demod range as a magic-number literal, not by referencing
`DemodType`/`N_DEMOD`:
```python
if not (0 <= demod_type <= 4):
    raise ValidationError(f"Invalid demod_type: {demod_type} (must be 0-4)")
```
This bound silently encodes "N_DEMOD − 1" as of the pin (4). It does not
break on `AUDIT_HEAD` (still correctly rejects garbage), but it will
also reject `5` (`IDLE_DEMOD`) once `types.py` is regenerated to know
about it — i.e. it is downstream *capability-exposure* work for whoever
implements Task 4, not a `grep`-detectable "uses `DemodType.N_DEMOD`"
hit, and not part of the Step 4 verdict test as literally specified in
the brief.

---

## Verdict

Applying the brief's Step 4 criterion mechanically: *"critical iff [the
removed/value-shifted member] is stream-critical per
`scripts/check_upstream_drift.py`'s allowlist **OR** used anywhere in
`ka9q/` or a client repo."*

There is exactly one removed/value-shifted member across all
client-visible enums in this range: `DemodType.N_DEMOD` (`5 → 6`,
`value_changed`; nothing was `removed`).

- **Allowlist disjunct — TRUE.** `check_upstream_drift.py` places the
  entire `DemodType` enum in `STREAM_CRITICAL_ENUMS`, and its
  `_classify_change()` unconditionally assigns `severity="fail"` to any
  `value_changed`/`removed` member of that enum, independent of which
  member it is. This was verified by direct code inspection (not just
  quoting the tool's summary label) and confirmed by actually running
  the tool (`--json` output above, `severity: "fail"`,
  `reason: "DemodType value shift — clients decode wrong format"`).
- **Usage disjunct — FALSE.** Zero references to `N_DEMOD` (by name, in
  any qualified form) exist anywhere in `ka9q/` or the four named client
  repos (`hf-timestd`, `wspr-recorder`, `psk-recorder`,
  `meteor-scatter`), nor in the two additional sigmond-suite clients
  checked for extra coverage (`hfdl-recorder`, `codar-sounder`). The
  five demod codes that *are* referenced and *are* transmitted over the
  wire (`LINEAR_DEMOD`, `FM_DEMOD`, `WFM_DEMOD`, `SPECT_DEMOD`,
  `SPECT2_DEMOD`) kept their exact numeric values (`0`–`4`) between pin
  and `AUDIT_HEAD`.

The criterion is an **OR**: the allowlist disjunct alone is sufficient.
Even though the usage disjunct is negative — meaning there is currently
no live client code that would misdecode a packet as a result of this
specific range, because `N_DEMOD` is a count sentinel never itself
placed on the wire, not a transmitted demod code — `check_upstream_drift.py`'s
own coded policy for `DemodType` (a blanket "any value shift in this
enum is critical" rule, chosen conservative-by-design per its header
comment) mechanically classifies this as stream-critical. Per the
brief's literal criteria, that is dispositive.

```
CONTRACT: CRITICAL-CHANGE — DO NOT ADVANCE
```

**Justification chain:**
1. `enum demod_type` (`src/radio.h`) is the only client-visible protocol
   enum touched in `14d780af..cedec349` (`status.h`/`rtp.h`/`window.h`
   byte-identical; nine other touched headers contain no enums at all).
2. Within it, one member value-shifted: `N_DEMOD` `5 → 6` (insertion of
   `IDLE_DEMOD` before it in commit `654fda5e`). No member was removed.
   The five wire-transmitted demod codes are unchanged.
3. `DemodType` is listed in `check_upstream_drift.py`'s
   `STREAM_CRITICAL_ENUMS` allowlist, whose documented and coded
   contract makes *any* value shift within it fail-severity, regardless
   of which member shifted or whether that member is itself ever
   encoded on the wire.
4. Per the brief's OR criterion, allowlist membership alone is
   sufficient to make this `CRITICAL-CHANGE`, independent of the (here,
   negative) live-usage check.
5. Practical remediation scope for whoever advances the pin (Task 4):
   `sync_types.py --apply` to add `IDLE_DEMOD=5` and bump
   `N_DEMOD` to `6` in `types.py`; separately, `INVALID_DEMOD=-1` will
   need a manual add or a parser fix (see Step 3 reconciliation) since
   the negative-literal gap makes `--apply` silently omit it; and
   optionally widen `control.py:2854`'s hardcoded `<= 4` bound to expose
   `IDLE_DEMOD` as a settable capability. None of this is a "someone
   left a hard-coded protocol constant that's now silently wrong"
   emergency — it is new-capability integration work gated by this
   verdict as a matter of process, not a live-bug fix.
