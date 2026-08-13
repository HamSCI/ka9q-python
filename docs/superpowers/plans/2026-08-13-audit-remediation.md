# Audit Remediation Implementation Plan (2026-08-12 Alignment Audit Follow-up)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remediate the ka9q-python findings approved at Checkpoint A of the 2026-08-12 upstream-alignment audit. Finding IDs (`F1`…`F14`) reference [docs/audit/2026-08-12-alignment/findings.md](../../audit/2026-08-12-alignment/findings.md); empirical evidence citations reference [docs/audit/2026-08-12-alignment/idempotency.md](../../audit/2026-08-12-alignment/idempotency.md). Scope: F1 (P0), F4, F5, F3+F7 (merged docstring errata), F8, F10, F11, F12, F13, F14, F6, plus final verification. F2 (pin advance) was already completed by the prior plan's Task 9 — `ka9q/types.py` already carries `IDLE_DEMOD=5` / `N_DEMOD=6`, which is why F13 is a live bug today, not a future one. F9 (ka9q-web) is a policy decision, not a code change — out of this plan.

**Architecture:** All changes are in ka9q-python (`ka9q/`, `scripts/`, `tests/`, `README.md`). One task per finding (F3+F7 merged: both are docstring errata in the same two docstrings). Order puts the P0 first, then the two one-line-fix-class items, then the behavior change with the widest test blast-radius (F5), then docs, then capability adds. Every code task is TDD: failing test first, then the exact implementation below.

**Tech Stack:** Python 3 / pytest via `uv run pytest`, git. No new dependencies.

## Global Constraints

- Work on `main` (per operator instruction). One commit per task; every commit message ends with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.
- Unit suite (`uv run pytest -q`, no radiod needed) must stay green after every commit, **except** these known environmental failures, which pre-date this plan and are not to be "fixed" here: `tests/test_integration.py` (10 failures), `tests/test_iq_20khz_f32.py` (1), `tests/test_protocol_compat.py` (2 — until the sibling `ka9q-radio` checkout is updated). Any *other* failure is a regression introduced by the task at hand — stop and fix it.
- Integration hosts: **only** `bee1-status.local` (b1) or `bee2-status.local` (b2). Never touch b3/b4 (dev/production). Default host env: `RADIOD_HOST=bee1-status.local`.
- All ephemeral live-test channels use SSRCs in **3999900000–3999900999** and are destroyed in `finally:` blocks / fixture teardown — b1/b2 must be left clean even on failure.
- Client repos (`hf-timestd`, `wspr-recorder`, `psk-recorder`, `meteor-scatter`, …) are **read-only**. Client-side findings **F15–F21 are OUT of scope** for this plan.
- **Signature-snapshot manifest:** `tests/client_usage_manifest.json` must be regenerated (`uv run python scripts/gen_client_manifest.py`) in any task that changes a **public signature** the manifest snapshots (module-level callables and class constructors — e.g. `RadiodControl.__init__`, `MultiStream.__init__`, `resolve_multicast_address`). The snapshot strings are **Python-3.11-rendering-sensitive** (known limitation: `inspect.signature` renders defaults/annotations differently across interpreter versions) — always regenerate with the project's pinned interpreter via `uv run`. No task below changes a snapshotted signature (method signatures are not snapshotted), so no regeneration is *expected* — but run `uv run pytest tests/test_client_contract.py -q` in every task's verification to prove it, and regenerate if it fails on an intentional change.
- The write-path caveat from the audit applies to this sandbox: if live commands to b1/b2 time out while `discover_channels()` works, check `ip route get <group>` for the `dev lo` route (finding F8; the Task 6 README section documents the remedy). The integration-test fixtures already force `interface=`.

---

### Task 1: F1 (P0) — MultiStream restore must re-send every creation-time setting

`MultiStream._attempt_restore()` calls `ensure_channel()` with only 5 of the 10 identity/config fields captured at `add_channel()`. `agc_enable`/`gain` feed `allocate_ssrc()`'s hash, so a restore of a channel created with non-default gain/AGC computes a **different SSRC** — silent channel-identity change on the recovery path every major client relies on. Fix mirrors the `731ce5e` lifetime/encoding pattern: store in `_ChannelSlot`, thread through restore. Two deliberate extensions of the same bug class, both in the restore call only: (a) restore re-sends `slot.requested_encoding` (what creation sent) instead of `slot.encoding` (what radiod granted) — otherwise a radiod F32→S16 downgrade at creation makes the restore hash a different SSRC too; (b) after restore, `slot.encoding` is re-resolved from the newly granted `channel_info`, the same authority rule `add_channel()` already applies (prevents NaN-poisoned decode after a restore that re-grants a different encoding). Owner's contract for this task (Checkpoint review, 2026-08-13): **the original requested channel requires full specification — restore must replay the complete original requested spec, never a partial one.**

**Files:**
- Create: `tests/test_multistream_restore.py`
- Modify: `ka9q/multi_stream.py` (`_ChannelSlot` ~line 88, `add_channel` slot construction ~line 238, `_attempt_restore` ~line 687)

- [ ] **Step 1: Write the failing tests**

`tests/test_multistream_restore.py`:

```python
"""MultiStream restore must re-send every creation-time setting (audit F1, P0).

_attempt_restore() used to call ensure_channel() with only 5 of the 10
identity/config fields captured at add_channel(); agc_enable/gain also feed
allocate_ssrc()'s hash, so a restore of a channel created with non-default
gain/AGC computed a *different SSRC* than the one being restored.  Asserted
at the RadiodControl.ensure_channel boundary, mirroring
tests/test_lifetime.py::TestMultiStreamLifetime.
"""
from unittest.mock import MagicMock

from ka9q.discovery import ChannelInfo
from ka9q.multi_stream import MultiStream

ADD_KWARGS = dict(
    frequency_hz=14_074_000.0,
    preset="usb",
    sample_rate=12000,
    encoding=4,          # F32LE requested
    agc_enable=1,
    gain=-10.0,
    low_edge=-250.0,
    high_edge=+250.0,
    kaiser_beta=11.0,
    lifetime=6000,
)


def _make_multi(ssrc=12345, granted_encoding=4):
    control = MagicMock()
    control.ensure_channel.return_value = ChannelInfo(
        ssrc=ssrc,
        preset="usb",
        sample_rate=12000,
        frequency=14_074_000.0,
        snr=0.0,
        multicast_address="239.1.2.3",
        port=5004,
        encoding=granted_encoding,
    )
    return MultiStream(control=control), control


def test_slot_stores_all_creation_settings():
    multi, _ = _make_multi()
    multi.add_channel(**ADD_KWARGS)
    slot = multi._slots[12345]
    assert slot.agc_enable == 1
    assert slot.gain == -10.0
    assert slot.low_edge == -250.0
    assert slot.high_edge == +250.0
    assert slot.kaiser_beta == 11.0
    assert slot.requested_encoding == 4
    assert slot.lifetime == 6000


def test_restore_resends_every_field_creation_sent():
    multi, control = _make_multi()
    multi.add_channel(**ADD_KWARGS)
    add_call = dict(control.ensure_channel.call_args.kwargs)
    add_call.pop("timeout")            # restore has no ACK-wait override
    slot = multi._slots[12345]
    slot.dropped = True
    control.ensure_channel.reset_mock()

    multi._attempt_restore(12345, slot)

    restore_call = dict(control.ensure_channel.call_args.kwargs)
    assert restore_call == add_call, (
        "restore must re-send exactly what add_channel sent "
        f"(symmetric difference of keys: {set(add_call) ^ set(restore_call)})"
    )


def test_restore_resends_requested_not_granted_encoding():
    # radiod downgraded F32(4) -> S16(1) at creation; the restore must
    # re-request 4 (what creation requested) so allocate_ssrc() computes
    # the same SSRC it computed at creation.
    multi, control = _make_multi(granted_encoding=1)
    multi.add_channel(**ADD_KWARGS)
    slot = multi._slots[12345]
    assert slot.encoding == 1              # granted encoding drives parsing
    slot.dropped = True
    control.ensure_channel.reset_mock()

    multi._attempt_restore(12345, slot)

    assert control.ensure_channel.call_args.kwargs["encoding"] == 4
    # and the slot keeps decoding with the (re-)granted wire encoding
    assert slot.encoding == 1
```

- [ ] **Step 2: Run to verify failure**

```bash
uv run pytest tests/test_multistream_restore.py -v
```
Expected: `test_slot_stores_all_creation_settings` fails with `AttributeError: '_ChannelSlot' object has no attribute 'agc_enable'`; the other two fail on kwargs mismatch.

- [ ] **Step 3: Implement**

In `ka9q/multi_stream.py`, `_ChannelSlot` (after the existing `lifetime: Optional[int] = None` field, ~line 88), add:

```python
    # Creation-time settings that _attempt_restore must re-send in full.
    # Same bug class as 731ce5e (lifetime/encoding): any field accepted by
    # add_channel() but missing here silently reverts on restore — and
    # agc_enable/gain also feed allocate_ssrc()'s hash, so dropping them
    # made a restore land on a *different SSRC* (audit finding F1, P0).
    agc_enable: int = 0
    gain: float = 0.0
    low_edge: Optional[float] = None
    high_edge: Optional[float] = None
    kaiser_beta: Optional[float] = None
```

In `add_channel()`'s `_ChannelSlot(...)` construction (~line 238), add after `lifetime=lifetime,`:

```python
            agc_enable=agc_enable,
            gain=gain,
            low_edge=low_edge,
            high_edge=high_edge,
            kaiser_beta=kaiser_beta,
```

In `_attempt_restore()` (~line 689), replace the `ensure_channel` call:

```python
            channel_info = self._control.ensure_channel(
                frequency_hz=slot.frequency_hz,
                preset=slot.preset,
                sample_rate=slot.sample_rate,
                agc_enable=slot.agc_enable,
                gain=slot.gain,
                encoding=slot.requested_encoding,
                low_edge=slot.low_edge,
                high_edge=slot.high_edge,
                kaiser_beta=slot.kaiser_beta,
                lifetime=slot.lifetime,
            )
```

and immediately after the existing `slot.channel_info = channel_info` line add:

```python
            # Re-resolve the granted wire encoding (radiod may re-grant a
            # different encoding than requested — same authority rule as
            # add_channel(); parsing with a stale dtype NaN-poisons samples).
            slot.encoding = (
                getattr(channel_info, 'encoding', 0) or slot.requested_encoding
            )
```

- [ ] **Step 4: Verify**

```bash
uv run pytest tests/test_multistream_restore.py tests/test_lifetime.py tests/test_multistream_prune.py tests/test_multistream_gap_storm.py tests/test_filter_edges.py -v
uv run pytest tests/test_client_contract.py -q
```
Expected: all pass (existing MultiStream tests construct slots by keyword, so the new defaulted fields are backward-compatible).

- [ ] **Step 5: Commit**

```bash
git add ka9q/multi_stream.py tests/test_multistream_restore.py
git commit -m "fix(multi_stream): restore re-sends agc/gain/filter-edges/requested-encoding (audit F1, P0)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: F4 — tune() records the requested encoding

`create_channel()` and `set_output_encoding()` write `self._requested_encoding[ssrc]`; `set_channel_lifetime()`'s keepalive re-assertion and `verify_channel()`'s default expectation read it. `tune(ssrc, encoding=X)` sends the `OUTPUT_ENCODING` TLV but never records it, so a channel last retuned via `tune()` gets the *wrong* encoding re-asserted by every subsequent keepalive. The write goes at TLV-encode time (not after the ACK wait) so the request is recorded even when the status response times out — the command was still transmitted (possibly multiple times), and this is also what makes the fix unit-testable with the existing abort-after-first-send pattern.

**Files:**
- Modify: `ka9q/control.py` (`tune()`, ~line 2195)
- Modify: `tests/test_keepalive_encoding.py` (append test class)

- [ ] **Step 1: Write the failing tests** — append to `tests/test_keepalive_encoding.py`:

```python
class TestTuneRecordsRequestedEncoding:
    """tune(encoding=X) must write _requested_encoding[ssrc] (audit F4) so
    the keepalive re-assertion and verify_channel()'s default expectation
    track channels retuned via tune(), not only create_channel()/
    set_output_encoding().  Same bug class as 731ce5e."""

    def _abort_after_first_send(self, control, sent):
        def side_effect(buf):
            sent.append(bytes(buf))
            raise TimeoutError("aborted after first send (unit test)")
        control.send_command = MagicMock(side_effect=side_effect)
        control._get_or_create_status_listener = MagicMock(
            return_value=MagicMock())

    def test_tune_encoding_is_remembered(self):
        c = _bare_control()
        sent = []
        self._abort_after_first_send(c, sent)
        with pytest.raises(TimeoutError):
            c.tune(ssrc=555, frequency_hz=7_040_000.0,
                   encoding=Encoding.F32LE, timeout=0.05)
        assert c._requested_encoding[555] == Encoding.F32LE

    def test_tune_without_encoding_leaves_record_alone(self):
        c = _bare_control()
        c._requested_encoding[555] = Encoding.F32LE
        sent = []
        self._abort_after_first_send(c, sent)
        with pytest.raises(TimeoutError):
            c.tune(ssrc=555, frequency_hz=7_040_000.0, timeout=0.05)
        assert c._requested_encoding[555] == Encoding.F32LE
```

- [ ] **Step 2: Run to verify failure**

```bash
uv run pytest tests/test_keepalive_encoding.py -v -k TuneRecords
```
Expected: `test_tune_encoding_is_remembered` fails with `KeyError: 555`; the other passes.

- [ ] **Step 3: Implement** — in `tune()` (~line 2195), replace:

```python
        if encoding is not None:
            encode_int(cmdbuffer, StatusType.OUTPUT_ENCODING, encoding)
```

with:

```python
        if encoding is not None:
            encode_int(cmdbuffer, StatusType.OUTPUT_ENCODING, encoding)
            # F4: record the request so set_channel_lifetime()'s keepalive
            # re-assertion and verify_channel()'s default expectation track
            # channels retuned via tune() too (same bug class as 731ce5e).
            self._requested_encoding[ssrc] = encoding
```

- [ ] **Step 4: Verify**

```bash
uv run pytest tests/test_keepalive_encoding.py tests/test_tune_method.py tests/test_lifetime.py -v
uv run pytest tests/test_client_contract.py -q
```
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add ka9q/control.py tests/test_keepalive_encoding.py
git commit -m "fix(control): tune() records _requested_encoding so keepalives re-assert it (audit F4)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: F5 — derive per-client destination; fail loudly only when underivable

Empirically proven (idempotency.md Round 2, "Second requirement found"): on both b1 and b2, a create command without an `OUTPUT_DATA_DEST_SOCKET` TLV produces **no error and no channel** — the docstring's "uses radiod's config-file default" fallback does not exist client-side, and the only symptom is a later timeout that doesn't name the cause.

**Owner's ruling (Checkpoint review, 2026-08-13):** the SUPPORTED model is a per-client unique destination derived by ka9q-python ("destination unique for a client among others") — exactly what `ensure_channel` already does via `generate_multicast_ip(unique_id=self.client_id, radiod_host=self.status_address)` (control.py:1771–1778). Explicit `destination=` is the DEPRECATED/legacy path: it keeps working and wins over derivation (operator override), but is documented as legacy. Therefore: `create_channel` gains the **same** derivation, extracted into a shared `_derive_client_destination()` helper so the two paths cannot drift; a create command is **never** sent without a destination TLV; and `ValidationError` is raised only when *neither* `client_id` nor `destination=` is available. **No runtime `DeprecationWarning` for explicit `destination=`** — hf-timestd passes resolved destinations on two production paths (`channel_manager.py:137`, `stream_manager.py:138`) and must not be warning-spammed; the deprecation lives in docstrings only. Recorded here so the decision is on the record.

**Caller audit (performed 2026-08-13, updated for the derive-first model):**
- `ensure_channel()` (control.py:1867) is the **only** in-library caller of `create_channel()`. It keeps its derivation (now via the shared helper) and raises the same-shaped `ValidationError` when underivable, so `create_channel`'s guard is unreachable from that path.
- `ManagedStream` forwards its `destination` parameter (managed_stream.py:237, 420); when it is `None`, derivation from the control's `client_id` applies — callers with neither now fail fast at `start()` instead of silently creating nothing.
- `MultiStream.add_channel` (multi_stream.py:199) has **no destination parameter** — MultiStream therefore requires a `client_id`-bearing `RadiodControl` (the supported model). Already production reality: hf-timestd, wspr-recorder and psk-recorder all construct `RadiodControl(..., client_id=...)` (verified by grep in the read-only client checkouts). Documented in the docstring below.
- `ChannelMonitor` (monitor.py:90, 147) forwards `**kwargs` — same inheritance as ManagedStream.

**Behavior note:** for a `client_id`-bearing control calling `create_channel()` directly with no `destination=` and no `ssrc=`, the derived destination now participates in `allocate_ssrc()`'s hash (previously `None` did) — this *aligns* direct-create SSRCs with `ensure_channel`'s (which always derived before allocating), and the audit found no client on that direct-create-no-destination path.

**Files:**
- Create: `tests/test_destination_required.py`
- Modify: `ka9q/control.py` (new `_derive_client_destination()`; `create_channel` guard + docstring; `ensure_channel` guard + docstring; `RadiodControl.__init__` docstring)
- Modify: `ka9q/multi_stream.py` (`add_channel` docstring note)
- Modify: `ka9q/__init__.py` (module docstring example)
- Modify: `tests/test_create_split_encoding.py`, `tests/test_lifetime.py`, `tests/test_remove_channel.py`, `tests/test_filter_edges.py`, `tests/test_ensure_channel_encoding.py` (call-site updates, see Step 5)
- Modify: `examples/*.py` (add `client_id=` at constructions; not suite-gating)

- [ ] **Step 1: Write the failing tests**

`tests/test_destination_required.py`:

```python
"""Destination resolution for channel creation (audit F5, owner-ruled model).

Empirically (idempotency.md Round 2, "Second requirement found") an omitted
OUTPUT_DATA_DEST_SOCKET TLV silently creates nothing.  The supported model
is a per-client destination derived from client_id (same derivation in
create_channel and ensure_channel via _derive_client_destination());
explicit destination= is the legacy path and keeps working.  Creation
fails loudly only when NEITHER is available."""

import threading
import time
from unittest.mock import MagicMock

import pytest

from ka9q.addressing import generate_multicast_ip
from ka9q.control import RadiodControl, StatusType
from ka9q.exceptions import ValidationError


def _bare_control(client_id=None) -> RadiodControl:
    c = RadiodControl.__new__(RadiodControl)
    c.status_address = "test.local"
    c.socket = MagicMock()
    c.dest_addr = ("239.1.2.3", 5006)
    c._socket_lock = threading.RLock()
    c.max_commands_per_sec = 100
    c._command_count = 0
    c._command_window_start = time.time()
    c._rate_limit_lock = threading.Lock()
    c.metrics = MagicMock()
    c._requested_encoding = {}
    c.client_id = client_id
    return c


def _capture_send(control):
    sent = []
    control.send_command = MagicMock(
        side_effect=lambda buf: sent.append(bytes(buf)))
    return sent


def _has_tag(buf: bytes, tag: int) -> bool:
    """TLV walker (same as tests/test_filter_edges.py)."""
    cp = 1
    while cp < len(buf):
        t = buf[cp]
        cp += 1
        if t == StatusType.EOL:
            break
        if cp >= len(buf):
            break
        optlen = buf[cp]
        cp += 1
        if optlen & 0x80:
            n = optlen & 0x7F
            optlen = 0
            for _ in range(n):
                if cp >= len(buf):
                    return False
                optlen = (optlen << 8) | buf[cp]
                cp += 1
        if t == tag:
            return True
        cp += optlen
    return False


def test_create_channel_underivable_raises():
    c = _bare_control(client_id=None)
    c.send_command = MagicMock()
    with pytest.raises(ValidationError, match="destination"):
        c.create_channel(frequency_hz=14_074_000.0, ssrc=12345)
    c.send_command.assert_not_called()   # fail BEFORE any bytes hit the wire


def test_create_channel_derives_destination_from_client_id():
    c = _bare_control(client_id="unit-test")
    sent = _capture_send(c)
    c.create_channel(frequency_hz=14_074_000.0, ssrc=12345)
    assert sent, "create_channel sent nothing"
    assert _has_tag(sent[0], StatusType.OUTPUT_DATA_DEST_SOCKET), (
        "derived destination must be encoded — radiod silently creates "
        "nothing without the destination TLV")


def test_explicit_destination_still_works_legacy():
    c = _bare_control(client_id=None)
    sent = _capture_send(c)
    c.create_channel(frequency_hz=14_074_000.0, ssrc=12345,
                     destination="239.9.8.7")
    assert _has_tag(sent[0], StatusType.OUTPUT_DATA_DEST_SOCKET)


def test_create_and_ensure_derive_identical_destination():
    """Locks the shared-helper invariant: create_channel and ensure_channel
    must derive the SAME per-(client, radiod) group, so the two paths can
    never drift apart again."""
    expected = generate_multicast_ip(unique_id="unit-test",
                                     radiod_host="test.local")
    c = _bare_control(client_id="unit-test")
    assert c._derive_client_destination() == expected


def test_ensure_channel_underivable_raises():
    c = _bare_control(client_id=None)
    with pytest.raises(ValidationError, match="destination"):
        c.ensure_channel(frequency_hz=14_074_000.0, preset="iq",
                         sample_rate=16000)


def test_ensure_channel_derives_and_reuses_matching_channel():
    # The derived destination must reach the reuse comparison: an existing
    # channel on exactly the derived group is reused, create_channel is
    # never called, and the guard does not fire.
    from ka9q.discovery import ChannelInfo
    derived = generate_multicast_ip(unique_id="unit-test",
                                    radiod_host="test.local")
    c = _bare_control(client_id="unit-test")
    c.create_channel = MagicMock()
    ch = ChannelInfo(ssrc=1, preset="iq", sample_rate=16000,
                     frequency=14_074_000.0, snr=0.0,
                     multicast_address=derived, port=5004)

    def poll(ssrc, *a, **k):
        ch.ssrc = ssrc
        return ch

    c.poll_channel = MagicMock(side_effect=poll)
    c.ensure_channel(frequency_hz=14_074_000.0, preset="iq",
                     sample_rate=16000)  # must not raise
    c.create_channel.assert_not_called()
```

- [ ] **Step 2: Run to verify failure**

```bash
uv run pytest tests/test_destination_required.py -v
```
Expected: `test_create_channel_underivable_raises`, `test_create_channel_derives_destination_from_client_id`, `test_create_and_ensure_derive_identical_destination` (`AttributeError: ... no attribute '_derive_client_destination'`) and `test_ensure_channel_underivable_raises` fail; the legacy-path and reuse tests pass.

- [ ] **Step 3: Implement the shared helper and both guards**

Add to `RadiodControl` immediately before `create_channel()` (~line 1275):

```python
    def _derive_client_destination(self) -> Optional[str]:
        """Derive the per-client destination multicast group, or None.

        CONTRACT v0.3 §7: when this RadiodControl was constructed with a
        client_id, channel destinations default to a deterministic
        per-(client, radiod) multicast group — unique for this client
        among others on the station — so peer clients land on distinct
        groups without per-client derivation code.  Shared by
        create_channel() and ensure_channel() so the two derivations can
        never drift apart (audit finding F5, owner ruling 2026-08-13).
        """
        if not self.client_id:
            return None
        from .addressing import generate_multicast_ip
        return generate_multicast_ip(
            unique_id=self.client_id,
            radiod_host=self.status_address,
        )
```

`create_channel()` — insert as the **first** statements of the method body (before SSRC auto-allocation, so the resolved destination participates in the SSRC hash, matching ensure_channel):

```python
        # Destination resolution (audit F5): explicit destination= (legacy)
        # wins; otherwise derive the per-client group from client_id
        # (supported model); otherwise fail loudly — radiod silently creates
        # NO channel when the OUTPUT_DATA_DEST_SOCKET TLV is omitted (no
        # error, no visible SSRC, only a later poll/ensure timeout).  A
        # create command is never sent without a destination TLV.
        if destination is None:
            destination = self._derive_client_destination()
            if destination is not None:
                logger.debug(
                    "create_channel: derived destination=%s for client_id=%r "
                    "radiod=%r", destination, self.client_id,
                    self.status_address,
                )
        if destination is None:
            raise ValidationError(
                "create_channel() could not resolve a destination, and radiod "
                "silently creates NO channel when the OUTPUT_DATA_DEST_SOCKET "
                "TLV is omitted (audit finding F5). Construct "
                "RadiodControl(..., client_id=...) so a deterministic "
                "per-client destination is derived (supported model), or pass "
                "destination='239.x.y.z[:port]' explicitly (legacy)."
            )
```

`ensure_channel()` — replace the existing derivation block (the `# CONTRACT v0.3 §7:` comment plus the `if destination is None and self.client_id:` block, ~lines 1765–1779) with:

```python
        # Destination resolution (audit F5): explicit destination= (legacy)
        # wins; otherwise derive the per-client group (supported model,
        # shared with create_channel); otherwise fail loudly.
        if destination is None:
            destination = self._derive_client_destination()
            if destination is not None:
                logger.debug(
                    "ensure_channel: derived destination=%s for client_id=%r "
                    "radiod=%r", destination, self.client_id,
                    self.status_address,
                )
        if destination is None:
            raise ValidationError(
                "ensure_channel() could not resolve a destination: construct "
                "RadiodControl(..., client_id=...) so a deterministic "
                "per-client destination is derived (supported model), or pass "
                "destination= explicitly (legacy). Omitting both silently "
                "creates no channel on radiod deployments without a "
                "config-file default (audit finding F5)."
            )
```

and remove the now-unused `from .addressing import generate_multicast_ip` at the top of `ensure_channel`'s body (~line 1755) — the helper does its own import; the adjacent `from .discovery import ChannelInfo, discover_channels` line stays.

- [ ] **Step 4: Update the docstrings**

`create_channel()` docstring, `destination:` argument (~lines 1307–1309) — replace with:

```
            destination: RTP destination multicast address.
                        SUPPORTED MODEL: omit this and construct
                        RadiodControl with client_id= — ka9q-python derives
                        a deterministic per-(client, radiod) multicast
                        group (unique for this client among others),
                        identical to ensure_channel's derivation.
                        LEGACY: passing destination= explicitly still works
                        and wins over derivation (operator override).
                        Format "address" or "address:port" (e.g.
                        "239.1.2.3", "wspr.local", "239.1.2.3:5004"); must
                        name a group this radiod is configured to output
                        on. When neither client_id nor destination is
                        available, ValidationError is raised: the
                        previously documented "radiod config-file default"
                        fallback does not exist client-side — on audited
                        deployments an omitted destination silently created
                        nothing (audit finding F5). A create command is
                        never sent without a destination TLV.
```

`ensure_channel()` docstring, `destination:` argument (~lines 1691–1701) — replace with:

```
            destination: RTP destination multicast address (optional).
                        Precedence: (1) explicit ``destination=`` wins
                        (LEGACY path / operator override); (2) otherwise if
                        the RadiodControl was constructed with
                        ``client_id=``, a deterministic per-(client,
                        radiod) ``239.x.y.z`` address is derived — the
                        SUPPORTED model, giving each peer client on a
                        station its own multicast group (same derivation as
                        create_channel: _derive_client_destination());
                        (3) otherwise ValidationError is raised — there is
                        no radiod config-default fallback client-side
                        (audit finding F5). The resolved address becomes
                        part of the channel identity (SSRC).
```

`RadiodControl.__init__()` docstring, `client_id:` argument (~line 863) — append to its existing text:

```
                      client_id is the SUPPORTED way to obtain channel
                      destinations: create_channel()/ensure_channel()
                      derive a deterministic per-(client, radiod) multicast
                      group from it whenever destination= is omitted;
                      passing destination= explicitly is the legacy path
                      (audit finding F5).
```

`MultiStream.add_channel()` docstring — append this paragraph (before the `Returns:` line):

```
        Because add_channel has no ``destination=`` parameter, the
        RadiodControl this MultiStream wraps MUST be constructed with
        ``client_id=`` so a per-client destination is derived (the
        supported model); otherwise ensure_channel raises ValidationError
        (audit finding F5 — an omitted destination used to silently
        create nothing).
```

`ka9q/__init__.py` module docstring, "Lower-level usage" example (~line 51): change `RadiodControl("radiod.local")` to `RadiodControl("radiod.local", client_id="my-app")` (the `create_channel` call then derives its destination — the supported model). Do the same in `create_channel`'s own two docstring examples (~lines 1343 and 1352: `RadiodControl("radiod.local")` → `RadiodControl("radiod.local", client_id="my-app")`).

Deliberately **no** `DeprecationWarning` on explicit `destination=`: hf-timestd passes resolved destinations in production (`channel_manager.py:137`, `stream_manager.py:138`) and must not be warning-spammed. Docstring-only deprecation, per owner ruling.

- [ ] **Step 5: Update the unit tests whose bare controls can now neither derive nor pass a destination**

Prefer the supported model (give the test control a `client_id`) except where a test's assertions depend on a specific address (there, pass explicit `destination=` — legacy still works):

- `tests/test_lifetime.py` — `_bare_control()` helper (~line 27): add `c.client_id = "unit-test"` (the two `create_channel` calls in `TestCreateChannelLifetime` then derive; the added `OUTPUT_DATA_DEST_SOCKET` TLV does not affect the `_has_lifetime_tag` walker assertions).
- `tests/test_remove_channel.py` — the inline bare control in `test_create_and_remove_pattern` (~line 121): add `control.client_id = "unit-test"`.
- `tests/test_create_split_encoding.py` — construction (~line 10): change to `RadiodControl("radiod.local", client_id="unit-test")` (send-count assertions are unaffected: the derived destination rides in the main creation packet).
- `tests/test_filter_edges.py` — `_bare_control()` (~line 41): change `c.client_id = None` to `c.client_id = "unit-test"` (the four `create_channel` tests then derive). The three `ensure_channel` tests (~lines 154, 187, 220) mock an existing channel at `"239.1.2.3"`, so derivation would not match it: add explicit `destination="239.1.2.3",` to those three calls (legacy override wins), and change any `allocate_ssrc(..., destination=None, ...)` precompute inside those same tests to `destination="239.1.2.3"` so the precomputed SSRC stays consistent.
- `tests/test_ensure_channel_encoding.py` — both `ensure_channel(...)` calls (~lines 35, 66): add `destination="239.1.1.1",` (matches the mocked `ChannelInfo.multicast_address`; `allocate_ssrc` is patched, so the SSRC is unaffected).

(`tests/test_channel_verification.py` and `tests/test_idempotency_integration.py` already pass `destination=` explicitly — legacy path, keeps working; no change. Existing tests that already pass `destination=` anywhere else likewise KEEP it.)

- [ ] **Step 6: Update examples to the supported model (docs-level; not suite-gating)**

Add `client_id="<example-name>"` to each `RadiodControl(...)` construction (the `create_channel` calls then derive their destinations): `examples/simple_am_radio.py`, `examples/codar_oceanography.py`, `examples/superdarn_recorder.py`, `examples/channel_cleanup_example.py`, `examples/hf_band_scanner.py`, `examples/test_improvements.py`. In `examples/test_improvements.py` the intentional-ValidationError calls (invalid ssrc/frequency) keep raising their intended errors — destination derivation succeeds first, then ssrc/frequency validation fires.

- [ ] **Step 7: Verify**

```bash
uv run pytest tests/test_destination_required.py -v
uv run pytest -q
uv run pytest tests/test_client_contract.py -q
```
Expected: new tests pass; full suite green minus the known environmental failures (Global Constraints); contract test green — the `create_channel`/`ensure_channel`/`__init__` **signatures are unchanged** (defaults stay `None`; resolution happens at runtime), so no manifest regeneration.

- [ ] **Step 8: Commit**

```bash
git add ka9q/control.py ka9q/multi_stream.py ka9q/__init__.py tests/ examples/
git commit -m "fix(control): derive per-client destination in create_channel; fail loudly only when underivable (audit F5)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: F3 + F7 — docstring errata: re-create semantics, poll-does-not-extend-lifetime, frame units

Two documented claims are false (both verified live in Task 7): (F3) `create_channel()` on an existing SSRC is a **delta-update**, not an atomic reset — preset-defined fields reset to preset defaults on every PRESET-bearing create, preset-undefined fields keep stale values; (F7) a bare `poll_channel()` does **not** extend LIFETIME — a lifetime=1000 channel expired on schedule through four polls (Probe 2b Phase A); only LIFETIME-tag-bearing commands refresh it (Phase B). The "(or any other poll)" claim appears in **both** the `create_channel` docstring (~1313–1322) and the `set_channel_lifetime` docstring (~1968–1971). Lifetime units — radiod main-loop frames, ~50/s — must be stated in both.

**Files:**
- Modify: `ka9q/control.py` (docstrings only — no behavior change, no new tests)

- [ ] **Step 1: `create_channel` lifetime paragraph** (~lines 1313–1322) — replace the `lifetime:` argument text with:

```
            lifetime: Optional channel auto-destruct timer (radiod commit 0f8b622+).
                  None (default) = don't send; the channel inherits radiod's
                  Template default (infinite). Integer value = sent verbatim as
                  the LIFETIME tag, in radiod main-loop frames; 0 = infinite,
                  >0 = decremented at the radiod frame rate (~50 frames/s at
                  the default 20 ms blocktime, so 1000 frames ≈ 20 s) and the
                  channel self-destructs at zero. Polling does NOT extend the
                  lifetime (verified live 2026-08-12, audit finding F7): only
                  a command that carries a LIFETIME tag refreshes the timer.
                  Callers using a finite lifetime as crash-safe cleanup must
                  periodically re-send set_channel_lifetime() (or
                  tune(lifetime=...)).
```

- [ ] **Step 2: `create_channel` re-create Note** — add this Note block to the docstring (after `Raises:`, before `Example:`):

```
        Note:
            Calling create_channel() again for an SSRC that already exists
            on radiod is a DELTA-UPDATE, not an atomic reset (verified live,
            audit finding F3): radiod only mutates fields whose TLV is
            present in this packet. Fields the active preset's config stanza
            defines are reset to the preset default on every PRESET-bearing
            create; fields the preset does not define keep whatever an
            earlier command set. "Uses radiod's default if not set" is true
            only for a fresh SSRC. To force a known state on an existing
            channel, pass every field explicitly.
```

Also amend the `sample_rate:` line (~1304) to: `sample_rate: Output sample rate in Hz (optional; radiod default applies on a fresh SSRC only — see the re-create Note below)`.

- [ ] **Step 3: `ensure_channel` re-create note** — append to its existing final `Note:` block (~line 1750):

```
            Re-create semantics: when an existing channel mismatches and
            ensure_channel falls through to create_channel(), that create is
            a delta-update of the surviving radiod channel state, not an
            atomic reset — see create_channel()'s re-create Note (audit
            finding F3).
```

- [ ] **Step 4: `set_channel_lifetime` docstring** — replace the second paragraph (~lines 1965–1971, "ka9q-radio commit 0f8b622+ … as a keep-alive.") with:

```
        ka9q-radio commit 0f8b622+ added a per-channel ``lifetime`` field
        that decrements every radiod main-loop frame (~50 frames/s at the
        default 20 ms blocktime; 1000 frames ≈ 20 s) and destroys the
        channel when it reaches zero.  Bare polls do NOT extend the
        lifetime (verified live 2026-08-12: a lifetime=1000 channel expired
        on schedule through four poll_channel() calls — audit finding F7):
        only a command carrying a LIFETIME tag refreshes the timer.  A
        client using this for crash-safe cleanup must call this method (or
        any command that includes a LIFETIME tag, e.g. tune(lifetime=...))
        periodically as a keep-alive.
```

and in the `lifetime:` argument text (~1975–1980), replace the sentence `radiod will bump it up to the configured idle-timeout floor (typically 1000 frames ≈ 20 s) when this poll is processed.` with `frames tick at ~50/s (default 20 ms blocktime).` — the floor-bump claim rode on the disproven poll-extends model and was not re-verified.

- [ ] **Step 5: Verify**

```bash
grep -n "any other poll\|auto-extends\|Each subsequent poll" ka9q/control.py
uv run pytest -q
```
Expected: grep returns nothing; suite green minus known environmental failures.

- [ ] **Step 6: Commit**

```bash
git add ka9q/control.py
git commit -m "docs(control): correct re-create and lifetime semantics — delta-update, polls don't extend, frame units (audit F3+F7)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: F6 — set_lock() warns that radiod silently ignores LOCK

`set_lock()` sends a correct LOCK TLV, but radiod's `decode_radio_commands()` has no `case LOCK:` at the audited head — the command is silently discarded. No ka9q-python encoding change can fix an upstream omission; the remediation is visibility: a docstring warning plus a `logger.warning` on every call.

**Files:**
- Create: `tests/test_set_lock_warns.py`
- Modify: `ka9q/control.py` (`set_lock`, ~line 3046)

- [ ] **Step 1: Write the failing test**

`tests/test_set_lock_warns.py`:

```python
"""set_lock() must warn loudly: radiod has no LOCK command handler, so the
correct bytes it sends are silently discarded (audit finding F6)."""

import logging
import threading
import time
from unittest.mock import MagicMock

from ka9q.control import RadiodControl


def _bare_control() -> RadiodControl:
    c = RadiodControl.__new__(RadiodControl)
    c.status_address = "test.local"
    c.socket = MagicMock()
    c.dest_addr = ("239.1.2.3", 5006)
    c._socket_lock = threading.RLock()
    c.max_commands_per_sec = 100
    c._command_count = 0
    c._command_window_start = time.time()
    c._rate_limit_lock = threading.Lock()
    c.metrics = MagicMock()
    c._requested_encoding = {}
    return c


def test_set_lock_sends_but_warns(caplog):
    c = _bare_control()
    c.send_command = MagicMock()
    with caplog.at_level(logging.WARNING, logger="ka9q.control"):
        c.set_lock(ssrc=12345, lock=True)
    c.send_command.assert_called_once()   # the (correct) bytes still go out
    assert "no LOCK command handler" in caplog.text
```

- [ ] **Step 2: Run to verify failure**

```bash
uv run pytest tests/test_set_lock_warns.py -v
```
Expected: fails on the warning assertion (no warning emitted today).

- [ ] **Step 3: Implement** — replace `set_lock`'s docstring and add the warning:

```python
    def set_lock(self, ssrc: int, lock: bool):
        """Lock/unlock the tuner (ignore retune commands when locked).

        WARNING: this is currently a SILENT NO-OP against real radiod.
        ka9q-radio's ``decode_radio_commands()`` has no ``case LOCK:``
        handler as of the audited head (cedec349, 2026-08-12), so radiod
        discards the (correctly encoded) TLV and the tuner is NOT
        protected, even though this call returns normally (audit finding
        F6).  A warning is logged on every call so the no-op is visible;
        remove the warning once upstream adds a LOCK command handler.
        """
        _validate_ssrc(ssrc)
        logger.warning(
            "set_lock(ssrc=%s): radiod has no LOCK command handler — this "
            "command will be silently ignored (audit finding F6)", ssrc
        )
        cmdbuffer = bytearray()
        cmdbuffer.append(CMD)
        encode_int(cmdbuffer, StatusType.LOCK, 1 if lock else 0)
        encode_int(cmdbuffer, StatusType.OUTPUT_SSRC, ssrc)
        encode_int(cmdbuffer, StatusType.COMMAND_TAG, secrets.randbits(31))
        encode_eol(cmdbuffer)
        logger.info(f"Setting LOCK={lock} for SSRC {ssrc}")
        self.send_command(cmdbuffer)
```

- [ ] **Step 4: Verify and commit**

```bash
uv run pytest tests/test_set_lock_warns.py -v && uv run pytest -q
git add ka9q/control.py tests/test_set_lock_warns.py
git commit -m "fix(control): set_lock() warns that radiod silently discards LOCK (audit F6)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: F8 — README Troubleshooting section for the lo-route silent write-path failure

The audit lost a full probe round to this: a `239.0.0.0/8 dev lo` kernel route makes every outbound control command vanish with **zero** error signal, while `discover_channels()` (read path) looks perfectly healthy. Diagnosis was tcpdump-confirmed (idempotency.md Round 1, "Root cause"); `RadiodControl(interface=...)` fixes it by forcing `IP_MULTICAST_IF`.

**Files:**
- Modify: `README.md` (new `## Troubleshooting` section + TOC entry)

- [ ] **Step 1: Add the TOC entry** — in the Table of Contents (~line 21), insert before `- [License](#license)`:

```markdown
- [Troubleshooting](#troubleshooting)
```

- [ ] **Step 2: Add the section** — insert immediately before `## License`:

````markdown
## Troubleshooting

### Created channels never appear (silent write-path failure)

**Symptom:** `discover_channels()` works — you can see the radiod's
existing channels — but every `create_channel()` / `ensure_channel()`
you issue "succeeds" (no exception from the send), and then the channel
never shows up. The only visible error is a later
`TimeoutError: Channel SSRC ... not verified within ...s` or a
`poll_channel()` that returns `None` — neither of which names the cause.

**Cause:** some hosts carry a kernel route that sends **all
locally-originated multicast to loopback** (e.g. a `239.0.0.0/8 dev lo`
entry). Inbound multicast still arrives on the real NIC — so discovery
(the read path) looks healthy — while every outbound control command is
routed to `lo` and never reaches radiod. There is no socket error: the
packet is delivered, just to the wrong interface. This was
tcpdump-confirmed during the 2026-08-12 audit: `tcpdump -i <nic>` saw
zero command packets while `tcpdump -i lo` captured all of them
(sourced from 127.0.0.1).

**Check:**

```bash
# Substitute the group your radiod status DNS name resolves to:
ip route get 239.205.73.40
# BAD:  ... dev lo   src ...   <- commands never leave this host
# GOOD: ... dev eth0 src ...
```

**Remedy:** pass your NIC's IP as `interface=` so ka9q-python sets
`IP_MULTICAST_IF` on the send socket, overriding the kernel route:

```python
control = RadiodControl("bee1-status.local", interface="192.168.1.176")
channels = discover_channels("bee1-status.local", interface="192.168.1.176")
```

**Rule of thumb:** a working `discover_channels()` proves only the
*read* path. If reads work but writes vanish, check `ip route get`
first — send-side ACKs don't exist in this protocol, so routing
misconfiguration is otherwise invisible.
````

- [ ] **Step 3: Verify and commit**

```bash
grep -n "Troubleshooting" README.md   # expect: TOC entry + section header
git add README.md
git commit -m "docs(readme): troubleshooting — lo-route silently swallows control commands; interface= remedy (audit F8)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 7: F10 — parse_c_enum accepts negative literals

`parse_c_enum()`'s value regex requires `(\d+)`, so `INVALID_DEMOD = -1` fails to match and is silently skipped — by **both** `sync_types.py` and `check_upstream_drift.py` (which imports it). The safety net has a blind spot for any negative sentinel upstream adds.

**Files:**
- Create: `tests/test_sync_types.py`
- Modify: `scripts/sync_types.py` (~line 63)

- [ ] **Step 1: Write the failing test**

`tests/test_sync_types.py`:

```python
"""Regression tests for scripts/sync_types.py's C-enum parser (audit F10).

The member-value regex required (\\d+), so a negative literal like
upstream's ``INVALID_DEMOD = -1`` failed to match and was silently
skipped — by sync_types.py AND check_upstream_drift.py (which imports
parse_c_enum), blinding the drift safety net to negative sentinels."""

import sys
from pathlib import Path

# Make scripts/ importable (same pattern as tests/test_upstream_drift.py)
SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from sync_types import parse_c_enum  # noqa: E402

HEADER = """
enum demod_type {
  INVALID_DEMOD = -1, // used as sentinel
  LINEAR_DEMOD = 0, // Linear demodulation
  FM_DEMOD, // Frequency demodulation
  N_DEMOD // Dummy equal to number of valid entries
};
"""


def test_negative_literal_is_parsed():
    entries = parse_c_enum(HEADER, "demod_type")
    assert ("INVALID_DEMOD", -1, "used as sentinel") in entries


def test_members_after_negative_keep_correct_values():
    values = {name: value
              for name, value, _ in parse_c_enum(HEADER, "demod_type")}
    assert values == {
        "INVALID_DEMOD": -1,
        "LINEAR_DEMOD": 0,
        "FM_DEMOD": 1,
        "N_DEMOD": 2,
    }
```

- [ ] **Step 2: Run to verify failure**

```bash
uv run pytest tests/test_sync_types.py -v
```
Expected: both fail — `INVALID_DEMOD` is absent from the parse.

- [ ] **Step 3: Implement** — in `scripts/sync_types.py` (~line 63), change the member regex from:

```python
            r"([A-Z][A-Z0-9_]*)\s*(?:=\s*(\d+))?\s*,?\s*(?://\s*(.*))?\s*$",
```

to:

```python
            r"([A-Z][A-Z0-9_]*)\s*(?:=\s*(-?\d+))?\s*,?\s*(?://\s*(.*))?\s*$",
```

(`int(m.group(2))` already handles a leading `-`; the `value += 1` implicit-continuation logic is sign-agnostic.)

- [ ] **Step 4: Verify**

```bash
uv run pytest tests/test_sync_types.py tests/test_upstream_drift.py -v
# Optional cross-check (requires the audit clone; skip cleanly if absent):
test -d ~/audit/ka9q-radio && python3 scripts/sync_types.py --ka9q-radio ~/audit/ka9q-radio --diff | grep -n INVALID_DEMOD
```
Expected: tests pass; if the clone is present, the `--diff` output now proposes `INVALID_DEMOD = -1` for `DemodType`. **Do NOT run `--apply` in this plan** — hand-editing or partially regenerating `ka9q/types.py` is out of scope. `INVALID_DEMOD = -1` lands with the next pin-advance sync, whose flow is: `python3 scripts/sync_types.py --ka9q-radio <clone-at-new-pin> --apply` regenerates `ka9q/types.py` + `ka9q/compat.py` + `ka9q_radio_compat` together, then `scripts/check_upstream_drift.py` must pass. Record that expectation in the commit message.

- [ ] **Step 5: Commit**

```bash
git add scripts/sync_types.py tests/test_sync_types.py
git commit -m "fix(sync_types): parse negative enum literals; INVALID_DEMOD=-1 lands on next sync (audit F10)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 8: F13 — set_demod_type bound derives from DemodType.N_DEMOD

`control.py:2854` hardcodes `0 <= demod_type <= 4`. `types.py` was resynced by the prior plan and now carries `IDLE_DEMOD = 5` / `N_DEMOD = 6`, so the hardcoded check **already** incorrectly rejects a valid demod. Derive the bound from the enum so the two can never drift again.

**Files:**
- Create: `tests/test_set_demod_type.py`
- Modify: `ka9q/control.py` (import line 34; `set_demod_type` ~line 2839)

- [ ] **Step 1: Write the failing test**

`tests/test_set_demod_type.py`:

```python
"""set_demod_type()'s range check must derive from DemodType.N_DEMOD
(audit F13).  types.py's 2026-08 resync added IDLE_DEMOD=5 / N_DEMOD=6;
the old hardcoded ``<= 4`` incorrectly rejects IDLE_DEMOD today."""

import threading
import time
from unittest.mock import MagicMock

import pytest

from ka9q.control import RadiodControl
from ka9q.exceptions import ValidationError
from ka9q.types import DemodType


def _bare_control() -> RadiodControl:
    c = RadiodControl.__new__(RadiodControl)
    c.status_address = "test.local"
    c.socket = MagicMock()
    c.dest_addr = ("239.1.2.3", 5006)
    c._socket_lock = threading.RLock()
    c.max_commands_per_sec = 100
    c._command_count = 0
    c._command_window_start = time.time()
    c._rate_limit_lock = threading.Lock()
    c.metrics = MagicMock()
    c._requested_encoding = {}
    return c


def test_accepts_every_valid_demod_including_idle():
    c = _bare_control()
    c.send_command = MagicMock()
    for demod in range(DemodType.N_DEMOD):        # 0..5 today
        c.set_demod_type(ssrc=12345, demod_type=demod)
    assert c.send_command.call_count == DemodType.N_DEMOD


def test_rejects_n_demod_and_negative():
    c = _bare_control()
    c.send_command = MagicMock()
    with pytest.raises(ValidationError, match="demod_type"):
        c.set_demod_type(ssrc=12345, demod_type=DemodType.N_DEMOD)
    with pytest.raises(ValidationError, match="demod_type"):
        c.set_demod_type(ssrc=12345, demod_type=-1)
    c.send_command.assert_not_called()
```

- [ ] **Step 2: Run to verify failure**

```bash
uv run pytest tests/test_set_demod_type.py -v
```
Expected: `test_accepts_every_valid_demod_including_idle` fails — `demod_type=5` (IDLE_DEMOD) raises today.

- [ ] **Step 3: Implement**

Change the import at `ka9q/control.py:34` to:

```python
from .types import StatusType, CMD, Encoding, DemodType
```

Replace `set_demod_type`'s docstring `demod_type:` line and range check:

```python
            demod_type: Demodulator type — use the DemodType constants
                (0=LINEAR, 1=FM, 2=WFM, 3=SPECTRUM, 4=SPECTRUM2, 5=IDLE).
                The valid range is derived from DemodType.N_DEMOD so it
                tracks types.py resyncs automatically (audit finding F13).
```

```python
        _validate_ssrc(ssrc)
        if not (0 <= demod_type < DemodType.N_DEMOD):
            raise ValidationError(
                f"Invalid demod_type: {demod_type} "
                f"(must be 0-{DemodType.N_DEMOD - 1})"
            )
```

- [ ] **Step 4: Verify and commit**

```bash
uv run pytest tests/test_set_demod_type.py -v && uv run pytest -q
git add ka9q/control.py tests/test_set_demod_type.py
git commit -m "fix(control): set_demod_type bound derives from DemodType.N_DEMOD — IDLE_DEMOD was rejected (audit F13)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 9: F11 — set_spectrum() gains the missing spectrum TLVs; SpectrumStream migrates to it

`WINDOW_TYPE`, `SPECTRUM_AVG`, `SPECTRUM_OVERLAP` are only reachable via `SpectrumStream`'s private hand-rolled buffer; `SPECTRUM_BASE`/`SPECTRUM_STEP` are reachable **nowhere**. Add all five to `set_spectrum()` and collapse the two write paths into one. To let `SpectrumStream._send_spectrum_command()` delegate *fully* (its packet also carries `DEMOD_TYPE` + `RADIO_FREQUENCY`, and single-packet channel setup must be preserved), `set_spectrum()` also gains optional `demod_type` and `frequency_hz` parameters — a deliberate, documented widening of the F11 remediation. Wire encodings match radiod's decode side (`radio_status.c`): int for WINDOW_TYPE/SPECTRUM_AVG, float for SPECTRUM_OVERLAP/SPECTRUM_BASE/SPECTRUM_STEP.

**Files:**
- Create: `tests/test_set_spectrum.py`
- Modify: `ka9q/control.py` (`set_spectrum`, ~line 2772; runs after Task 8 so `DemodType` is already imported)
- Modify: `ka9q/spectrum_stream.py` (`_send_spectrum_command` ~line 225; imports ~lines 43–45)

- [ ] **Step 1: Write the failing tests**

`tests/test_set_spectrum.py`:

```python
"""set_spectrum() must expose every spectrum-mode TLV radiod accepts, and
SpectrumStream must use it instead of a private hand-rolled buffer
(audit finding F11)."""

import threading
import time
from unittest.mock import MagicMock

import pytest

from ka9q.control import RadiodControl, StatusType
from ka9q.exceptions import ValidationError
from ka9q.types import DemodType, WindowType


def _bare_control() -> RadiodControl:
    c = RadiodControl.__new__(RadiodControl)
    c.status_address = "test.local"
    c.socket = MagicMock()
    c.dest_addr = ("239.1.2.3", 5006)
    c._socket_lock = threading.RLock()
    c.max_commands_per_sec = 100
    c._command_count = 0
    c._command_window_start = time.time()
    c._rate_limit_lock = threading.Lock()
    c.metrics = MagicMock()
    c._requested_encoding = {}
    return c


def _capture_send(control):
    sent = []
    control.send_command = MagicMock(
        side_effect=lambda buf: sent.append(bytes(buf)))
    return sent


def _has_tag(buf: bytes, tag: int) -> bool:
    """TLV walker (same as tests/test_filter_edges.py)."""
    cp = 1
    while cp < len(buf):
        t = buf[cp]
        cp += 1
        if t == StatusType.EOL:
            break
        if cp >= len(buf):
            break
        optlen = buf[cp]
        cp += 1
        if optlen & 0x80:
            n = optlen & 0x7F
            optlen = 0
            for _ in range(n):
                if cp >= len(buf):
                    return False
                optlen = (optlen << 8) | buf[cp]
                cp += 1
        if t == tag:
            return True
        cp += optlen
    return False


NEW_TAGS = (
    StatusType.WINDOW_TYPE, StatusType.SPECTRUM_AVG,
    StatusType.SPECTRUM_OVERLAP, StatusType.SPECTRUM_BASE,
    StatusType.SPECTRUM_STEP,
)


class TestSetSpectrumNewParams:
    def test_all_new_tags_emitted(self):
        c = _bare_control()
        sent = _capture_send(c)
        c.set_spectrum(
            ssrc=12345, bin_bw_hz=100.0, bin_count=512,
            window_type=WindowType.HANN_WINDOW, avg=4, overlap=0.5,
            base=-120.0, step=0.5,
            demod_type=DemodType.SPECT2_DEMOD, frequency_hz=14.1e6,
        )
        assert len(sent) == 1
        for tag in NEW_TAGS + (StatusType.DEMOD_TYPE,
                               StatusType.RADIO_FREQUENCY):
            assert _has_tag(sent[0], tag), f"missing tag {tag}"

    def test_omitted_params_send_no_tags(self):
        c = _bare_control()
        sent = _capture_send(c)
        c.set_spectrum(ssrc=12345, bin_bw_hz=100.0, bin_count=512)
        for tag in NEW_TAGS + (StatusType.DEMOD_TYPE,
                               StatusType.RADIO_FREQUENCY):
            assert not _has_tag(sent[0], tag), f"unexpected tag {tag}"

    def test_avg_must_be_at_least_one(self):
        c = _bare_control()
        c.send_command = MagicMock()
        with pytest.raises(ValidationError, match="avg"):
            c.set_spectrum(ssrc=12345, avg=0)

    def test_overlap_must_be_below_one(self):
        c = _bare_control()
        c.send_command = MagicMock()
        with pytest.raises(ValidationError, match="overlap"):
            c.set_spectrum(ssrc=12345, overlap=1.0)


def test_spectrum_stream_delegates_to_set_spectrum():
    from ka9q.spectrum_stream import SpectrumStream
    control = MagicMock()
    s = SpectrumStream(
        control=control, frequency_hz=14.1e6, bin_count=512,
        resolution_bw=100.0, window_type=WindowType.HANN_WINDOW,
        kaiser_beta=11.0, averaging=4, overlap=0.5,
    )
    s._ssrc = 777
    s._send_spectrum_command()
    control.set_spectrum.assert_called_once_with(
        777,
        bin_bw_hz=100.0,
        bin_count=512,
        kaiser_beta=11.0,
        window_type=WindowType.HANN_WINDOW,
        avg=4,
        overlap=0.5,
        demod_type=DemodType.SPECT2_DEMOD,
        frequency_hz=14.1e6,
    )
    assert s._polls_sent == 1
```

- [ ] **Step 2: Run to verify failure**

```bash
uv run pytest tests/test_set_spectrum.py -v
```
Expected: all fail (`set_spectrum() got an unexpected keyword argument 'window_type'`; delegation test fails because `set_spectrum` is never called).

- [ ] **Step 3: Implement `set_spectrum`** — replace the whole method (~lines 2772–2810) with:

```python
    def set_spectrum(self, ssrc: int, bin_bw_hz: Optional[float] = None, bin_count: Optional[int] = None,
                     crossover_hz: Optional[float] = None, kaiser_beta: Optional[float] = None,
                     window_type: Optional[int] = None, avg: Optional[int] = None,
                     overlap: Optional[float] = None, base: Optional[float] = None,
                     step: Optional[float] = None, demod_type: Optional[int] = None,
                     frequency_hz: Optional[float] = None):
        """
        Configure spectrum analyzer mode parameters.

        Every parameter is optional; only the TLVs for parameters you pass
        are sent (radiod leaves absent fields unchanged — delta-update).
        This is the single public write path for spectrum TLVs; audit
        finding F11 added window_type/avg/overlap/base/step (previously
        unreachable or reachable only via SpectrumStream's private buffer)
        plus demod_type/frequency_hz so SpectrumStream can build its whole
        channel-setup command through this method in one packet.

        Args:
            ssrc: SSRC of the channel
            bin_bw_hz: Bin bandwidth in Hz (RESOLUTION_BW)
            bin_count: Number of frequency bins (BIN_COUNT)
            crossover_hz: Crossover frequency between algorithms in Hz (CROSSOVER)
            kaiser_beta: Kaiser window beta for spectrum analysis (SPECTRUM_SHAPE)
            window_type: FFT window (WINDOW_TYPE — see WindowType constants)
            avg: Number of FFTs averaged into each spectrum response
                 (SPECTRUM_AVG; must be >= 1)
            overlap: FFT window overlap ratio when averaging
                 (SPECTRUM_OVERLAP; 0.0 <= overlap < 1.0)
            base: Base level of 1-byte analyzer data in dB (SPECTRUM_BASE)
            step: Level step of 1-byte analyzer data in dB (SPECTRUM_STEP)
            demod_type: Optional demod to set in the same packet (typically
                 DemodType.SPECT_DEMOD or SPECT2_DEMOD) so a spectrum
                 channel can be created/retargeted with one command.
            frequency_hz: Optional center frequency to set in the same packet.

        Example:
            >>> control.set_spectrum(ssrc=12345, bin_bw_hz=100, bin_count=512,
            ...                      window_type=WindowType.HANN_WINDOW,
            ...                      avg=4, overlap=0.5)
        """
        _validate_ssrc(ssrc)

        cmdbuffer = bytearray()
        cmdbuffer.append(CMD)

        # DEMOD_TYPE / RADIO_FREQUENCY first, mirroring the packet layout
        # SpectrumStream has always sent (channel setup before analyzer
        # parameters).
        if demod_type is not None:
            if not (0 <= demod_type < DemodType.N_DEMOD):
                raise ValidationError(
                    f"Invalid demod_type: {demod_type} "
                    f"(must be 0-{DemodType.N_DEMOD - 1})"
                )
            encode_int(cmdbuffer, StatusType.DEMOD_TYPE, demod_type)
        if frequency_hz is not None:
            _validate_frequency(frequency_hz)
            encode_double(cmdbuffer, StatusType.RADIO_FREQUENCY, frequency_hz)

        if bin_bw_hz is not None:
            _validate_positive(bin_bw_hz, "Bin bandwidth")
            encode_float(cmdbuffer, StatusType.RESOLUTION_BW, bin_bw_hz)
        if bin_count is not None:
            if bin_count <= 0:
                raise ValidationError(f"bin_count must be positive, got {bin_count}")
            encode_int(cmdbuffer, StatusType.BIN_COUNT, bin_count)
        if crossover_hz is not None:
            _validate_positive(crossover_hz, "Crossover frequency")
            encode_float(cmdbuffer, StatusType.CROSSOVER, crossover_hz)
        if kaiser_beta is not None:
            encode_float(cmdbuffer, StatusType.SPECTRUM_SHAPE, kaiser_beta)
        if window_type is not None:
            if window_type < 0:
                raise ValidationError(
                    f"window_type must be >= 0, got {window_type}")
            encode_int(cmdbuffer, StatusType.WINDOW_TYPE, window_type)
        if avg is not None:
            if avg < 1:
                raise ValidationError(f"avg must be >= 1, got {avg}")
            encode_int(cmdbuffer, StatusType.SPECTRUM_AVG, avg)
        if overlap is not None:
            if not (0.0 <= overlap < 1.0):
                raise ValidationError(
                    f"overlap must be in [0.0, 1.0), got {overlap}")
            encode_float(cmdbuffer, StatusType.SPECTRUM_OVERLAP, overlap)
        if base is not None:
            encode_float(cmdbuffer, StatusType.SPECTRUM_BASE, base)
        if step is not None:
            encode_float(cmdbuffer, StatusType.SPECTRUM_STEP, step)

        encode_int(cmdbuffer, StatusType.OUTPUT_SSRC, ssrc)
        encode_int(cmdbuffer, StatusType.COMMAND_TAG, secrets.randbits(31))
        encode_eol(cmdbuffer)

        logger.info(
            f"Setting spectrum for SSRC {ssrc}: bw={bin_bw_hz} Hz, "
            f"bins={bin_count}, crossover={crossover_hz} Hz, "
            f"window={window_type}, avg={avg}, overlap={overlap}, "
            f"base={base}, step={step}"
        )
        self.send_command(cmdbuffer)
```

- [ ] **Step 4: Migrate `SpectrumStream._send_spectrum_command`** — replace the method (~lines 225–245) with:

```python
    def _send_spectrum_command(self):
        """Request spectrum data via RadiodControl.set_spectrum().

        Formerly hand-encoded its own TLV buffer, which made it the only
        reachable write path for WINDOW_TYPE/SPECTRUM_AVG/SPECTRUM_OVERLAP;
        migrated to the public set_spectrum() so there is exactly one
        spectrum write path (audit finding F11).  Packet layout (DEMOD_TYPE
        and RADIO_FREQUENCY before the analyzer parameters, all in one
        command) is preserved by set_spectrum().
        """
        self._control.set_spectrum(
            self._ssrc,
            bin_bw_hz=self._resolution_bw,
            bin_count=self._bin_count,
            kaiser_beta=self._kaiser_beta,
            window_type=self._window_type,
            avg=self._averaging,
            overlap=self._overlap,
            demod_type=self._demod_type,
            frequency_hz=self._frequency_hz,
        )
        self._polls_sent += 1
```

and replace the now-partially-unused imports (~lines 43–45):

```python
from .control import RadiodControl
from .status import ChannelStatus, decode_status_packet
from .types import DemodType
```

(`encode_int`/`encode_float`/`encode_double`/`encode_eol`, `StatusType`, and `CMD` were used only by the old buffer — confirm with `grep -n "encode_\|StatusType\|CMD" ka9q/spectrum_stream.py` before deleting; `secrets`/`struct` are still used elsewhere in the file and stay.)

- [ ] **Step 5: Verify and commit**

```bash
uv run pytest tests/test_set_spectrum.py tests/test_spectrum.py -v && uv run pytest -q
uv run pytest tests/test_client_contract.py -q
git add ka9q/control.py ka9q/spectrum_stream.py tests/test_set_spectrum.py
git commit -m "feat(control): set_spectrum gains window/avg/overlap/base/step (+demod/freq); SpectrumStream delegates (audit F11)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 10: F12 — decode SETOPTS into ChannelStatus.options

radiod encodes `chan->options` under the `SETOPTS` tag (`encode_int64`) on **every** status packet; ka9q-python drops it. Without it a client can't confirm `set_options()` took effect or see options set elsewhere.

**Files:**
- Modify: `ka9q/status.py` (`ChannelStatus` field ~line 254; decode branch ~line 620)
- Modify: `tests/test_status_decoder.py` (append tests)

- [ ] **Step 1: Write the failing tests** — append to `tests/test_status_decoder.py`:

```python
def test_decode_setopts_options_bitmask():
    """radiod encode_int64()s chan->options under SETOPTS on every status
    packet; it must land in ChannelStatus.options (audit F12)."""
    pkt = _build_packet(
        ("int", StatusType.OUTPUT_SSRC, 42),
        ("int64", StatusType.SETOPTS, 0b1010),
    )
    st = decode_status_packet(pkt)
    assert st is not None
    assert st.options == 0b1010


def test_options_none_when_absent():
    pkt = _build_packet(("int", StatusType.OUTPUT_SSRC, 42))
    st = decode_status_packet(pkt)
    assert st.options is None
```

- [ ] **Step 2: Run to verify failure**

```bash
uv run pytest tests/test_status_decoder.py -v -k options
```
Expected: `AttributeError: 'ChannelStatus' object has no attribute 'options'`.

- [ ] **Step 3: Implement** — in `ka9q/status.py`, add to `ChannelStatus`'s "Options / modes" field group (directly after the `lifetime` field, ~line 256):

```python
    options: Optional[int] = None              # SETOPTS — demod option bitmask;
                                               # radiod encodes chan->options on
                                               # every status packet (audit F12)
```

and in `decode_status_packet`, after the `StatusType.LIFETIME` branch (~line 620):

```python
        elif t == StatusType.SETOPTS:
            st.options = decode_int64(data, optlen)
```

- [ ] **Step 4: Verify and commit**

```bash
uv run pytest tests/test_status_decoder.py -v && uv run pytest -q
git add ka9q/status.py tests/test_status_decoder.py
git commit -m "feat(status): decode SETOPTS into ChannelStatus.options (audit F12)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 11: F14 — re-export resolve_multicast_address from ka9q/__init__.py

`ka9q.utils.resolve_multicast_address` is public but was never re-exported, so hf-timestd reaches into `ka9q.utils` directly. Simplest approved option: re-export it (its signature is already snapshotted in the manifest as `ka9q.utils:resolve_multicast_address` and does not change). The report's alternative — a typed `RadiodControl.is_reachable()` health-check with a proper exception class — was considered and **deferred**: it is a new API surface deserving its own design pass, and the re-export unblocks the internals-reach today.

**Files:**
- Modify: `ka9q/__init__.py`
- Modify: `tests/test_multicast_helpers.py` (append test)

- [ ] **Step 1: Write the failing test** — append to `tests/test_multicast_helpers.py`:

```python
def test_resolve_multicast_address_is_reexported():
    """audit F14: hf-timestd reaches into ka9q.utils for this; make it a
    first-class export so the internals-reach can be retired."""
    import ka9q
    from ka9q.utils import resolve_multicast_address as util_fn
    assert ka9q.resolve_multicast_address is util_fn
    assert "resolve_multicast_address" in ka9q.__all__
```

- [ ] **Step 2: Run to verify failure**

```bash
uv run pytest tests/test_multicast_helpers.py -v -k reexported
```
Expected: `AttributeError: module 'ka9q' has no attribute 'resolve_multicast_address'`.

- [ ] **Step 3: Implement** — in `ka9q/__init__.py`:

In the `__all__` list, under the `# Utilities` group (next to `'generate_multicast_ip'`), add:

```python
    'resolve_multicast_address',
```

At the bottom import cluster (next to `from .addressing import generate_multicast_ip`), add:

```python
from .utils import resolve_multicast_address
```

- [ ] **Step 4: Verify and commit**

```bash
uv run pytest tests/test_multicast_helpers.py tests/test_client_contract.py -v
uv run pytest -q
git add ka9q/__init__.py tests/test_multicast_helpers.py
git commit -m "feat(api): re-export resolve_multicast_address from ka9q (audit F14; is_reachable() alternative deferred)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 12: Version bump + full-suite and live verification

**Files:**
- Modify: `pyproject.toml` (version)

- [ ] **Step 1: Version bump** — this plan adds public capability (F11 params, F12 field, F14 export) and one deliberate behavior change (F5: create_channel now derives a per-client destination from client_id and fails loudly only when neither client_id nor destination= is available): a **minor** bump. In `pyproject.toml` change:

```toml
version = "3.21.1"
```

to:

```toml
version = "3.22.0"
```

- [ ] **Step 2: Full unit suite**

```bash
uv run pytest -q
```
Expected: green except exactly the known environmental failures (Global Constraints: `test_integration` ×10, `test_iq_20khz_f32` ×1, `test_protocol_compat` ×2). Compare the failure list against a pre-plan baseline (`git stash` not needed — the known list above IS the baseline). Any new failure: stop, use superpowers:systematic-debugging, fix before committing.

- [ ] **Step 3: Contract guardrail**

```bash
uv run pytest tests/test_client_contract.py tests/test_gen_client_manifest.py -q
```
Expected: all pass with **no** manifest regeneration (no snapshotted signature changed in this plan). If `test_client_contract` fails, a task changed a snapshotted signature unintentionally — investigate before reaching for `gen_client_manifest.py`.

- [ ] **Step 4: Live integration run (b1)**

```bash
RADIOD_HOST=bee1-status.local uv run pytest tests/test_idempotency_integration.py -v --radiod-host=bee1-status.local
```
Rules: b1 first, b2 (`bee2-status.local`) as fallback; **never** b3/b4. The fixtures already force `interface=` (F8) and default `destination=` to a discovered live group (F5), and confine SSRCs to 3999900010–3999900019 with unconditional teardown. If everything times out while discovery works, apply the new README Troubleshooting section (`ip route get`). If both hosts are unreachable, record that in the commit message — do not fake results.

- [ ] **Step 5: Leftover sweep** — b1/b2 must be left clean:

```bash
uv run python - <<'EOF'
from ka9q import discover_channels
for host in ("bee1-status.local",):
    for ssrc, ch in discover_channels(host, listen_duration=5.0).items():
        if 3999900000 <= ssrc <= 3999900999:
            print("LEFTOVER:", host, ssrc)
EOF
```
Expected: no output. Any leftover: remove via `RadiodControl(host, interface=<NIC IP>).remove_channel(ssrc)` and file the teardown gap as a finding.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml
git commit -m "chore: bump version to 3.22.0 — 2026-08-12 audit remediation (F1,F3-F8,F10-F14)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```
