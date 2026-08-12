# Upstream Alignment & Client Compatibility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Audit ka9q-python against 144+ commits of ka9q-radio upstream drift and against every sigmond-suite client, remediate the known gaps, and install durable guardrails so future changes cannot silently break clients.

**Architecture:** Three sequential phases per the approved spec ([docs/superpowers/specs/2026-08-12-upstream-alignment-and-client-compat-design.md](../specs/2026-08-12-upstream-alignment-and-client-compat-design.md)): Phase 1 produces an evidence-backed audit report in `docs/audit/2026-08-12-alignment/`; Phase 2 executes the remediations already known to be needed (pin advance, doc corrections); Phase 3 adds a client-usage manifest, an API contract test, and idempotency integration tests. Finding-driven remediation beyond the known items gets its own follow-up plan at Checkpoint A.

**Tech Stack:** Python 3 / pytest (run via `uv run pytest`), git, the repo's own `scripts/sync_types.py` and `scripts/check_upstream_drift.py`.

## Global Constraints

- Integration hosts: **only** `bee1-status.local` (b1) or `bee2-status.local` (b2). Never touch b3/b4 (dev/production).
- All ephemeral test channels use SSRCs in **3999900000–3999900999** and are destroyed in `finally:` blocks — b1/b2 must be left clean even on failure.
- No modifications to any client repo (`hf-timestd`, `wspr-recorder`, `psk-recorder`, `meteor-scatter`, …). Findings about them go in the report only.
- Pinned ka9q-radio commit: `14d780af624e821941708bd0d64fd895a0c80a2a` (file `ka9q_radio_compat`).
- Fresh upstream clone lives at `~/audit/ka9q-radio` (owned by `hamsci`; the `/opt/git/sigmond/ka9q-radio` checkout is left untouched).
- Do **not** advance the pin if Task 3 finds a stream-critical field removed or value-shifted; stop and coordinate with the user (CLAUDE.md operator workflow).
- Unit suite (`uv run pytest`, no radiod needed) must stay green after every commit.
- Every commit message ends with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

---

## Phase 1 — Audit

### Task 1: Audit workspace + authoritative upstream ref

**Files:**
- Create: `docs/audit/2026-08-12-alignment/upstream.md` (preamble)
- Create: `~/audit/ka9q-radio` (clone; not committed)

**Interfaces:**
- Produces: clone at `~/audit/ka9q-radio` with `origin` = GitHub, used by Tasks 2, 3, 4, 9. Records `AUDIT_HEAD` (the audited upstream sha) in `upstream.md`; Tasks 2–4 and 9 must use that exact sha, not a moving branch.

- [ ] **Step 1: Clone locally, retarget origin to GitHub, fetch**

```bash
mkdir -p ~/audit
git clone /opt/git/sigmond/ka9q-radio ~/audit/ka9q-radio
cd ~/audit/ka9q-radio
git remote set-url origin https://github.com/ka9q/ka9q-radio.git
git fetch origin
git rev-parse origin/main   # this sha is AUDIT_HEAD — record it
```
Expected: fetch succeeds; `origin/main` sha is equal to or newer than `1c0a4231d20f`.

- [ ] **Step 2: Re-run the drift watcher against the fresh clone**

```bash
cd /opt/git/sigmond/ka9q-python
python3 scripts/check_upstream_drift.py --ka9q-radio ~/audit/ka9q-radio 2>&1 | tee /tmp/drift.txt
python3 scripts/check_upstream_drift.py --ka9q-radio ~/audit/ka9q-radio --json > /tmp/drift.json
```
Expected: runs clean (no permission error). Record severity, commit count, and per-field detail.

- [ ] **Step 3: Write `upstream.md` preamble**

Create `docs/audit/2026-08-12-alignment/upstream.md` containing: audit date, pin sha, `AUDIT_HEAD` sha (pinned literal from Step 1), commit count `git rev-list --count 14d780af..AUDIT_HEAD`, and the drift watcher's verdict + per-field JSON (inline, fenced). State explicitly which sha the whole audit is frozen to.

- [ ] **Step 4: Commit**

```bash
git add docs/audit/2026-08-12-alignment/upstream.md
git commit -m "audit: freeze upstream ref and record drift-watcher baseline"
```

### Task 2: Classify every unpinned upstream commit

**Files:**
- Modify: `docs/audit/2026-08-12-alignment/upstream.md` (append "Commit classification" section)

**Interfaces:**
- Consumes: `AUDIT_HEAD` from `upstream.md` (Task 1).
- Produces: a classification table Task 8 ranks findings from. Classes (exact spelling, used again in Task 8): `payload-rtp`, `status-tlv`, `capability`, `behavior`, `internal`, `packaging`.

- [ ] **Step 1: Enumerate the range**

```bash
cd ~/audit/ka9q-radio
git log --oneline --reverse 14d780af..<AUDIT_HEAD> > /tmp/commits.txt
wc -l /tmp/commits.txt
```

- [ ] **Step 2: Classify each commit**

For each commit: `git show --stat <sha>`; read the full diff (`git show <sha>`) for any commit touching `*.h`, `radio.c`, `rtp.c`, `multicast.c`, `status.c`, `radio_status.c`, or demod sources (`fm.c`, `linear.c`, `wfm.c`, `spectrum.c`). Assign exactly one class:
- `payload-rtp` — changes what bytes/timing clients receive on the RTP stream
- `status-tlv` — changes the status/command TLV contract (enum values, encoding)
- `capability` — adds a feature ka9q-python could expose
- `behavior` — changes radiod runtime behavior clients could observe (defaults, AGC, filters)
- `internal` — refactors, comments, null checks, build tweaks; no observable change
- `packaging` — release tags, docs, systemd/service files

Append a table to `upstream.md`: `| sha | subject | class | note (1 line, why) |` — one row per commit, no elisions. Commits classed `payload-rtp`, `status-tlv`, or `capability` get a 2–4 sentence expansion below the table citing the file/function changed.

- [ ] **Step 3: Sanity totals**

Append a count-per-class line. The sum must equal the Step 1 count — if not, find the missed commits.

- [ ] **Step 4: Commit**

```bash
git add docs/audit/2026-08-12-alignment/upstream.md
git commit -m "audit: classify all unpinned upstream commits"
```

### Task 3: Header contract diff + sync dry-run

**Files:**
- Create: `docs/audit/2026-08-12-alignment/contract.md`

**Interfaces:**
- Consumes: clone + `AUDIT_HEAD` (Task 1).
- Produces: a verdict line Task 9 gates on, exactly one of: `CONTRACT: SAFE-TO-ADVANCE` or `CONTRACT: CRITICAL-CHANGE — DO NOT ADVANCE`.

- [ ] **Step 1: Raw header diffs**

```bash
cd ~/audit/ka9q-radio
for f in $(git ls-tree -r --name-only <AUDIT_HEAD> | grep -E '(^|/)(status|rtp|multicast)\.h$'); do
  git diff 14d780af..<AUDIT_HEAD> -- "$f"
done > /tmp/header-diff.txt
```

- [ ] **Step 2: Enum-level analysis**

From the diff, list every `enum status_type` / `enum encoding` (and any other client-visible enum) member that was **added**, **removed**, or **value-shifted** between pin and `AUDIT_HEAD`. Cross-check each against `ka9q/types.py` (current) and against the stream-critical allowlist at the top of `scripts/check_upstream_drift.py`. Write the table into `contract.md`: `| enum | member | pin value | head value | change | stream-critical? |`.

- [ ] **Step 3: sync_types dry-run**

```bash
cd /opt/git/sigmond/ka9q-python
python3 scripts/sync_types.py --ka9q-radio ~/audit/ka9q-radio --diff 2>&1 | tee -a /tmp/sync-diff.txt
```
Paste the output into `contract.md` (fenced). It must be consistent with Step 2's table — reconcile any disagreement before proceeding.

- [ ] **Step 4: Verdict**

End `contract.md` with the single verdict line (see Interfaces). `CRITICAL-CHANGE` iff any removed/value-shifted member is stream-critical **or** is used anywhere in `ka9q/` or a client repo (`grep -rn "StatusType\.<MEMBER>\|Encoding\.<MEMBER>" ka9q/ /opt/git/sigmond/{hf-timestd,wspr-recorder,psk-recorder,meteor-scatter}`).

- [ ] **Step 5: Commit**

```bash
git add docs/audit/2026-08-12-alignment/contract.md
git commit -m "audit: header contract diff pin..AUDIT_HEAD with advance verdict"
```

### Task 4: Control-surface completeness matrix

**Files:**
- Create: `docs/audit/2026-08-12-alignment/control-surface.md`

**Interfaces:**
- Consumes: clone at `AUDIT_HEAD` (Task 1).
- Produces: gap list (TLVs radiod honors that `RadiodControl` cannot send) consumed by Task 8.

- [ ] **Step 1: Extract the TLVs radiod accepts as commands**

```bash
cd ~/audit/ka9q-radio
git checkout <AUDIT_HEAD>
grep -rn "decode_radio_commands" --include='*.c' .
```
Open the file containing the function (historically `radio.c`); extract every `case <ENUM>:` label inside `decode_radio_commands`. That set = the **writable control surface**.

- [ ] **Step 2: Extract the TLVs radiod emits as status**

Locate the status-encoding function (`grep -rn "encode_radio_status\|send_radio_status" --include='*.c' .`) and extract every `encode_*(..., <ENUM>, ...)` TLV it emits. That set = the **readable surface**.

- [ ] **Step 3: Map to ka9q-python**

For each writable TLV: `grep -n "StatusType\.<NAME>" /opt/git/sigmond/ka9q-python/ka9q/control.py`. Mapped = appears in a command-encoding path (a `set_*`/`create_channel`/`tune` method). For each readable TLV: `grep -rn "StatusType\.<NAME>" ka9q/status.py ka9q/discovery.py ka9q/control.py`. Write `control-surface.md`: `| TLV | direction | RadiodControl method / decoder | status |` with status ∈ `exposed`, `gap`, `not-in-types.py` (upstream-new). Restore the clone with `git checkout main` (detached-HEAD hygiene) when done.

- [ ] **Step 4: Summarize gaps**

End with a "Gaps" section: each unmapped writable TLV with a 1-line note on what a client loses without it. Since ka9q-python is the mandatory RX888 control path (no-bypass policy), every gap is a finding regardless of current client usage.

- [ ] **Step 5: Commit**

```bash
git add docs/audit/2026-08-12-alignment/control-surface.md
git commit -m "audit: radiod control-surface completeness matrix"
```

### Task 5: Client contract matrix + bypass sweep

**Files:**
- Create: `docs/audit/2026-08-12-alignment/clients.md`

**Interfaces:**
- Produces: per-client symbol matrix (Task 11 cross-checks the generated manifest against it) and bypass findings (Task 8).

- [ ] **Step 1: Symbol matrix for the four importers**

For each of `hf-timestd`, `wspr-recorder`, `psk-recorder`, `meteor-scatter` under `/opt/git/sigmond`:

```bash
grep -rn --include='*.py' -E '^\s*(from ka9q[\w.]* import|import ka9q)' /opt/git/sigmond/<repo>
```
For every imported symbol, read the call sites (grep the symbol within that repo) and record in `clients.md`: `| client | module | symbol | how used (1 line) | load-bearing behavior |`. "Load-bearing behavior" names what would break the client if changed: payload format, timing/RTP semantics, enum values, return shape.

- [ ] **Step 2: Internals reaches**

Separate subsection listing every import not re-exported from `ka9q/__init__.py` (check against its `from .X import` lines) — known: `ka9q.control.encode_int`, `encode_double`, `encode_eol`, `CMD` in hf-timestd. For each: why the client needed it (read the call site) and what public API would replace it.

- [ ] **Step 3: Bypass sweep across ALL sigmond repos**

Policy: no client talks to radiod directly. Sweep every repo in `/opt/git/sigmond` (skip `ka9q-python`, `ka9q-radio`, `.venv`, `.git`):

```bash
cd /opt/git/sigmond
grep -rln --include='*.py' --include='*.sh' --include='*.c' --include='*.go' \
  -E '(pcmrecord|metadump|/usr/local/bin/(tune|control|monitor)\b|\btune -|\bcontrol -)' . 2>/dev/null
grep -rln --include='*.py' -E 'socket\.(socket|sendto).*5006|status\.local|SOCK_DGRAM' . 2>/dev/null
```
Then read each hit in context — a grep hit is a lead, not a finding. Special attention: `hfdl-recorder` and `codar-sounder` (zero `ka9q` imports yet presumably consume radiod feeds — determine *how*: RTP-only consumption without control is compliant; direct control is a bypass). For each true bypass record: repo, file:line, mechanism, and **which ka9q-python capability gap motivated it**.

- [ ] **Step 4: Commit**

```bash
git add docs/audit/2026-08-12-alignment/clients.md
git commit -m "audit: client contract matrix and no-bypass policy sweep"
```

### Task 6: Idempotency code audit

**Files:**
- Create: `docs/audit/2026-08-12-alignment/idempotency.md`

**Interfaces:**
- Produces: per-question verdicts, each classed `verified-by-code`, `needs-empirical` (drives Task 7), or `defect` (drives Task 8).

- [ ] **Step 1: Answer the five spec questions from source, with file:line evidence**

1. **create_channel convergence** — read `ka9q/control.py:1275` (`create_channel`): what happens when the SSRC already exists on radiod? Is the outcome state-identical to first creation (all params re-sent atomically) or partial?
2. **allocate_ssrc determinism** — read `allocate_ssrc` in `ka9q/control.py` and `ka9q/addressing.py`: same inputs ⇒ same SSRC across processes/restarts? Collision behavior?
3. **Recovery equivalence** — read `ka9q/managed_stream.py` + `ka9q/monitor.py`: on recreation after loss, is every original parameter re-applied (compare against the `__init__` parameter list at `managed_stream.py:117`), or only a subset?
4. **Keepalive preservation** — read the keepalive path (`ka9q/control.py`, see `set_channel_lifetime:1960` and the fix in commit `731ce5e`): does any keepalive/refresh path send fewer parameters than creation did? List every field it preserves vs. omits.
5. **Resequencer continuity** — read `ka9q/resequencer.py`: under what loss/reorder conditions does delivered payload differ between two identical runs (gap-fill determinism)?

For each: verdict + class + evidence (`file:line` plus a quoted snippet).

- [ ] **Step 2: Sibling-bug scan for the `731ce5e` class**

That bug: a maintenance path silently dropped a creation-time setting (encoding). Enumerate every code path that re-sends channel state (keepalive, recovery, retune, `tune`) and diff its parameter set against `create_channel`'s. Any path sending a strict subset without justification → `defect` or `needs-empirical`.

- [ ] **Step 3: Commit**

```bash
git add docs/audit/2026-08-12-alignment/idempotency.md
git commit -m "audit: idempotency code audit with per-question verdicts"
```

### Task 7: Empirical idempotency verification on b1

**Files:**
- Create: `docs/audit/2026-08-12-alignment/probes/probe_create_twice.py`
- Create: `docs/audit/2026-08-12-alignment/probes/probe_keepalive_settings.py`
- Create: `docs/audit/2026-08-12-alignment/probes/probe_recovery_equivalence.py`
- Modify: `docs/audit/2026-08-12-alignment/idempotency.md` (append "Empirical results")

**Interfaces:**
- Consumes: Task 6's `needs-empirical` list (run at minimum the three probes below; add probes if Task 6 flags more).
- Produces: pass/fail evidence per probe for Task 8.

- [ ] **Step 1: Write the probes**

`probe_create_twice.py`:
```python
"""Does create_channel converge when the channel already exists? (b1 only)"""
import sys
from ka9q import RadiodControl

HOST, SSRC, FREQ = "bee1-status.local", 3999900001, 7_040_000.0
FIELDS = ("frequency", "sample_rate", "preset", "encoding")

def snap(info):
    return {f: getattr(info, f, None) for f in FIELDS}

c = RadiodControl(HOST)
try:
    c.create_channel(FREQ, preset="usb", sample_rate=12000, ssrc=SSRC)
    first = snap(c.poll_channel(SSRC, expected_freq=FREQ, timeout=5.0))
    c.create_channel(FREQ, preset="usb", sample_rate=12000, ssrc=SSRC)
    second = snap(c.poll_channel(SSRC, expected_freq=FREQ, timeout=5.0))
    print("first: ", first)
    print("second:", second)
    print("CONVERGES" if first == second else "DIVERGES")
    sys.exit(0 if first == second else 1)
finally:
    try:
        c.remove_channel(SSRC)
    finally:
        c.close()
```

`probe_keepalive_settings.py`:
```python
"""Does a keepalive-refreshed channel keep its creation-time encoding? (b1 only)"""
import sys, time
from ka9q import RadiodControl, Encoding

HOST, SSRC, FREQ = "bee1-status.local", 3999900002, 7_040_000.0

c = RadiodControl(HOST)
try:
    c.create_channel(FREQ, preset="usb", sample_rate=12000, ssrc=SSRC,
                     encoding=Encoding.F32LE, lifetime=20)
    before = c.poll_channel(SSRC, expected_freq=FREQ, timeout=5.0)
    time.sleep(30)  # > one keepalive interval for lifetime=20
    after = c.poll_channel(SSRC, expected_freq=FREQ, timeout=5.0)
    print("encoding before/after:", before.encoding, after.encoding)
    ok = after is not None and after.encoding == Encoding.F32LE
    print("PRESERVED" if ok else "LOST")
    sys.exit(0 if ok else 1)
finally:
    try:
        c.remove_channel(SSRC)
    finally:
        c.close()
```
(If `ChannelInfo` lacks an `encoding` attribute, check `ka9q/discovery.py` for the actual field name and adjust — record the actual name used in the results section.)

`probe_recovery_equivalence.py`:
```python
"""After ManagedStream recovery, is channel state identical? Simulates loss by
removing the channel out from under the stream. (b1 only)"""
import sys, time
from ka9q import RadiodControl
from ka9q.managed_stream import ManagedStream

HOST, SSRC, FREQ = "bee1-status.local", 3999900003, 7_040_000.0
FIELDS = ("frequency", "sample_rate", "preset", "encoding")

def snap(info):
    return {f: getattr(info, f, None) for f in FIELDS}

c = RadiodControl(HOST)
saboteur = RadiodControl(HOST)
stream = None
try:
    stream = ManagedStream(c, FREQ, preset="usb", sample_rate=12000,
                           on_samples=lambda *a, **k: None)
    stream.start()
    time.sleep(3)
    ssrc = stream.ssrc  # ManagedStream allocates; read actual attribute
    before = snap(c.poll_channel(ssrc, expected_freq=FREQ, timeout=5.0))
    saboteur.remove_channel(ssrc)          # simulate radiod losing the channel
    time.sleep(15)                          # allow monitor to detect + recreate
    after = snap(c.poll_channel(ssrc, expected_freq=FREQ, timeout=5.0))
    print("before:", before)
    print("after: ", after)
    print("EQUIVALENT" if before == after else "DIVERGED")
    sys.exit(0 if before == after else 1)
finally:
    if stream is not None:
        stream.stop()
    for ctl in (saboteur, c):
        try:
            ctl.close()
        except Exception:
            pass
```
(Verify the actual attribute for the stream's SSRC — `stream.ssrc` — and the stop method name against `ka9q/managed_stream.py` before running; adjust and note.)

- [ ] **Step 2: Run each probe against b1**

```bash
cd /opt/git/sigmond/ka9q-python
uv run python docs/audit/2026-08-12-alignment/probes/probe_create_twice.py; echo "exit=$?"
uv run python docs/audit/2026-08-12-alignment/probes/probe_keepalive_settings.py; echo "exit=$?"
uv run python docs/audit/2026-08-12-alignment/probes/probe_recovery_equivalence.py; echo "exit=$?"
```
Record full stdout + exit codes. If b1 is unreachable, use `bee2-status.local`; if both unreachable, record that and mark the probes blocked — do not fake results.

- [ ] **Step 3: Verify no leftovers on b1**

```bash
uv run python - <<'EOF'
from ka9q import discover_channels
for ch in discover_channels("bee1-status.local"):
    if 3999900000 <= ch.ssrc <= 3999900999:
        print("LEFTOVER:", ch.ssrc)
EOF
```
Expected: no output. If leftovers exist, remove them with `RadiodControl.remove_channel` and note the teardown bug as a finding.

- [ ] **Step 4: Append results to `idempotency.md` and commit**

```bash
git add docs/audit/2026-08-12-alignment/
git commit -m "audit: empirical idempotency probes and results from b1"
```

### Task 8: Ranked findings + report assembly

**Files:**
- Create: `docs/audit/2026-08-12-alignment/findings.md`
- Create: `docs/audit/2026-08-12-alignment/README.md`

**Interfaces:**
- Consumes: all of Tasks 1–7.
- Produces: the ranked findings list Checkpoint A reviews; each finding has an ID (`F1`, `F2`, …) the follow-up plan will reference.

- [ ] **Step 1: Write `findings.md`**

Every finding from Tasks 2–7 (capability gaps, bypasses, internals reaches, idempotency defects, doc drift), ranked: **P0** breaks a client today; **P1** violates a spec guarantee (idempotency, no-bypass, mandatory-path completeness); **P2** capability gap, no current consumer; **P3** doc/hygiene. Format per finding: ID, priority, one-sentence claim, evidence link (report file + section), proposed remediation (1–2 lines).

- [ ] **Step 2: Write `README.md`** — index of the report files with a 5–10 line executive summary (drift verdict, counts per finding priority, whether the pin is safe to advance).

- [ ] **Step 3: Publish the report as an artifact page** (single HTML assembled from the report files) for comfortable reading; note the URL in `README.md`.

- [ ] **Step 4: Commit**

```bash
git add docs/audit/2026-08-12-alignment/
git commit -m "audit: ranked findings and report index"
```

### CHECKPOINT A — user review (hard gate)

Present `findings.md` to the user. Finding-driven remediation beyond Tasks 9–10 is **not in this plan** — write a follow-up plan from the approved findings. Expected follow-up scope (spec commitments, do not drop): public equivalents for the internals clients reach into (`ka9q.control.encode_int`/`encode_double`/`encode_eol`/`CMD`), closure of any bypass found in Task 5 (close the capability gap, then the client can migrate), fixes for any idempotency `defect` from Tasks 6–7, and new-capability APIs for Task 4 gaps — each new-capability integration test version-probed/skip-guarded for b1/b2's possibly-older radiod. Do not proceed to Task 9 until the user has seen the findings and Task 3's verdict is `SAFE-TO-ADVANCE`.

---

## Phase 2 — Known remediation

### Task 9: Advance the pin

**Gate:** Task 3 verdict is `CONTRACT: SAFE-TO-ADVANCE` and Checkpoint A passed.

**Files:**
- Modify: `ka9q/types.py`, `ka9q_radio_compat`, `ka9q/compat.py` (regenerated together)

- [ ] **Step 1: Pin the clone to AUDIT_HEAD and apply**

```bash
cd ~/audit/ka9q-radio && git checkout <AUDIT_HEAD>
cd /opt/git/sigmond/ka9q-python
python3 scripts/sync_types.py --ka9q-radio ~/audit/ka9q-radio --apply
git diff --stat
```
Expected: the three files change; `ka9q_radio_compat` now contains `AUDIT_HEAD`.

- [ ] **Step 2: Full unit suite**

```bash
uv run pytest -q
```
Expected: all pass. A failure here means generated types broke an assumption — stop, diagnose with the superpowers:systematic-debugging skill, do not paper over.

- [ ] **Step 3: Drift watcher must now pass clean**

```bash
python3 scripts/check_upstream_drift.py --ka9q-radio ~/audit/ka9q-radio
```
Expected: pass, 0 commits ahead (or only commits newer than `AUDIT_HEAD`).

- [ ] **Step 4: Commit the trio together**

```bash
git add ka9q/types.py ka9q_radio_compat ka9q/compat.py
git commit -m "sync: advance ka9q-radio pin to <AUDIT_HEAD-short>"
```

### Task 10: CLAUDE.md corrections

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Verify current facts before editing**

```bash
grep -n "bee1-hf-status\|hfdl-recorder\|codar-sounder\|wsprdaemon" CLAUDE.md
grep -rln --include='*.py' -E '^(from ka9q|import ka9q)' /opt/git/sigmond/hfdl-recorder /opt/git/sigmond/codar-sounder /opt/git/sigmond/meteor-scatter
```
Expected: meteor-scatter hits; hfdl-recorder/codar-sounder do not (if this changed, update the edits below to match reality and say so in the commit).

- [ ] **Step 2: Edit CLAUDE.md**

- Replace every `bee1-hf-status.local` with `bee1-status.local`.
- Client roster (MultiStream rationale + drift-watcher coordination list): replace the enumeration `psk-recorder, wspr-recorder, hfdl-recorder, codar-sounder` with the audited truth from Task 5, adding `meteor-scatter` and moving repos that no longer import ka9q out of the coordination list (mention them as historical if Task 5 found they consume RTP without control).
- Add one sentence to the Protocol Notes section: "ka9q-python is the mandatory control path: no sigmond-suite client may talk to radiod directly (own sockets, hand-built TLVs, or ka9q-radio CLI tools)."

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: correct client roster and radiod status hostname; state no-bypass policy"
```

---

## Phase 3 — Guardrails

### Task 11: Client-usage manifest generator

**Files:**
- Create: `scripts/gen_client_manifest.py`
- Create: `tests/test_gen_client_manifest.py`
- Create: `tests/client_usage_manifest.json` (generated, checked in)

**Interfaces:**
- Produces: `scan_repo(path: Path) -> dict[str, list[str]]` (module → sorted symbol names) and `build_manifest(root: Path, repos: list[str] | None = None) -> dict`. Manifest JSON shape (Task 12 depends on it):

```json
{
  "root": "/opt/git/sigmond",
  "clients": {
    "hf-timestd": {"ka9q": ["RadiodControl", "SlotClock"], "ka9q.control": ["CMD", "encode_int"]}
  },
  "signatures": {"ka9q:RadiodControl": "(status_address, ...)", "ka9q.control:encode_int": "(...)"}
}
```
Signature values: `str(inspect.signature(obj))` for callables, `null` for non-callables.

- [ ] **Step 1: Write the failing test**

`tests/test_gen_client_manifest.py`:
```python
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
from gen_client_manifest import scan_repo, build_manifest


def make_client(tmp_path, name, source):
    repo = tmp_path / name
    repo.mkdir()
    (repo / "main.py").write_text(source)
    return repo


def test_scan_repo_collects_symbols_per_module(tmp_path):
    repo = make_client(tmp_path, "clientA", (
        "from ka9q import RadiodControl, MultiStream\n"
        "from ka9q.control import encode_int as ei, CMD\n"
        "import ka9q\n"
    ))
    result = scan_repo(repo)
    assert result == {
        "ka9q": ["MultiStream", "RadiodControl"],
        "ka9q.control": ["CMD", "encode_int"],
    }


def test_scan_repo_skips_venv_and_git(tmp_path):
    repo = make_client(tmp_path, "clientB", "from ka9q import SlotClock\n")
    hidden = repo / ".venv" / "lib"
    hidden.mkdir(parents=True)
    (hidden / "noise.py").write_text("from ka9q import RTPRecorder\n")
    assert scan_repo(repo) == {"ka9q": ["SlotClock"]}


def test_build_manifest_shape_and_signatures(tmp_path):
    make_client(tmp_path, "clientA", "from ka9q import RadiodControl\n")
    manifest = build_manifest(tmp_path)
    assert manifest["clients"] == {"clientA": {"ka9q": ["RadiodControl"]}}
    sig = manifest["signatures"]["ka9q:RadiodControl"]
    assert sig is None or sig.startswith("(")
    json.dumps(manifest)  # must be serializable


def test_build_manifest_is_deterministic(tmp_path):
    make_client(tmp_path, "b_client", "from ka9q import SlotClock\n")
    make_client(tmp_path, "a_client", "from ka9q import Encoding\n")
    m1 = json.dumps(build_manifest(tmp_path), sort_keys=True)
    m2 = json.dumps(build_manifest(tmp_path), sort_keys=True)
    assert m1 == m2
```

- [ ] **Step 2: Run to verify failure**

```bash
uv run pytest tests/test_gen_client_manifest.py -v
```
Expected: FAIL — `ModuleNotFoundError: No module named 'gen_client_manifest'`.

- [ ] **Step 3: Implement `scripts/gen_client_manifest.py`**

```python
#!/usr/bin/env python3
"""Generate tests/client_usage_manifest.json from sigmond-suite client repos.

Scans every repo under a root directory for `import ka9q` / `from ka9q...
import ...` statements and records, per client, which symbols it uses from
which ka9q module — plus a signature snapshot for every callable symbol.
tests/test_client_contract.py replays this manifest against the installed
ka9q package, so a breaking API change fails in this repo, naming the
affected clients, before it ships to them.

Usage:
    uv run python scripts/gen_client_manifest.py                  # default root
    uv run python scripts/gen_client_manifest.py --root /some/dir --out path.json
"""
from __future__ import annotations

import argparse
import importlib
import inspect
import json
import re
import sys
from pathlib import Path

DEFAULT_ROOT = Path("/opt/git/sigmond")
DEFAULT_OUT = Path(__file__).resolve().parent.parent / "tests" / "client_usage_manifest.json"
SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules"}
SKIP_REPOS = {"ka9q-python", "ka9q-radio"}

FROM_IMPORT = re.compile(r"^\s*from\s+(ka9q[\w.]*)\s+import\s+(.+)$")


def _symbols(clause: str) -> list[str]:
    clause = clause.split("#")[0].strip().strip("()")
    names = []
    for part in clause.split(","):
        part = part.strip()
        if not part:
            continue
        names.append(part.split(" as ")[0].strip())
    return names


def scan_repo(path: Path) -> dict[str, list[str]]:
    found: dict[str, set[str]] = {}
    for py in sorted(path.rglob("*.py")):
        if any(seg in SKIP_DIRS for seg in py.parts):
            continue
        try:
            text = py.read_text(errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            m = FROM_IMPORT.match(line)
            if m:
                found.setdefault(m.group(1), set()).update(_symbols(m.group(2)))
    return {mod: sorted(syms) for mod, syms in sorted(found.items())}


def _signature(module: str, symbol: str) -> str | None:
    try:
        obj = getattr(importlib.import_module(module), symbol)
    except Exception:
        return None
    try:
        return str(inspect.signature(obj))
    except (TypeError, ValueError):
        return None


def build_manifest(root: Path, repos: list[str] | None = None) -> dict:
    clients: dict[str, dict] = {}
    for repo in sorted(root.iterdir()):
        if not repo.is_dir() or repo.name in SKIP_REPOS or repo.name.startswith("."):
            continue
        if repos is not None and repo.name not in repos:
            continue
        usage = scan_repo(repo)
        if usage:
            clients[repo.name] = usage
    signatures: dict[str, str | None] = {}
    for usage in clients.values():
        for module, symbols in usage.items():
            for sym in symbols:
                signatures.setdefault(f"{module}:{sym}", _signature(module, sym))
    return {"root": str(root), "clients": clients,
            "signatures": dict(sorted(signatures.items()))}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args()
    manifest = build_manifest(args.root)
    args.out.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(f"wrote {args.out} ({len(manifest['clients'])} clients)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run tests to verify pass**

```bash
uv run pytest tests/test_gen_client_manifest.py -v
```
Expected: 4 passed.

- [ ] **Step 5: Generate the real manifest and cross-check**

```bash
uv run python scripts/gen_client_manifest.py
```
Cross-check `tests/client_usage_manifest.json` against Task 5's `clients.md` matrix — same clients, same symbols. Reconcile disagreements (fix the script or the report; state which).

- [ ] **Step 6: Commit**

```bash
git add scripts/gen_client_manifest.py tests/test_gen_client_manifest.py tests/client_usage_manifest.json
git commit -m "guardrail: client-usage manifest generator with checked-in manifest"
```

### Task 12: Client contract test

**Files:**
- Create: `tests/test_client_contract.py`

**Interfaces:**
- Consumes: `tests/client_usage_manifest.json` (Task 11 shape).

- [ ] **Step 1: Write the test (it must pass immediately against the real manifest — its "failing" mode is future breakage)**

```python
"""Guardrail: every symbol a sigmond-suite client imports from ka9q must
exist and keep its signature. Regenerate the manifest with
    uv run python scripts/gen_client_manifest.py
after intentional API changes — the diff shows exactly what clients see."""
import importlib
import inspect
import json
from pathlib import Path

import pytest

MANIFEST = json.loads(
    (Path(__file__).parent / "client_usage_manifest.json").read_text()
)


def _clients_using(module: str, symbol: str) -> list[str]:
    return sorted(
        name for name, usage in MANIFEST["clients"].items()
        if symbol in usage.get(module, [])
    )


def _all_usages() -> list[tuple[str, str]]:
    pairs = set()
    for usage in MANIFEST["clients"].values():
        for module, symbols in usage.items():
            for sym in symbols:
                pairs.add((module, sym))
    return sorted(pairs)


@pytest.mark.parametrize("module,symbol", _all_usages())
def test_client_symbol_exists(module, symbol):
    mod = importlib.import_module(module)
    assert hasattr(mod, symbol), (
        f"{module}.{symbol} is gone but still imported by: "
        f"{', '.join(_clients_using(module, symbol))}"
    )


@pytest.mark.parametrize(
    "key,expected", sorted(MANIFEST["signatures"].items())
)
def test_client_symbol_signature_stable(key, expected):
    if expected is None:
        pytest.skip("non-callable or signature not captured")
    module, symbol = key.split(":")
    obj = getattr(importlib.import_module(module), symbol)
    actual = str(inspect.signature(obj))
    assert actual == expected, (
        f"Signature of {module}.{symbol} changed\n"
        f"  was: {expected}\n  now: {actual}\n"
        f"  clients affected: {', '.join(_clients_using(module, symbol))}\n"
        f"  If intentional, regenerate: uv run python scripts/gen_client_manifest.py"
    )
```

- [ ] **Step 2: Run it**

```bash
uv run pytest tests/test_client_contract.py -v
```
Expected: all pass (the manifest was generated from the current API).

- [ ] **Step 3: Prove it bites**

Temporarily rename `SlotClock` in `ka9q/__init__.py` (`from .slot_clock import SlotClock as _SlotClock`), run the test, confirm it fails naming hf-timestd; revert, rerun, confirm green.

- [ ] **Step 4: Commit**

```bash
git add tests/test_client_contract.py
git commit -m "guardrail: client contract test — symbols and signatures clients rely on"
```

### Task 13: Idempotency integration tests

**Files:**
- Create: `tests/test_idempotency_integration.py`

**Interfaces:**
- Consumes: `radiod_address` session fixture from `tests/conftest.py`; probes from Task 7 (same scenarios, now permanent, following the `tests/test_integration.py` skip idiom: skip when `SKIP_INTEGRATION` is set or radiod is unreachable).

- [ ] **Step 1: Write the tests**

```python
"""Idempotency guardrails against a live radiod (b1/b2 only; never b3/b4).

Run:  uv run pytest tests/test_idempotency_integration.py --radiod-host=bee1-status.local
Skip: SKIP_INTEGRATION=1, or automatically when radiod is unreachable.
Test channels use SSRCs 3999900010-3999900019, destroyed unconditionally."""
import os
import time

import pytest

from ka9q import Encoding, RadiodControl

FREQ = 7_040_000.0
FIELDS = ("frequency", "sample_rate", "preset", "encoding")


def _snap(info):
    return {f: getattr(info, f, None) for f in FIELDS}


@pytest.fixture
def control(radiod_address):
    if os.environ.get("SKIP_INTEGRATION"):
        pytest.skip("SKIP_INTEGRATION set")
    try:
        ctl = RadiodControl(radiod_address)
    except Exception as exc:
        pytest.skip(f"radiod not reachable at {radiod_address}: {exc}")
    yield ctl
    ctl.close()


@pytest.fixture
def channel(control):
    """Yields an SSRC allocator that guarantees teardown."""
    created = []

    def make(ssrc, **kwargs):
        control.create_channel(FREQ, ssrc=ssrc, **kwargs)
        created.append(ssrc)
        return ssrc

    yield make
    for ssrc in created:
        try:
            control.remove_channel(ssrc)
        except Exception:
            pass


def test_create_twice_converges(control, channel):
    ssrc = channel(3999900010, preset="usb", sample_rate=12000)
    first = _snap(control.poll_channel(ssrc, expected_freq=FREQ, timeout=5.0))
    channel(3999900010, preset="usb", sample_rate=12000)
    second = _snap(control.poll_channel(ssrc, expected_freq=FREQ, timeout=5.0))
    assert first == second, "re-creating an existing channel must converge"


def test_keepalive_preserves_encoding(control, channel):
    ssrc = channel(3999900011, preset="usb", sample_rate=12000,
                   encoding=Encoding.F32LE, lifetime=20)
    time.sleep(30)  # cross at least one keepalive refresh
    info = control.poll_channel(ssrc, expected_freq=FREQ, timeout=5.0)
    assert info is not None, "channel vanished despite keepalive"
    assert info.encoding == Encoding.F32LE, "keepalive dropped the encoding"


def test_remove_is_idempotent(control, channel):
    ssrc = channel(3999900012, preset="usb", sample_rate=12000)
    control.remove_channel(ssrc)
    control.remove_channel(ssrc)  # second remove must not raise
```
Adjust attribute names (`info.encoding` etc.) to whatever Task 7 recorded as the real `ChannelInfo` field names.

- [ ] **Step 2: Run against b1**

```bash
uv run pytest tests/test_idempotency_integration.py -v --radiod-host=bee1-status.local
```
Expected: 3 passed (or documented failures — a failure here is a real finding; file it, don't weaken the test).

- [ ] **Step 3: Verify clean skip without radiod**

```bash
SKIP_INTEGRATION=1 uv run pytest tests/test_idempotency_integration.py -v
```
Expected: 3 skipped. Then the full unit suite: `uv run pytest -q` — green.

- [ ] **Step 4: Commit**

```bash
git add tests/test_idempotency_integration.py
git commit -m "guardrail: idempotency integration tests (create-twice, keepalive, remove)"
```

### Task 14: Document the guardrails + final verification

**Files:**
- Modify: `CLAUDE.md` (add guardrail workflow to the drift-watcher section)

- [ ] **Step 1: Add to CLAUDE.md** (after the upstream-drift-watcher section)

```markdown
### Client compatibility guardrails

- `tests/client_usage_manifest.json` — checked-in snapshot of every ka9q
  symbol each sigmond-suite client imports, with signatures. Regenerate
  after intentional API changes: `uv run python scripts/gen_client_manifest.py`
  (scans /opt/git/sigmond). Review the diff — it is exactly what clients see.
- `tests/test_client_contract.py` — fails, naming the affected clients, if
  a manifest symbol disappears or changes signature.
- `tests/test_idempotency_integration.py` — live-radiod guardrails
  (create-twice convergence, keepalive setting preservation, idempotent
  remove). Runs against `--radiod-host` (b1/b2 only), skips cleanly offline.
```

- [ ] **Step 2: Full verification**

```bash
uv run pytest -q
uv run pytest tests/test_idempotency_integration.py -v --radiod-host=bee1-status.local
python3 scripts/check_upstream_drift.py --ka9q-radio ~/audit/ka9q-radio
```
Expected: unit suite green; integration green (or documented skips); drift watcher pass.

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: guardrail workflow — manifest, contract test, idempotency integration"
```
