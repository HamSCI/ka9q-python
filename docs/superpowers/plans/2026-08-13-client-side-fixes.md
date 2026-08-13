# Client-Side Remediation Implementation Plan (2026-08-12 Alignment Audit — F15/F16/F18/F19/F21, client scope)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remediate the client-side findings of the 2026-08-12 upstream-alignment audit that were declared out of scope for the ka9q-python remediation plan ([2026-08-13-audit-remediation.md](2026-08-13-audit-remediation.md), Global Constraints: "Client-side findings F15–F21 are OUT of scope"). Finding IDs reference [docs/audit/2026-08-12-alignment/findings.md](../../audit/2026-08-12-alignment/findings.md); exact call-site evidence references [docs/audit/2026-08-12-alignment/clients.md](../../audit/2026-08-12-alignment/clients.md). Scope: F19 + F18 (meteor-scatter), F18 (psk-recorder), F15 + F16 + F21 (hf-timestd), F16 (wspr-recorder), and the ka9q-python half of F16 (the `DeprecationWarning` on the `rtp_to_wallclock` alias, deferred until the two lagging clients migrate). **F17 (StreamQuality field-stability doc) and F20 (hf-timestd audio_streamer consolidation) are explicitly OUT of scope** — F20 is an optional consolidation, deferred by owner instruction.

**Architecture:** One task per repo, each independently shippable and committed to that repo's own `main`. Order: meteor-scatter first (highest value — its slide-follow timing correction is currently inert, F19), then psk-recorder (smallest), then hf-timestd (largest), then wspr-recorder (carries a pre-existing dirty `uv.lock` that needs explicit handling), and finally ka9q-python (Task 5, **gated** on Tasks 3+4 landing — the alias must not start warning while hf-timestd and wspr-recorder still call it in production). Every repo task also bumps that repo's `ka9q-python` floor to `>=3.22.0` (the version carrying the ka9q-side audit remediations, commit `ee33f07`) and re-locks. Every code change is TDD: failing test first, then the exact implementation below. All recon claims in this plan (line numbers, current code, test conventions) were verified against the working trees on 2026-08-13.

**Tech Stack:** Python 3 / pytest, `uv` (canonical in all five repos: `uv sync --extra dev`, `uv run pytest`), git. No new dependencies anywhere.

## Global Constraints

- **One repo per task; commit to that repo's own `main`.** Every commit message ends with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`. During a task, all repos other than the task's repo are read-only (Task 5 may *read* client repos to verify the gate and regenerate the manifest, but modifies only ka9q-python).
- **Deployment/restart of live services is OUT of scope.** Commits only — the user deploys (`sudo ./scripts/deploy.sh` in each client repo is the operator's job, not this plan's). Note this at the end of every task.
- **Production behavior of hf-timestd / psk-recorder / meteor-scatter / wspr-recorder must not change beyond the findings' remediations.** Concretely ruled here: wspr-recorder's `correlation_source` label strings (`"rtp_to_wallclock[+authority]"`) are observable status/journal values and are **kept unchanged** by the F16 migration (Task 4); hf-timestd's own public re-export name `hf_timestd.rtp_to_wallclock` is **kept** as an alias (Task 3).
- **Test commands are per-repo** (verified against each repo's CLAUDE.md during recon — do not assume):
  - meteor-scatter: `uv sync --extra dev` then `uv run pytest tests/ -v` (~222 tests)
  - psk-recorder: `uv sync --extra dev` then `uv run pytest tests/ -v` (~222 tests)
  - hf-timestd: `uv run pytest tests/`
  - wspr-recorder: `uv run pytest tests/` (pytest-asyncio, `asyncio_mode = "auto"`, ~361 tests)
  - ka9q-python: `uv run pytest -q` (known environmental failures per the sibling plan's Global Constraints: `tests/test_integration.py` (10), `tests/test_iq_20khz_f32.py` (1), `tests/test_protocol_compat.py` (2) — not yours to fix)
- **Record a baseline before touching anything** in each repo (`uv run pytest tests/ -q 2>&1 | tail -5`, saved to the task notes). Pre-existing failures are not yours to fix — report them and show the post-change run has no *new* failures.
- **No live radiod needed anywhere in this plan.** Every test below is unit-level (mocks/fakes). Do not touch bee1–bee4.
- **ka9q-python 3.22.0 behavior change relevant to every bump (audit F5):** `create_channel()`/`ensure_channel()` with **neither** `client_id` (on the `RadiodControl`) **nor** `destination=` now raises `ValidationError` instead of silently creating nothing. Each repo task therefore includes a verification step grepping that repo's `RadiodControl(` constructions. Recon result (verify, don't trust): all four repos' **production** constructions pass `client_id` — meteor-scatter `receiver_manager.py:188` (`client_id="meteor-scatter"`), psk-recorder `receiver_manager.py:193`, wspr-recorder `receiver_manager.py:349`, hf-timestd `channel_manager.py:77` / `core_recorder_v2.py:224-225` / `stream/stream_manager.py:329-330`. The only 3.22.0-breakable call sites found are three hf-timestd **operator scripts** that construct bare `RadiodControl(...)` and then create/ensure — fixed in Task 3 Step 8.
- All ka9q-python floors move to `>=3.22.0` via: edit `pyproject.toml`, then `uv lock --upgrade-package ka9q-python`, then `uv sync --extra dev`, then verify the lock records `version = "3.22.0"` for `ka9q-python`. All four client repos use `[tool.uv.sources] ka9q-python = { path = "../ka9q-python", editable = true }`, so the lock picks the version straight from the sibling checkout — which is already at 3.22.0 (`ee33f07`).
- Self-review before finishing each task: no placeholders, every referenced file exists, every command runnable from the repo root named in the task.

---

### Task 1: meteor-scatter — wire StatusListener (F19), guard offset_of_rtp() (F18), bump to ka9q-python 3.22.0

**Repo:** `/opt/git/sigmond/meteor-scatter` (all paths below relative to it). Three commits: F19, F18, bump.

Why F19 matters (findings.md): meteor-scatter's slide-follow hook `ChannelSink._anchor_utc_now()` (`src/meteor_scatter/core/stream.py:136-159`) re-reads `channel_info.gps_time`/`rtp_timesnap` every tick via `rtp_to_utc`, but nothing ever refreshes that object — unlike psk-recorder, which starts a `ka9q.StatusListener` and registers each channel's `ChannelInfo` so radiod's ~2 Hz STATUS broadcasts mutate it **in place**. The mechanism is present in code but inert. The reference implementation is psk-recorder's `src/psk_recorder/core/receiver_manager.py` — listener start at lines 199-209, `register_channel` at 435-441, listener stop at 516-526, `self._status_listener = None` init at line 126. Mirror it.

Why F18 matters: both `src/meteor_scatter/core/stream.py:245` (`ChannelSink.on_samples`, called from MultiStream's receive thread) and `src/meteor_scatter/core/slot.py:185` (`SlotWorker._tick`, worker thread) call `SlotClock.offset_of_rtp()` unguarded. `ka9q/slot_clock.py` raises `SlotClockDesyncError` when a timestamp is >`2**30` samples from the unwrap high-water; `SlotClock.advance()` catches it internally (slot_clock.py:250-264: log error → `self.reset()` → return, so the caller's "if not anchored: anchor()" path re-establishes the grid), but `offset_of_rtp()` deliberately propagates. Today a genuine desync either propagates into MultiStream's receive thread (stream.py) or loops as a logged `SlotWorker tick error` every 500 ms forever with no recovery (slot.py — `_loop` catches `Exception` but nothing resets the clock, so `offset_of_rtp` raises identically on every subsequent tick). The correct recovery, mirroring both `advance()`'s internal handling and this repo's own `on_stream_restored` precedent, is: drop the anchor **and** the ring (ring offsets live in the dead reference space), reset the slot worker's boundary, and let the next batch re-anchor from live `channel_info` — which F19's listener keeps fresh. That is exactly the reset block `on_stream_restored` already contains (stream.py:293-301); extract it as `_reset_timing()` and reuse it.

**Files:**
- Modify: `src/meteor_scatter/core/receiver_manager.py` (F19: 4 edits)
- Modify: `src/meteor_scatter/core/stream.py` (F18: import, `_reset_timing()`, `on_samples` guard, `on_stream_restored` rewire, `SlotWorker(...)` construction)
- Modify: `src/meteor_scatter/core/slot.py` (F18: import, `on_desync` param, `_tick` guard)
- Create: `tests/test_status_listener_wiring.py`
- Modify: `tests/test_stream_anchoring.py`, `tests/test_slot.py` (append test classes)
- Modify: `pyproject.toml`, `uv.lock` (bump)

- [ ] **Step 0: Baseline**

```bash
cd /opt/git/sigmond/meteor-scatter && git status --short   # expect clean
uv run pytest tests/ -q 2>&1 | tail -5                     # record pass/fail counts
```

- [ ] **Step 1: Write the failing F19 tests**

`tests/test_status_listener_wiring.py` (fake `ka9q` modules via `sys.modules`, matching this suite's deliberate no-real-ka9q-import convention in `tests/test_receiver_manager.py`):

```python
"""audit F19: wire ka9q.StatusListener so slide-follow tracks radiod's
live RTP<->UTC drift.

ChannelSink._anchor_utc_now() re-reads channel_info.gps_time/rtp_timesnap
every tick, but nothing refreshed that object in place — psk-recorder
starts a StatusListener and registers each channel's ChannelInfo so
radiod's ~2 Hz STATUS broadcasts mutate it live.  These tests assert the
same wiring, mirrored from psk-recorder's receiver_manager.py.
"""
from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from unittest import mock
from unittest.mock import MagicMock

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from meteor_scatter.core.receiver_manager import ReceiverManager


def _fake_ka9q_modules():
    """Minimal fake ka9q + ka9q.status_listener for provisioning paths."""
    ka9q = types.ModuleType("ka9q")
    ka9q.RadiodControl = MagicMock(name="RadiodControl")
    ka9q.MultiStream = MagicMock(name="MultiStream")
    sl_mod = types.ModuleType("ka9q.status_listener")
    sl_mod.StatusListener = MagicMock(name="StatusListener")
    ka9q.status_listener = sl_mod
    return {"ka9q": ka9q, "ka9q.status_listener": sl_mod}


def _make_rx(radiod_block=None):
    return ReceiverManager(
        config={"paths": {}, "station": {}, "processing": {}},
        radiod_block=radiod_block or {"status": "rx.local"},
        spool_root=Path("/tmp/ms-test-spool"),
        log_dir=Path("/tmp/ms-test-log"),
        radiod_lifetime_frames=0,
    )


class StatusListenerStartTests(unittest.TestCase):

    def test_provision_starts_status_listener(self):
        """provision_channels must start a StatusListener on the radiod's
        status address right after creating the RadiodControl (psk-recorder
        pattern).  A zero-band radiod_block makes provisioning raise
        RuntimeError at the end ("no channels could be provisioned"); the
        listener must already be up and recorded by then."""
        mods = _fake_ka9q_modules()
        rx = _make_rx()
        with mock.patch.dict(sys.modules, mods):
            with self.assertRaises(RuntimeError):
                rx.provision_channels(
                    decoder="", decoder_kind="jt9",
                    keep_wav=False, spool_spots=False,
                )
        sl_cls = mods["ka9q.status_listener"].StatusListener
        sl_cls.assert_called_once_with("rx.local")
        sl_cls.return_value.start.assert_called_once_with()
        self.assertIs(rx._status_listener, sl_cls.return_value)

    def test_listener_failure_does_not_block_provisioning(self):
        """Best-effort: StatusListener blowing up must not abort
        provisioning (psk-recorder wraps the whole block in try/except)."""
        mods = _fake_ka9q_modules()
        mods["ka9q.status_listener"].StatusListener.side_effect = OSError("no socket")
        rx = _make_rx()
        with mock.patch.dict(sys.modules, mods):
            with self.assertRaises(RuntimeError):   # zero bands, as above
                rx.provision_channels(
                    decoder="", decoder_kind="jt9",
                    keep_wav=False, spool_spots=False,
                )
        self.assertIsNone(rx._status_listener)


class RegisterChannelTests(unittest.TestCase):

    def _provisioned_rx(self):
        rx = _make_rx()
        rx._control = MagicMock()
        info = MagicMock()
        info.multicast_address = "239.1.2.3"
        info.port = 5004
        rx._control.ensure_channel.return_value = info
        return rx

    def _sink(self):
        sink = MagicMock()
        sink.frequency_hz = 28_145_000
        sink.preset = "usb"
        sink.sample_rate = 12000
        sink.encoding = 4
        return sink

    def test_add_sink_registers_channel_info_with_listener(self):
        """_add_sink_to_multi must register the SAME ChannelInfo object it
        hands the sink — in-place mutation by the listener is the whole
        mechanism (clients.md: 'assumes the listener mutates the same
        ChannelInfo object in place')."""
        mods = _fake_ka9q_modules()
        rx = self._provisioned_rx()
        ch_info = MagicMock()
        ch_info.ssrc = 42
        mods["ka9q"].MultiStream.return_value.add_channel.return_value = ch_info
        rx._status_listener = MagicMock()
        sink = self._sink()
        with mock.patch.dict(sys.modules, mods):
            rx._add_sink_to_multi(sink, {})
        sink.set_channel_info.assert_called_once_with(ch_info)
        rx._status_listener.register_channel.assert_called_once_with(ch_info)

    def test_register_failure_is_swallowed(self):
        mods = _fake_ka9q_modules()
        rx = self._provisioned_rx()
        mods["ka9q"].MultiStream.return_value.add_channel.return_value = (
            MagicMock(ssrc=7))
        rx._status_listener = MagicMock()
        rx._status_listener.register_channel.side_effect = RuntimeError("boom")
        with mock.patch.dict(sys.modules, mods):
            rx._add_sink_to_multi(self._sink(), {})   # must not raise


class StopStopsListenerTests(unittest.TestCase):

    def test_stop_stops_listener_and_clears_it(self):
        rx = _make_rx()
        listener = MagicMock()
        rx._status_listener = listener
        rx.stop()
        listener.stop.assert_called_once_with()
        self.assertIsNone(rx._status_listener)

    def test_stop_swallows_listener_stop_error(self):
        rx = _make_rx()
        listener = MagicMock()
        listener.stop.side_effect = RuntimeError("already dead")
        rx._status_listener = listener
        rx.stop()   # must not raise
        self.assertIsNone(rx._status_listener)
```

- [ ] **Step 2: Run to verify failure**

```bash
uv run pytest tests/test_status_listener_wiring.py -v
```
Expected: all six fail — `StatusListener` never constructed, `register_channel` never called, `rx._status_listener` untouched by `stop()`.

- [ ] **Step 3: Implement F19 (four edits in `src/meteor_scatter/core/receiver_manager.py`, mirroring psk-recorder verbatim)**

(a) In `__init__` (~line 122, next to `self._control = None`), add:

```python
        self._status_listener = None
```

(b) In `provision_channels()`, immediately after the `self._control = RadiodControl(status, client_id="meteor-scatter")` line (~188), insert:

```python
        # Keep each channel_info fresh from radiod's status broadcasts
        # (~2 Hz) so the slide-follow hook (ChannelSink._anchor_utc_now)
        # reads radiod's CURRENT GPS reference instead of the
        # provisioning-time snapshot.  Without this the slide-follow
        # mechanism is inert (audit finding F19).  psk-recorder pattern,
        # mirrored verbatim.  Best-effort: a listener failure must not
        # block provisioning.
        try:
            from ka9q.status_listener import StatusListener
            self._status_listener = StatusListener(status)
            self._status_listener.start()
            logger.info("ReceiverManager %s: status anchor listener started on %s",
                        self._radiod_id, status)
        except Exception as e:
            logger.warning("ReceiverManager %s: status anchor listener unavailable: %s",
                           self._radiod_id, e)
            self._status_listener = None
```

(c) In `_add_sink_to_multi()`, immediately after `sink.set_channel_info(ch_info)` (~line 413), insert:

```python
        # Register this same ChannelInfo so the listener refreshes its
        # anchor in place — keeping the object the sink holds current (F19).
        if self._status_listener is not None:
            try:
                self._status_listener.register_channel(ch_info)
            except Exception as e:
                logger.debug("register_channel failed for ssrc %s: %s",
                             getattr(ch_info, "ssrc", "?"), e)
```

(d) In `stop()` (~line 484), insert as the **first** block of the method body (before the ch_tailer loop):

```python
        if self._status_listener is not None:
            try:
                self._status_listener.stop()
            except Exception:
                logger.exception(
                    "ReceiverManager %s: error stopping status listener",
                    self._radiod_id,
                )
            self._status_listener = None
```

Known parity limitation (mirror of psk-recorder, deliberately NOT fixed here — report it): `on_stream_restored` hands the sink a **new** `ChannelInfo` object that is never registered with the listener, so after a radiod-restart-class restore the slide-follow reads a fresh-but-unrefreshed snapshot until the next full re-provision. psk-recorder has the identical gap; fixing it in one sibling only would fork the shared design. Flag for a follow-up covering both repos.

- [ ] **Step 4: Verify and commit F19**

```bash
uv run pytest tests/test_status_listener_wiring.py tests/test_receiver_manager.py -v
git add src/meteor_scatter/core/receiver_manager.py tests/test_status_listener_wiring.py
git commit -m "fix(receiver): wire ka9q StatusListener so slide-follow tracks live RTP<->UTC drift (audit F19)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

- [ ] **Step 5: Write the failing F18 tests**

Append to `tests/test_stream_anchoring.py` (reuses that file's existing `_make_sink`/`_cleanup_sink`/`_FakeQuality`/`_FakeChannelInfo`/`_NoAuthority` helpers and its `SR = 12000`; `mock` and `np` are already imported there):

```python
class TestDesyncRecovery(unittest.TestCase):
    """audit F18: a SlotClockDesyncError from offset_of_rtp() must not
    propagate out of on_samples.  Recover the way SlotClock.advance()
    does internally (ka9q/slot_clock.py:250-264: log, reset, return) plus
    this sink's own on_stream_restored precedent: drop anchor AND ring
    (ring offsets live in the dead reference space), then re-anchor on
    the next batch."""

    def _anchor(self, sink, first_rtp=1_000_000, n=2400):
        q = _FakeQuality(last_rtp_timestamp=(first_rtp + n) & 0xFFFFFFFF)
        with mock.patch("ka9q.rtp_to_utc", return_value=1_700_000_500.0):
            with mock.patch("hamsci_dsp.timing.time.time",
                            return_value=1_700_000_500.0):
                sink.on_samples(np.zeros(n, dtype=np.float32), q)
        self.assertTrue(sink._clock.anchored)

    def test_desync_batch_resets_instead_of_raising(self):
        sink = _make_sink(authority_reader=_NoAuthority())
        sink.set_channel_info(_FakeChannelInfo())
        try:
            self._anchor(sink)
            n = 2400
            # > _SAFE_UNWRAP_SAMPLES (2**30) past the unwrap high-water:
            # offset_of_rtp cannot disambiguate and raises.
            far = (1_000_000 + n + 2**30 + 10 * SR) & 0xFFFFFFFF
            q2 = _FakeQuality(last_rtp_timestamp=far)
            sink.on_samples(np.zeros(n, dtype=np.float32), q2)  # must not raise
            self.assertFalse(sink._clock.anchored)
            self.assertIsNone(sink._anchor_rtp)
        finally:
            _cleanup_sink(sink)

    def test_next_batch_after_desync_reanchors(self):
        sink = _make_sink(authority_reader=_NoAuthority())
        sink.set_channel_info(_FakeChannelInfo())
        try:
            self._anchor(sink)
            n = 2400
            far = (1_000_000 + n + 2**30 + 10 * SR) & 0xFFFFFFFF
            sink.on_samples(np.zeros(n, dtype=np.float32),
                            _FakeQuality(last_rtp_timestamp=far))
            # The very next batch re-establishes the grid cleanly.
            self._anchor(sink, first_rtp=far, n=n)
        finally:
            _cleanup_sink(sink)
```

Append to `tests/test_slot.py` (reuses its `_make_worker` helper; `tempfile` is already imported there; `SR = 12000` at module top):

```python
class DesyncGuardTests(unittest.TestCase):
    """audit F18: a SlotClockDesyncError inside _tick's harvest must be
    caught and recovered via the on_desync callback (ChannelSink's
    _reset_timing) instead of re-raising as a 'SlotWorker tick error'
    every 500 ms forever with no recovery."""

    def test_tick_desync_fires_on_desync_and_returns(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            worker, clock, ring, box = _make_worker(tmpdir)
            clock.anchor(rtp_timestamp=0, utc=900_000_000.0)
            fired = []
            worker._on_desync = lambda: fired.append(1)
            box["rtp"] = 2**30 + 10 * SR   # beyond the safe unwrap window
            worker._tick()                 # must not raise
            self.assertEqual(fired, [1])

    def test_tick_desync_without_callback_still_swallowed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            worker, clock, ring, box = _make_worker(tmpdir)
            clock.anchor(rtp_timestamp=0, utc=900_000_000.0)
            worker._on_desync = None
            box["rtp"] = 2**30 + 10 * SR
            worker._tick()                 # must not raise
```

- [ ] **Step 6: Run to verify failure**

```bash
uv run pytest tests/test_stream_anchoring.py -k Desync -v
uv run pytest tests/test_slot.py -k Desync -v
```
Expected: all four fail with `ka9q.slot_clock.SlotClockDesyncError` propagating (the `worker._on_desync = ...` assignments create the attribute, but nothing reads it yet).

- [ ] **Step 7: Implement F18**

`src/meteor_scatter/core/stream.py`:

(a) Extend the existing module-level import (line 46) to:

```python
from ka9q import SlotClock, SlotClockDesyncError
```

(b) Add `_reset_timing()` right after `on_stream_restored()` (~line 305), extracting that method's existing reset block:

```python
    def _reset_timing(self) -> None:
        """Drop the SlotClock anchor + ring so the next batch re-anchors
        from radiod's live channel_info (the StatusListener keeps it
        fresh — audit F19).  Shared recovery for on_stream_restored and
        the SlotClockDesyncError guards (audit F18); mirrors the reset
        SlotClock.advance() forces internally on the same exception.
        """
        with self._clock_lock:
            self._clock.reset()
        self._ring.clear()
        self._latest_rtp = None
        self._anchor_source = ""
        self._anchor_rtp = None
        # New RTP reference space -> the SlotWorker must re-seed its boundary.
        self._slot_worker.reset_boundary()
```

(c) In `on_stream_restored()`, replace the now-duplicated reset lines (`with self._clock_lock:` / `self._clock.reset()` / `self._ring.clear()` / `self._latest_rtp = None` / `self._anchor_source = ""` / `self._anchor_rtp = None` / `self._slot_worker.reset_boundary()` plus the boundary comment, ~lines 293-301) with a single `self._reset_timing()` call. Keep `self._channel_info = channel_info` before it and the "stream restored" log after it — behavior identical.

(d) In `on_samples()` (~line 230), wrap the existing `with self._clock_lock:` block (the anchor branch + `start_off = self._clock.offset_of_rtp(batch_first_rtp)`, currently lines 230-245) in a try/except — indent the block one level, leaving `self._ring.push(...)` and the lines after it outside the `try`:

```python
        try:
            with self._clock_lock:
                if not self._clock.anchored:
                    anchor_utc, source = self._anchor_utc_for(batch_first_rtp, n)
                    if anchor_utc is None:
                        return
                    self._clock.anchor(batch_first_rtp, anchor_utc)
                    self._anchor_source = source
                    # The fixed RTP reference for the ring + the slide-follow
                    # re-pin (see _anchor_utc_now).  Set once; only changes on a
                    # genuine stream restart (on_stream_restored resets the clock).
                    self._anchor_rtp = batch_first_rtp
                    logger.info(
                        "%s %d Hz: SlotClock anchored via %s",
                        self._mode.upper(), self._frequency_hz, source,
                    )
                start_off = self._clock.offset_of_rtp(batch_first_rtp)
        except SlotClockDesyncError as exc:
            # F18: recover like SlotClock.advance() — drop the anchor (and
            # the ring: its offsets live in the dead reference space) and
            # re-anchor on the next batch, instead of letting the desync
            # propagate up through MultiStream's receive thread.
            logger.error(
                "%s %d Hz: SlotClock desync in on_samples — %s; dropping "
                "anchor + ring to force a clean re-anchor",
                self._mode.upper(), self._frequency_hz, exc,
            )
            self._reset_timing()
            return
```

(Lock-safety note, verify when editing: `self._clock_lock` is a plain `threading.Lock` (stream.py:98), not an RLock — the `except` body runs after the `with` block has released it, so `_reset_timing()` re-acquiring it is safe. Do NOT move `_reset_timing()` inside the `with`.)

(e) In the `SlotWorker(...)` construction inside `ChannelSink.__init__` (~line 107), add one kwarg:

```python
            on_desync=self._reset_timing,
```

`src/meteor_scatter/core/slot.py`:

(f) Extend the module-level import (`from ka9q import SlotClock`) to:

```python
from ka9q import SlotClock, SlotClockDesyncError
```

(g) In `SlotWorker.__init__`, add a keyword parameter after `get_anchor_utc_now` (defaulted, so existing constructions/tests stay valid):

```python
        on_desync: Optional[Callable[[], None]] = None,
```

and store it next to the other callback attributes:

```python
        # audit F18: invoked when offset_of_rtp() raises
        # SlotClockDesyncError during harvest — ChannelSink._reset_timing,
        # the same full anchor+ring reset used on stream restore.
        self._on_desync = on_desync
```

(h) In `_tick()` (~line 180), wrap the `with self._clock_lock:` harvest block (the `if not self._clock.anchored: return` check through the end of the `while True:` boundary loop, currently lines ~183-207) in a try/except, leaving the `for start_off, start_utc in harvested:` extraction loop outside the `try`:

```python
        try:
            with self._clock_lock:
                if not self._clock.anchored:
                    return
                latest_off = self._clock.offset_of_rtp(latest_rtp)
                # ... existing boundary-seeding comment + code unchanged ...
                # ... existing while True: harvest loop unchanged ...
        except SlotClockDesyncError as exc:
            logger.error(
                "%s %d Hz: SlotClock desync in harvest — %s; requesting "
                "anchor reset (audit F18)",
                self._mode.upper(), self._frequency_hz, exc,
            )
            if self._on_desync is not None:
                self._on_desync()
            return
```

(Everything between `if not self._clock.anchored:` and the end of the `while True:` loop is kept byte-identical, just indented one level. `self._on_desync()` acquires `_clock_lock` — safe, the `with` has already released it.)

- [ ] **Step 8: Verify and commit F18**

```bash
uv run pytest tests/test_stream_anchoring.py tests/test_slot.py -v
git add src/meteor_scatter/core/stream.py src/meteor_scatter/core/slot.py tests/test_stream_anchoring.py tests/test_slot.py
git commit -m "fix(timing): guard SlotClock.offset_of_rtp() against desync — reset anchor+ring and re-anchor (audit F18)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

- [ ] **Step 9: Bump ka9q-python floor to 3.22.0**

In `pyproject.toml` line 15, change `"ka9q-python>=3.18.0",` to `"ka9q-python>=3.22.0",` (and update the stale `>=3.18.0` mention in the `[tool.uv.sources]` comment block, ~line 44, to `>=3.22.0`). Then:

```bash
uv lock --upgrade-package ka9q-python
uv sync --extra dev
grep -A2 'name = "ka9q-python"' uv.lock | head -3   # expect: version = "3.22.0"
# 3.22.0 ValidationError safety check (Global Constraints): every
# RadiodControl construction must carry client_id (or an explicit
# destination at its create/ensure call sites).
grep -rn "RadiodControl(" src/ scripts/ 2>/dev/null
# Recon: exactly one construction, receiver_manager.py:188, and it passes
# client_id="meteor-scatter" — if the grep shows anything else without
# client_id, add client_id before committing (3.22.0 raises otherwise).
uv run pytest tests/ -q 2>&1 | tail -5   # compare against Step 0 baseline
git add pyproject.toml uv.lock
git commit -m "build: require ka9q-python >=3.22.0 (2026-08 audit remediation release)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

**Deploy note:** commits only — the user deploys/restarts the live meteor-scatter instances.

---

### Task 2: psk-recorder — guard offset_of_rtp() (F18), bump to ka9q-python 3.22.0

**Repo:** `/opt/git/sigmond/psk-recorder`. Two commits: F18, bump. Identical design to Task 1's F18 (the two repos are structural siblings — same `ChannelSink`/`SlotWorker` shape, verified in clients.md), with two psk-only differences: (1) the unguarded call sites are `src/psk_recorder/core/stream.py:250` and `src/psk_recorder/core/slot.py:266`; (2) psk-recorder already has a full-reset precedent method, `_on_wallclock_timing_fault` (stream.py:317-344), whose reset block is byte-identical to `on_stream_restored`'s — extract `_reset_timing()` and rewire **both** to it. psk-recorder already wires StatusListener (no F19 here).

**Files:**
- Modify: `src/psk_recorder/core/stream.py`, `src/psk_recorder/core/slot.py`
- Modify: `tests/test_stream_anchoring.py`, `tests/test_slot.py` (append)
- Modify: `pyproject.toml`, `uv.lock`

- [ ] **Step 0: Baseline**

```bash
cd /opt/git/sigmond/psk-recorder && git status --short   # expect clean
uv run pytest tests/ -q 2>&1 | tail -5
```

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_stream_anchoring.py` the same `TestDesyncRecovery` class as Task 1 Step 5, unchanged except it reuses **this** repo's helpers (`_make_sink` builds an ft8 sink here; `SR = 12000` likewise; the `_anchor` helper, `_FakeQuality`, `_FakeChannelInfo`, `_NoAuthority`, `_cleanup_sink` all exist in this file with the same names — verified). Append to `tests/test_slot.py` the same `DesyncGuardTests` class as Task 1 Step 5 verbatim (`_make_worker` exists here with the same signature; note psk's `_make_worker` default `decoder_kind` is `"decode_ft8"`, which only affects `_reap_finished`, a no-op with no pending procs).

- [ ] **Step 2: Run to verify failure**

```bash
uv run pytest tests/test_stream_anchoring.py -k Desync -v
uv run pytest tests/test_slot.py -k Desync -v
```
Expected: all four fail with `SlotClockDesyncError` propagating.

- [ ] **Step 3: Implement**

`src/psk_recorder/core/stream.py`:
- Extend the ka9q import (line 52) to `from ka9q import SlotClock, SlotClockDesyncError`.
- Add the same `_reset_timing()` method as Task 1 Step 7(b), placed after `on_stream_restored()`.
- Rewire `on_stream_restored()` (reset block at ~lines 304-311) AND `_on_wallclock_timing_fault()` (reset block at ~lines 330-336) to call `self._reset_timing()` — each keeps its own surrounding lines (`self._channel_info = channel_info` / the respective log messages) unchanged.
- Wrap `on_samples()`'s `with self._clock_lock:` block (~lines 235-250, ending at `start_off = self._clock.offset_of_rtp(batch_first_rtp)`) in the same try/except as Task 1 Step 7(d) — `ft8`/`ft4` mode strings come from `self._mode` so the code is literally identical.
- Add `on_desync=self._reset_timing,` to the `SlotWorker(...)` construction (~line 107).

`src/psk_recorder/core/slot.py`:
- Extend the ka9q import (`from ka9q import SlotClock`, line ~31) to include `SlotClockDesyncError`.
- Add the defaulted `on_desync: Optional[Callable[[], None]] = None` parameter (after `on_timing_fault` in `SlotWorker.__init__`) and store `self._on_desync = on_desync` next to `self._on_timing_fault`.
- Wrap `_tick()`'s `with self._clock_lock:` harvest block (~lines 264-287, containing `latest_off = self._clock.offset_of_rtp(latest_rtp)`) in the same try/except as Task 1 Step 7(h), leaving the `for start_off, start_utc in harvested:` loop (with its `_wallclock_guard` logic) outside the `try`.

- [ ] **Step 4: Verify and commit F18**

```bash
uv run pytest tests/test_stream_anchoring.py tests/test_slot.py -v
uv run pytest tests/ -q 2>&1 | tail -5   # no new failures vs baseline
git add src/psk_recorder/core/stream.py src/psk_recorder/core/slot.py tests/test_stream_anchoring.py tests/test_slot.py
git commit -m "fix(timing): guard SlotClock.offset_of_rtp() against desync — reset anchor+ring and re-anchor (audit F18)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

- [ ] **Step 5: Bump ka9q-python floor to 3.22.0**

Same procedure as Task 1 Step 9: `pyproject.toml` line 15 `>=3.18.0` → `>=3.22.0` (plus the `[tool.uv.sources]` comment ~line 44), `uv lock --upgrade-package ka9q-python`, `uv sync --extra dev`, verify lock shows 3.22.0, client_id check (`grep -rn "RadiodControl(" src/` — recon: one construction, `receiver_manager.py:193`, passes `client_id="psk-recorder"`), full suite vs baseline, then:

```bash
git add pyproject.toml uv.lock
git commit -m "build: require ka9q-python >=3.22.0 (2026-08 audit remediation release)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

**Deploy note:** commits only — the user deploys/restarts the live psk-recorder instances.

---

### Task 3: hf-timestd — migrate to set_filter() (F15), rtp_to_wallclock → rtp_to_utc (F16), delete dead imports (F21), bump to ka9q-python 3.22.0

**Repo:** `/opt/git/sigmond/hf-timestd`. Four commits: F15, F16, F21, bump. **F20 (audio_streamer.py's hand-rolled RTP-only multicast join) is explicitly OUT of scope** — optional consolidation, deferred.

**Files:**
- Modify: `src/hf_timestd/core/stream_recorder_v2.py` (F15: `_set_filter_edges`, lines 884-911)
- Create: `tests/test_set_filter_edges.py` (F15)
- Modify: `src/hf_timestd/__init__.py` (F16: lines 92 + `__all__`)
- Modify: `src/hf_timestd/core/core_recorder_v2.py` (F16: 6 lazy-import sites; F21: line 1444)
- Modify: `tests/test_core_recorder_t6_fine_integration.py` (F16: patch targets)
- Modify: `scripts/inspect_channels_full.py` (F21: line 2)
- Modify: `scripts/verify_ensure_behavior.py`, `scripts/wwvb_live_tap.py`, `scripts/test_real_data_pipeline.py` (3.22.0 client_id safety)
- Modify: `pyproject.toml`, `uv.lock` (bump)

- [ ] **Step 0: Baseline**

```bash
cd /opt/git/sigmond/hf-timestd && git status --short   # expect clean
uv run pytest tests/ -q 2>&1 | tail -5                 # record — suite is large; pre-existing failures are report-only
```

- [ ] **Step 1: F15 — write the failing tests**

Context (verified): `_set_filter_edges()` (stream_recorder_v2.py:884-911) hand-builds a TLV packet via `from ka9q.control import encode_int, encode_double, encode_eol, CMD` + `from ka9q.types import StatusType` + `import secrets`, then `self._control.send_command(cmdbuffer)`. The audit (clients.md § Step 2 item 1) verified `RadiodControl.set_filter(ssrc, low_edge=, high_edge=, kaiser_beta=)` (ka9q/control.py:1217) sends the identical `LOW_EDGE`/`HIGH_EDGE` TLV field set — different field order, which is irrelevant to radiod's tag-keyed TLV decode loop — and `set_filter` omits any `None` field exactly like the hand-rolled code does. These are the **only** uses of `encode_*`/`CMD`/`send_command` in stream_recorder_v2.py (grep-verified: lines 893-907 only), so the internals imports vanish entirely with the rewrite.

`tests/test_set_filter_edges.py`:

```python
"""audit F15: _set_filter_edges must call the public
RadiodControl.set_filter() instead of hand-encoding LOW_EDGE/HIGH_EDGE
TLVs via ka9q.control internals (encode_int/encode_double/encode_eol/CMD).
set_filter() sends the identical TLV field set (order differs; radiod's
TLV decode is a tag-keyed linear scan, so order is irrelevant)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))

from hf_timestd.core.stream_recorder_v2 import (
    StreamRecorderConfig,
    StreamRecorderV2,
)


def _make_recorder(low_edge=None, high_edge=None):
    config = StreamRecorderConfig(
        ssrc=None, frequency_hz=7_850_000, sample_rate=24_000,
        preset='iq', encoding=4, agc_enable=0, gain=0.0,
        description='TEST_FILTER', output_dir=Path('/tmp'),
        receiver_grid='AA00aa', station_config={},
        archive=False,      # skip BinaryArchiveWriter init
        ring_seconds=0,     # skip RingBuffer create
        low_edge=low_edge, high_edge=high_edge,
    )
    control = MagicMock()
    return StreamRecorderV2(config=config, control=control), control


class TestSetFilterEdges(unittest.TestCase):

    def test_uses_public_set_filter(self):
        sr, control = _make_recorder(low_edge=-25_000, high_edge=25_000)
        sr._set_filter_edges(0xCAFE)
        control.set_filter.assert_called_once_with(
            0xCAFE, low_edge=-25_000.0, high_edge=25_000.0)
        control.send_command.assert_not_called()   # no hand-built TLV buffer

    def test_partial_edges_pass_none_through(self):
        # set_filter omits None fields — same wire behavior as the old code.
        sr, control = _make_recorder(low_edge=-3_000)
        sr._set_filter_edges(1)
        control.set_filter.assert_called_once_with(
            1, low_edge=-3_000.0, high_edge=None)

    def test_noop_when_unconfigured(self):
        sr, control = _make_recorder()
        sr._set_filter_edges(1)
        control.set_filter.assert_not_called()
        control.send_command.assert_not_called()

    def test_set_filter_failure_is_swallowed(self):
        # Best-effort semantics preserved: failures log a warning, never raise.
        sr, control = _make_recorder(low_edge=-3_000, high_edge=3_000)
        control.set_filter.side_effect = RuntimeError("radiod down")
        sr._set_filter_edges(1)   # must not raise
```

(Construction pattern `StreamRecorderV2(config=..., control=...)` and the `archive=False, ring_seconds=0` shortcut mirror `tests/test_stream_recorder_register_with.py`, verified; `StreamRecorderConfig.low_edge`/`.high_edge` are `Optional[float] = None` fields at stream_recorder_v2.py:101-102.)

- [ ] **Step 2: Run to verify failure**

```bash
uv run pytest tests/test_set_filter_edges.py -v
```
Expected: `test_uses_public_set_filter` and `test_partial_edges_pass_none_through` fail (`set_filter` never called; `send_command` called); the other two pass.

- [ ] **Step 3: F15 — implement**

Replace the entire `_set_filter_edges` body (stream_recorder_v2.py:884-911) with:

```python
    def _set_filter_edges(self, ssrc: int):
        """Send filter edge commands to radiod if configured.

        Uses the public RadiodControl.set_filter() (audit F15) — it sends
        the same LOW_EDGE/HIGH_EDGE TLV fields the old hand-rolled buffer
        did (field order differs; radiod's TLV decoder is a tag-keyed
        linear scan, so order is irrelevant) and drops this file's reach
        into ka9q.control wire-encoding internals.
        """
        low = self.config.low_edge
        high = self.config.high_edge
        if low is None and high is None:
            return

        try:
            self._control.set_filter(
                ssrc,
                low_edge=float(low) if low is not None else None,
                high_edge=float(high) if high is not None else None,
            )
            logger.info(f"{self.config.description}: Set filter edges: low={low}, high={high}")
        except Exception as e:
            logger.warning(f"{self.config.description}: Failed to set filter edges: {e}")
```

- [ ] **Step 4: F15 — verify and commit**

```bash
uv run pytest tests/test_set_filter_edges.py tests/test_stream_recorder_register_with.py -v
grep -rn "from ka9q.control import" src/ scripts/    # expect: no hits (the internals reach is gone)
git add src/hf_timestd/core/stream_recorder_v2.py tests/test_set_filter_edges.py
git commit -m "fix(stream): _set_filter_edges uses public RadiodControl.set_filter (audit F15)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

- [ ] **Step 5: F16 — migrate rtp_to_wallclock → rtp_to_utc**

Executable sites (verified by grep 2026-08-13; everything else is comment/docstring prose, which stays — the historical name in prose is not a call site):

1. `src/hf_timestd/__init__.py:92` — replace:

```python
# ka9q timing functions (GPS_TIME/RTP_TIMESNAP support)
from ka9q import rtp_to_wallclock, parse_rtp_header
```

with:

```python
# ka9q timing functions (GPS_TIME/RTP_TIMESNAP support)
from ka9q import rtp_to_utc, parse_rtp_header

# Legacy alias for hf-timestd's own downstream importers ("rtp_to_wallclock"
# is deprecated in ka9q-python — audit F16).  Bound directly to rtp_to_utc
# so it never routes through ka9q's DeprecationWarning wrapper.
rtp_to_wallclock = rtp_to_utc
```

and in `__all__` (line ~134), add `"rtp_to_utc",` immediately before the existing `"rtp_to_wallclock",` entry (which stays — it is hf-timestd's own public compat surface, per clients.md's hf-timestd matrix row for `__init__.py:89,92`).

2. `src/hf_timestd/core/core_recorder_v2.py` — six function-local lazy imports, at lines 2388, 2433, 2557, 2773, 3034, 4064. At each site, change:

```python
            from ka9q.rtp_recorder import rtp_to_wallclock
```

to:

```python
            from ka9q.rtp_recorder import rtp_to_utc
```

and rename the bound name in the call expression immediately following (e.g. line 2389 `wall = rtp_to_wallclock(` → `wall = rtp_to_utc(`; same mechanical rename at 2434, 2558, 2774, 3035, and 4068). Enumerate with `grep -n "from ka9q.rtp_recorder import rtp_to_wallclock" src/hf_timestd/core/core_recorder_v2.py` and fix every hit — six expected. Keep the imports function-local (they are lazy on purpose).

3. `tests/test_core_recorder_t6_fine_integration.py` — replace **every** `patch('ka9q.rtp_recorder.rtp_to_wallclock'` with `patch('ka9q.rtp_recorder.rtp_to_utc'` (14 occurrences at recon; `grep -c "ka9q.rtp_recorder.rtp_to_wallclock" tests/test_core_recorder_t6_fine_integration.py` must be 0 afterwards). These patches work because the production code imports the name at call time from the `ka9q.rtp_recorder` module namespace — the mechanism is unchanged, only the attribute name moves.

- [ ] **Step 6: F16 — verify and commit**

```bash
uv run pytest tests/test_core_recorder_t6_fine_integration.py tests/test_native_anchor.py -v
# Executable references all migrated — remaining hits must be prose or the __init__ alias:
grep -rn "rtp_to_wallclock" src/ scripts/ tests/ | grep -v archive | grep -v "^\s*#" | grep -v '"""'
uv run python -c "import hf_timestd; assert hf_timestd.rtp_to_wallclock is hf_timestd.rtp_to_utc; print('alias OK')"
git add src/hf_timestd/__init__.py src/hf_timestd/core/core_recorder_v2.py tests/test_core_recorder_t6_fine_integration.py
git commit -m "refactor(timing): migrate rtp_to_wallclock -> rtp_to_utc at every executable site (audit F16)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

- [ ] **Step 7: F21 — delete the two dead imports (verify still dead first)**

```bash
grep -n "StatusType" src/hf_timestd/core/core_recorder_v2.py   # expect exactly one hit: line 1444 (the import itself)
grep -n "Encoding" scripts/inspect_channels_full.py            # expect: line 2 (import) + a print-header string literal only
```

(Recon confirms both; the adjacent `from ka9q import RadiodStream, Encoding` at core_recorder_v2.py:1443 is **live** — `Encoding` is used later in the T6 block — do not touch it.) Then:

- `src/hf_timestd/core/core_recorder_v2.py:1444`: delete the line `from ka9q.types import StatusType`.
- `scripts/inspect_channels_full.py:2`: change `from ka9q import discover_channels, Encoding` to `from ka9q import discover_channels`.

```bash
grep -c "StatusType" src/hf_timestd/core/core_recorder_v2.py   # expect 0
uv run pytest tests/ -q 2>&1 | tail -5                          # no new failures vs baseline
git add src/hf_timestd/core/core_recorder_v2.py scripts/inspect_channels_full.py
git commit -m "chore: delete two dead ka9q imports (audit F21)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

- [ ] **Step 8: Bump ka9q-python floor to 3.22.0 + 3.22.0 client_id safety for scripts**

In `pyproject.toml` line 30, change `"ka9q-python>=3.21.0",` to `"ka9q-python>=3.22.0",` and extend its trailing comment with `; 3.22.0 derives per-client destinations in create_channel and raises ValidationError when neither client_id nor destination is available`. Also update the `>=3.21.0` mention in the `[tool.uv.sources]` comment (~line 105).

3.22.0 safety sweep (Global Constraints): `grep -rn "RadiodControl(" src/ scripts/`. Recon: production paths all pass `client_id="hf-timestd"` (channel_manager.py:77, core_recorder_v2.py:224-225, stream/stream_manager.py:329-330 — verified). Three operator scripts construct bare controls **and** then create/ensure without a destination, which 3.22.0 turns from silent no-op into `ValidationError` — add a distinct `client_id` to each so their throwaway channels derive their own group instead of colliding with production's:

- `scripts/verify_ensure_behavior.py:5`: `CTL = RadiodControl('239.192.152.141')` → `CTL = RadiodControl('239.192.152.141', client_id="hf-timestd-verify")`
- `scripts/wwvb_live_tap.py:107`: `control = RadiodControl(args.radiod)` → `control = RadiodControl(args.radiod, client_id="hf-timestd-wwvb-tap")`
- `scripts/test_real_data_pipeline.py:144`: `control = RadiodControl(status_address)` → `control = RadiodControl(status_address, client_id="hf-timestd-pipeline-test")` (check first whether its `create_channel` call at line 155 already passes `destination=` — if it does, the legacy path still works, but add the client_id anyway for consistency)

The remaining bare constructions (`scripts/cleanup_channels.py`, `scripts/cleanup_all.py`, `scripts/monitor_radiod_health.py`, `scripts/verify_pipeline.sh`'s inline snippet) only discover/remove — unaffected by the F5 guard; leave them.

```bash
uv lock --upgrade-package ka9q-python
uv sync --extra dev
grep -A2 'name = "ka9q-python"' uv.lock | head -3   # expect: version = "3.22.0"
uv run pytest tests/ -q 2>&1 | tail -5              # compare vs baseline
git add pyproject.toml uv.lock scripts/verify_ensure_behavior.py scripts/wwvb_live_tap.py scripts/test_real_data_pipeline.py
git commit -m "build: require ka9q-python >=3.22.0; scripts pass client_id (F5 ValidationError safety)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

**Deploy note:** commits only — the user deploys/restarts the live hf-timestd instance.

---

### Task 4: wspr-recorder — rtp_to_wallclock → rtp_to_utc (F16), bump to ka9q-python 3.22.0 (with pre-existing dirty uv.lock handling)

**Repo:** `/opt/git/sigmond/wspr-recorder`. Two commits: F16, bump. **This repo is NOT clean:** `uv.lock` has been modified-but-uncommitted since 2026-08-11 (`git status` shows ` M uv.lock`, 74 insertions / 52 deletions). The task must NOT silently absorb it.

- [ ] **Step 0: Quarantine the pre-existing uv.lock drift, then baseline**

```bash
cd /opt/git/sigmond/wspr-recorder
SCRATCH="${SCRATCHPAD:-/tmp}"   # your session scratchpad dir, /tmp fallback
git diff uv.lock > "$SCRATCH/wspr-uvlock-preexisting.diff"
git stash push -m "pre-existing uv.lock drift (observed since 2026-08-11, pre-remediation)" uv.lock
git status --short   # expect clean now
uv run pytest tests/ -q 2>&1 | tail -5   # baseline (~361 tests, pytest-asyncio auto mode)
```

**Examine the saved diff before proceeding.** Plan-time recon (2026-08-13) found it contains ONLY a routine lock refresh: `ka9q-python` (editable `../ka9q-python`) 3.20.0 → 3.20.1, `numpy` 2.5.1 → 2.5.2 (plus the accompanying sdist/wheel URL+hash churn), and an `exceptiongroup` dependency-marker widening (`typing-extensions` marker dropped its `python_full_version < '3.11'` guard) — no source changes, no dependency-spec changes. **Decision rule:** if your examination matches that description, the fresh `uv lock` in Step 4 strictly supersedes it — `git stash drop` at the end of Step 4 and record the summary (and the saved diff path) in the task report. If it contains ANYTHING else, STOP: leave the stash in place, do not proceed with the bump, and report the full diff to the controller/user for a ruling.

- [ ] **Step 1: F16 — flip the test patch targets first (they become the failing tests)**

The production code imports `rtp_to_wallclock` **inside function bodies on every call** (deliberate — clients.md flags that `tests/test_band_recorder_ring.py` monkeypatches the `ka9q` module attribute and would silently stop working if the import were hoisted; keep the imports function-local). Because `ka9q.rtp_to_wallclock` and `ka9q.rtp_to_utc` are distinct module attributes bound to the same function, patching the *new* name while the code still imports the *old* one makes these tests fail — a genuine red phase.

Update patch targets only (grep-driven; do NOT touch `correlation_source` string assertions):

```bash
grep -rn 'rtp_to_wallclock' tests/
```

- `tests/test_band_recorder_ring.py`: both `monkeypatch.setattr(ka9q, "rtp_to_wallclock", ...)` calls (lines 477 and 525 at recon) → `monkeypatch.setattr(ka9q, "rtp_to_utc", ...)`. Renaming the local `fake_rtp_to_wallclock` helper to `fake_rtp_to_utc` is optional cosmetics; the setattr target is what matters.
- `tests/test_sync_strategy.py`: every `mock.patch("ka9q.rtp_to_wallclock", ...)` (line 292 and the sibling in `test_channel_info_without_authority_uses_rtp_to_wallclock`) → `mock.patch("ka9q.rtp_to_utc", ...)`. **Leave the assertions `strategy.correlation_source == "rtp_to_wallclock+authority"` (and `"rtp_to_wallclock"`) unchanged** — see Step 2's label ruling.

```bash
uv run pytest tests/test_band_recorder_ring.py -k SlideFollow -v    # expect failures (patch no longer intercepts)
uv run pytest tests/test_sync_strategy.py -k ChannelInfo -v          # expect failures (falls through to authority/wall-clock source)
```

- [ ] **Step 2: F16 — migrate the three production call sites**

Ruling recorded here (Global Constraints): the `correlation_source` label strings `"rtp_to_wallclock"`/`"rtp_to_wallclock+authority"` are observable status/journal values (set at `sync_strategy.py:263-266`, enumerated in the comment at line 135) — renaming them is a production-behavior change outside F16's scope, so they are **kept**. Only the imported callable, the calls, and log/debug messages change.

1. `wspr_recorder/sync_strategy.py` (~lines 252-269): change `from ka9q import rtp_to_wallclock` → `from ka9q import rtp_to_utc`; `utc_sec = rtp_to_wallclock(` → `utc_sec = rtp_to_utc(`; the warning at line 269 `"rtp_to_wallclock raised at correlation: %s"` → `"rtp_to_utc raised at correlation: %s"`. Add above the `source =` assignment:

```python
                    # Label strings predate the ka9q rename (rtp_to_wallclock
                    # -> rtp_to_utc); kept stable for status/journal consumers.
```

2. `wspr_recorder/band_recorder.py` `_anchor_utc_now` (~lines 467-473): `from ka9q import rtp_to_wallclock` → `from ka9q import rtp_to_utc`; `cur = rtp_to_wallclock(` → `cur = rtp_to_utc(`; debug message `"%s: anchor rtp_to_wallclock raised: %s"` → `"%s: anchor rtp_to_utc raised: %s"`. Update the docstring mention at line ~457 (`rtp_to_wallclock returns None` → `rtp_to_utc returns None`).

3. `wspr_recorder/band_recorder.py` `_on_minute_boundary` (~lines 532-539): same import/call rename (`ref_sec = rtp_to_utc(`); update the comment at ~line 522 (`rtp_to_wallclock reads the` → `rtp_to_utc reads the`).

Comment-only mentions elsewhere (`__main__.py:278`, `band_recorder.py:267`, sync_strategy docstrings at 70/130/145/178/225-227) may be updated to `rtp_to_utc` where they describe the *call* — mechanical, optional; do not touch the line-135 enumeration of label values (those labels really are still `rtp_to_wallclock*`).

- [ ] **Step 3: F16 — verify and commit**

```bash
uv run pytest tests/test_band_recorder_ring.py tests/test_sync_strategy.py -v
grep -rn "rtp_to_wallclock" wspr_recorder/ | grep -v "correlation_source\|Label strings\|+authority"   # remaining hits must be label-value strings/comments only
uv run pytest tests/ -q 2>&1 | tail -5    # no new failures vs baseline
git add wspr_recorder/sync_strategy.py wspr_recorder/band_recorder.py tests/test_band_recorder_ring.py tests/test_sync_strategy.py
git commit -m "refactor(timing): migrate rtp_to_wallclock -> rtp_to_utc at every call site; labels kept stable (audit F16)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

- [ ] **Step 4: Bump ka9q-python floor to 3.22.0; resolve the stash**

In `pyproject.toml` line 29, change `"ka9q-python>=3.20.0",` to `"ka9q-python>=3.22.0",` (and update the stale `>=3.14.0` mention in the `[tool.uv.sources]` comment, ~line 43, to `>=3.22.0`). Then:

```bash
uv lock --upgrade-package ka9q-python
uv sync --extra dev
grep -A2 'name = "ka9q-python"' uv.lock | head -3   # expect: version = "3.22.0"
# 3.22.0 client_id safety (Global Constraints): recon shows one construction,
# receiver_manager.py:347-349, passing client_id="wspr-recorder" — verify:
grep -rn "RadiodControl(" wspr_recorder/ | grep -v "^Binary"
uv run pytest tests/ -q 2>&1 | tail -5              # compare vs baseline
git add pyproject.toml uv.lock
git commit -m "build: require ka9q-python >=3.22.0 (2026-08 audit remediation release)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
# Stash resolution — ONLY if Step 0's examination confirmed "older lock
# refresh, nothing else" (it is superseded by the uv lock just committed):
git stash drop
```

Report requirement: the task report must state what the stashed diff contained (the Step 0 summary + saved diff path), and that it was dropped as superseded — or, in the other branch, that the stash was left in place and why.

**Deploy note:** commits only — the user deploys/restarts the live wspr-recorder instances.

---

### Task 5: ka9q-python — rtp_to_wallclock emits DeprecationWarning (F16, ka9q side) — GATED on Tasks 3+4

**Repo:** `/opt/git/sigmond/ka9q-python`. One commit. **GATE (hard precondition):** hf-timestd's and wspr-recorder's F16 migration commits must be on their `main` branches first — they were the two lagging clients still calling the alias in production, and the alias must not start warning under their feet.

Recon fact that widens this task beyond the one-line alias (found 2026-08-13, must not be skipped): **ka9q-python itself still calls `rtp_to_wallclock` on three internal per-packet paths** — `ka9q/stream.py:41` (import) + `:590` (call in `RadiodStream`'s receive loop), `ka9q/multi_stream.py:55` (import) + `:515` (call in `MultiStream`'s receive loop), and `ka9q/rtp_recorder.py:482` (`RTPRecorder`). If the alias becomes a warning wrapper without migrating these, the library deprecation-warns *about itself* and pays `warnings.warn` filter-machinery overhead per received packet on every client's hot path. Migrate all three to `rtp_to_utc` in the same commit.

**Files:**
- Create: `tests/test_rtp_to_wallclock_deprecated.py`
- Modify: `ka9q/rtp_recorder.py` (alias at line 247 → wrapper; internal call at 482; `import warnings` at top)
- Modify: `ka9q/stream.py`, `ka9q/multi_stream.py` (internal callers → `rtp_to_utc`)
- Modify: `CHANGELOG.md`
- Possibly regenerate: `tests/client_usage_manifest.json` (see Step 5)

- [ ] **Step 0: Verify the gate + baseline**

```bash
git -C /opt/git/sigmond/hf-timestd log --oneline -8      # must show the F16 migration commit
git -C /opt/git/sigmond/wspr-recorder log --oneline -8   # must show the F16 migration commit
# Belt and braces — no production import of the alias remains in either repo:
grep -rn "import rtp_to_wallclock" /opt/git/sigmond/hf-timestd/src /opt/git/sigmond/wspr-recorder/wspr_recorder
# expect: zero hits.  If either check fails, STOP — this task is gated.
git status --short    # expect clean
uv run pytest -q 2>&1 | tail -5                          # baseline (known environmental failures per Global Constraints)
```

- [ ] **Step 1: Write the failing tests**

`tests/test_rtp_to_wallclock_deprecated.py` (channel construction mirrors `tests/test_rtp_recorder.py`'s `_channel` helper; the `wallclock_hint_sec` pin makes the wrap-epoch pick deterministic without patching `time.time`):

```python
"""audit F16 (ka9q side): rtp_to_wallclock must emit DeprecationWarning
while staying signature- and behavior-identical to rtp_to_utc.  The two
lagging clients (hf-timestd, wspr-recorder) migrated first — this warning
is the migration signal for any remaining out-of-tree callers."""

import inspect
import warnings

import pytest

from ka9q.discovery import ChannelInfo
from ka9q.rtp_recorder import rtp_to_utc, rtp_to_wallclock

GPS_UTC_OFFSET = 315964800
BILLION = 1_000_000_000
GPS_TIME_NS = 1234567890000000000


def _channel(sample_rate=48000, gps_time_ns=GPS_TIME_NS, rtp_timesnap=1000):
    return ChannelInfo(
        ssrc=1234, preset="test", sample_rate=sample_rate,
        frequency=100.0, snr=0.0, multicast_address="239.1.2.3",
        port=5004, gps_time=gps_time_ns, rtp_timesnap=rtp_timesnap,
    )


def _hint():
    # The channel's own wall-clock instant — pins wrap epoch k=0.
    return (GPS_TIME_NS + BILLION * (GPS_UTC_OFFSET - 18)) / BILLION


def test_emits_deprecation_warning():
    with pytest.warns(DeprecationWarning, match="rtp_to_utc"):
        rtp_to_wallclock(2000, _channel(), wallclock_hint_sec=_hint())


def test_result_identical_to_rtp_to_utc():
    ch = _channel()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        old = rtp_to_wallclock(2000, ch, wallclock_hint_sec=_hint())
    new = rtp_to_utc(2000, ch, wallclock_hint_sec=_hint())
    assert old is not None
    assert old == new


def test_none_path_preserved():
    # Same construction as tests/test_rtp_recorder.py::
    # test_rtp_to_wallclock_returns_none_when_timing_missing.
    ch = _channel(gps_time_ns=None, rtp_timesnap=None)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        assert rtp_to_wallclock(2000, ch) is None


def test_signature_renders_identically():
    # tests/client_usage_manifest.json snapshots str(inspect.signature) for
    # ka9q:rtp_to_wallclock and ka9q.rtp_recorder:rtp_to_wallclock — the
    # wrapper must render exactly like the old plain alias did.
    assert str(inspect.signature(rtp_to_wallclock)) == str(
        inspect.signature(rtp_to_utc))


def test_rtp_to_utc_does_not_warn():
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        rtp_to_utc(2000, _channel(), wallclock_hint_sec=_hint())


def test_internal_hot_paths_do_not_use_the_alias():
    """The library must not deprecation-warn about itself: the per-packet
    receive paths in stream.py / multi_stream.py / rtp_recorder.py call
    rtp_to_utc directly.  (hasattr checks: those two modules used to
    *import* the alias, so the module attribute existing at all proves
    the old wiring.  ka9q/__init__.py's public re-export is exempt and
    untouched.)"""
    import ka9q.multi_stream
    import ka9q.rtp_recorder
    import ka9q.stream
    assert not hasattr(ka9q.stream, "rtp_to_wallclock")
    assert not hasattr(ka9q.multi_stream, "rtp_to_wallclock")
    src = inspect.getsource(ka9q.rtp_recorder.RTPRecorder)
    assert "rtp_to_wallclock(" not in src
```

- [ ] **Step 2: Run to verify failure**

```bash
uv run pytest tests/test_rtp_to_wallclock_deprecated.py -v
```
Expected: `test_emits_deprecation_warning` fails (no warning today — plain alias) and `test_internal_hot_paths_do_not_use_the_alias` fails (`ka9q.stream`/`ka9q.multi_stream` import the alias today at stream.py:41 / multi_stream.py:55, so the module attribute exists, and `RTPRecorder`'s source at rtp_recorder.py:482 still calls it); the rest pass (the plain alias is already signature/behavior-identical).

- [ ] **Step 3: Implement**

(a) `ka9q/rtp_recorder.py` — add `import warnings` to the stdlib import block at the top (after `import time`, ~line 18). Replace the alias (lines 245-247):

```python
# Deprecated alias — kept so existing callers keep working.  Prefer
# ``rtp_to_utc`` (the reference is RTP/GPS, not the host wall clock).
rtp_to_wallclock = rtp_to_utc
```

with:

```python
def rtp_to_wallclock(
    rtp_timestamp: int,
    channel: ChannelInfo,
    wallclock_hint_sec: Optional[float] = None,
) -> Optional[float]:
    """Deprecated alias for :func:`rtp_to_utc` (renamed 2026-06-27).

    Identical signature and behavior.  Emits ``DeprecationWarning`` on
    every call (audit finding F16) — the in-tree sigmond clients migrated
    2026-08; this is the visible signal for any remaining callers.
    """
    warnings.warn(
        "ka9q rtp_to_wallclock() is deprecated; use rtp_to_utc() "
        "(same signature and behavior, renamed 2026-06-27)",
        DeprecationWarning,
        stacklevel=2,
    )
    return rtp_to_utc(rtp_timestamp, channel, wallclock_hint_sec)
```

(The parameter list, annotations, and return annotation are copied verbatim from `rtp_to_utc`'s def (rtp_recorder.py:127-131) so `str(inspect.signature(...))` renders identically to the manifest's snapshot: `(rtp_timestamp: int, channel: ka9q.discovery.ChannelInfo, wallclock_hint_sec: Optional[float] = None) -> Optional[float]`.)

(b) Migrate the three internal callers to `rtp_to_utc`:
- `ka9q/rtp_recorder.py:482`: `wallclock = rtp_to_wallclock(header.timestamp, self.channel)` → `wallclock = rtp_to_utc(header.timestamp, self.channel)`
- `ka9q/stream.py:41`: `from .rtp_recorder import RTPHeader, parse_rtp_header, rtp_to_wallclock` → `from .rtp_recorder import RTPHeader, parse_rtp_header, rtp_to_utc`; and `:590`: `wallclock = rtp_to_wallclock(header.timestamp, self.channel)` → `wallclock = rtp_to_utc(header.timestamp, self.channel)`
- `ka9q/multi_stream.py:55`: same import change; and `:515`: `wallclock = rtp_to_wallclock(header.timestamp, slot.channel_info)` → `wallclock = rtp_to_utc(header.timestamp, slot.channel_info)`

Leave `ka9q/__init__.py`'s re-export (lines 97, 163) exactly as is — the alias stays public. Leave `tests/test_rtp_recorder.py` untouched: its 15 `rtp_to_wallclock` calls now double as regression coverage that the deprecated alias still computes correctly (they will emit DeprecationWarnings in the suite output — expected noise, no `filterwarnings = error` is configured in this repo; note it in the commit message).

(c) `CHANGELOG.md` — insert under the top `# Changelog` heading, above `## 3.22.0`:

```markdown
## Unreleased

### Deprecated

- **`rtp_to_wallclock()` now emits `DeprecationWarning` on every call**
  (audit F16, completing the 2026-06-27 rename). Signature and behavior
  are unchanged — it delegates to `rtp_to_utc()`. The in-tree clients
  (hf-timestd, wspr-recorder) migrated to `rtp_to_utc` before this
  landed; internal callers (`RadiodStream`, `MultiStream`, `RTPRecorder`)
  were migrated in the same commit so the library never warns about
  itself on the per-packet path.
```

(No version bump in this task — a warning-only change; the next release cut decides the number.)

- [ ] **Step 4: Verify**

```bash
uv run pytest tests/test_rtp_to_wallclock_deprecated.py tests/test_rtp_recorder.py -v
uv run pytest tests/test_client_contract.py -q
uv run pytest -q 2>&1 | tail -5   # vs baseline; known environmental failures only
```
Expected: all green. `test_client_contract.py` in particular must pass **without** manifest regeneration — the wrapper's signature string is unchanged. If it fails on a rendering difference (the snapshot strings are Python-3.11-rendering-sensitive — always run via `uv run`), fix the wrapper's annotations to match `rtp_to_utc`'s verbatim; regenerating to paper over an unintended signature change is not allowed.

- [ ] **Step 5: Manifest refresh (client-import rows went stale in Tasks 3+4)**

`tests/client_usage_manifest.json` records which symbols each client imports; hf-timestd and wspr-recorder no longer import `rtp_to_wallclock`, so their rows are stale. Regenerate and inspect:

```bash
uv run python scripts/gen_client_manifest.py
git diff tests/client_usage_manifest.json
```

Expected diff: hf-timestd's `"ka9q"`/`"ka9q.rtp_recorder"` symbol lists swap `rtp_to_wallclock` → `rtp_to_utc`; wspr-recorder's `"ka9q"` list likewise; if **no** scanned repo still imports the alias, the `ka9q:rtp_to_wallclock` / `ka9q.rtp_recorder:rtp_to_wallclock` signature entries drop out entirely — acceptable, the new deprecation test now guards the alias's existence and signature instead. **Call the exact diff out in the commit message.** Caution: the generator rescans every repo under `/opt/git/sigmond`, so the diff may also pick up unrelated drift from other repos' recent development — review every hunk; if a hunk is surprising (a symbol appearing/disappearing that Tasks 1-4 don't explain), report it rather than silently committing it. Then:

```bash
uv run pytest tests/test_client_contract.py -q   # must be green against the fresh manifest
```

- [ ] **Step 6: Commit**

```bash
git add ka9q/rtp_recorder.py ka9q/stream.py ka9q/multi_stream.py tests/test_rtp_to_wallclock_deprecated.py CHANGELOG.md tests/client_usage_manifest.json
git commit -m "feat(timing)!: rtp_to_wallclock emits DeprecationWarning; internal hot paths use rtp_to_utc (audit F16)

Alias signature/behavior unchanged (manifest snapshot renders
identically); manifest regenerated — hf-timestd/wspr-recorder rows now
import rtp_to_utc (their F16 migrations landed first, gating this).
tests/test_rtp_recorder.py deliberately still exercises the alias and
now emits expected DeprecationWarnings in suite output.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

**Deploy note:** commits only — the version cut and any client redeploys are the user's call.

---

## Verification (whole plan)

- [ ] Each of the five repos: `git log --oneline -6` shows the expected commits on `main`, working tree clean (wspr-recorder additionally: stash resolved per Task 4's decision rule, with the outcome recorded in the report).
- [ ] Each client repo's `uv.lock` records `ka9q-python` `version = "3.22.0"`; each `pyproject.toml` floor is `>=3.22.0`.
- [ ] Each repo's test suite shows no new failures relative to its Step-0 baseline (report the baselines and finals side by side).
- [ ] `grep -rn "from ka9q.control import" /opt/git/sigmond/hf-timestd/src` → empty (F15's internals reach gone).
- [ ] `grep -rn "import rtp_to_wallclock" /opt/git/sigmond/hf-timestd/src /opt/git/sigmond/wspr-recorder/wspr_recorder /opt/git/sigmond/ka9q-python/ka9q` → empty (F16 complete on both sides; hf_timestd's own alias is an assignment, not an import, and ka9q's wrapper is a def).
- [ ] Nothing in this plan touched b1–b4 or restarted any service.
