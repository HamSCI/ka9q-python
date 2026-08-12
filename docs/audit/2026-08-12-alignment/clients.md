# Client Contract Matrix + No-Bypass Policy Sweep

**AUDIT_HEAD:** `cedec349f7b4212078de3e007d142b4c64d36546` (2026.08.12-1-trixie1)

This document has two parts, per the task brief:

1. A per-symbol contract matrix for the four sigmond-suite repos that are
   the brief's named importers (`hf-timestd`, `wspr-recorder`,
   `psk-recorder`, `meteor-scatter`) — every symbol each imports from
   `ka9q`, how it's used, and what "load-bearing behavior" a client
   depends on (the thing that breaks if ka9q-python's contract shifts).
   Task 11 cross-checks the generated API manifest against this matrix.
2. A no-bypass policy sweep across **every** repo under `/opt/git/sigmond`
   (not just the four named clients), classifying every lead as
   bypass / compliant / not-radiod-related. Findings feed Task 8.

All work is read-only outside `ka9q-python`; nothing under any client
repo was modified. Every grep hit below was opened and read in context —
per the brief, "a grep hit is a lead, not a finding."

---

## Step 1: Symbol matrix — the four named importers

Baseline: `ka9q/__init__.py`'s `__all__` (checked directly) contains:
`RadiodControl`, `allocate_ssrc`, `discover_channels`,
`discover_channels_native`, `discover_channels_via_control`,
`discover_radiod_services`, `ChannelInfo`, `StatusType`, `Encoding`,
`DemodType`, `WindowType`, `FrontendStatus`, `ChannelStatus`,
`PllStatus`, `FmStatus`, `SpectrumStatus`, `Filter2Status`,
`OpusStatus`, `decode_status_packet`, `Ka9qError`, `ConnectionError`,
`CommandError`, `ValidationError`, `RTPRecorder`, `RecorderState`,
`RTPHeader`, `RecordingMetrics`, `parse_rtp_header`, `rtp_to_utc`,
`rtp_to_wallclock`, `SlotClock`, `Slot`, `SlotClockDesyncError`,
`rtp_diff`, `RadiodStream`, `StreamQuality`, `GapSource`, `GapEvent`,
`PacketResequencer`, `RTPPacket`, `ResequencerStats`, `ManagedStream`,
`ManagedStreamStats`, `StreamState`, `MultiStream`, `SpectrumStream`,
`StatusListener`, `StatusListenerStats`, `generate_multicast_ip`,
`ChannelMonitor`, `BpskPpsCalibrator`, `PpsCalibrationResult`,
`NotchFilter500Hz`. `ka9q.compat.KA9Q_RADIO_COMMIT` is imported into
the package namespace (`__init__.py:207`) but omitted from `__all__` —
still reachable as `ka9q.KA9Q_RADIO_COMMIT` (attribute access doesn't
go through `__all__`; that list only gates `from ka9q import *`).
`ka9q.utils` and `ka9q.control`'s wire-encoding primitives
(`encode_int`/`encode_double`/`encode_eol`/`CMD`) are **not** imported
into `__init__.py` at all — reaching them requires an explicit
`ka9q.utils.*` / `ka9q.control.*` submodule import (see Step 2).

Scope note: `archive/**` and test-only imports were excluded from the
matrix below (legacy/deprecated code and test harnesses, not the live
production contract) except where noted inline.

### hf-timestd

| module | symbol | how used | load-bearing behavior |
|---|---|---|---|
| `src/hf_timestd/radiod_health.py:12` | `discover_channels` | radiod liveness probe — result used only via `len()`/`in` (SSRC membership) | must return `Dict[int, ChannelInfo]` (possibly empty) without raising against a live-but-empty radiod |
| `src/hf_timestd/__init__.py:89,92` | `discover_channels`, `ChannelInfo`, `RadiodControl`, `rtp_to_wallclock`, `parse_rtp_header` | re-exported at hf-timestd's own package top level (`discover_channels_via_control = discover_channels` back-compat alias; all five in hf-timestd's `__all__`) | these five must keep existing as public `ka9q.<name>` attributes — anything downstream importing them from `hf_timestd` breaks at import time otherwise |
| `src/hf_timestd/channel_manager.py:11` | `RadiodControl` | `RadiodControl(status_address, client_id=...)`; `.create_channel(...)`, `.verify_channel(ssrc, freq)`, `.tune(ssrc, frequency_hz, preset, sample_rate, encoding=int)`, `.set_output_encoding(ssrc, int)`, `.close()` | exact kwarg names/positions of `create_channel`/`tune`/`verify_channel`/`set_output_encoding`; `create_channel` must return a falsy/None-on-failure int SSRC; `client_id=` (per-client deterministic multicast derivation, "CONTRACT v0.3 §7") must keep working |
| `src/hf_timestd/channel_manager.py:11` | `discover_channels` | `Dict[int, ChannelInfo]`, iterated by `.keys()`/`.items()` | `ChannelInfo.frequency`/`.preset`/`.sample_rate` compared directly against requested config (1 Hz tolerance) to decide reuse vs. reconfigure |
| `src/hf_timestd/channel_manager.py:11,473-500` | `Encoding` | `_ENCODING_ALIASES` dict → `Encoding.F32LE/F32BE/S16LE/S16BE/OPUS/OPUS_VOIP`, sent as `int` to `tune`/`set_output_encoding` | the **integer values** must exactly match `ka9q-radio/src/rtp.h`'s `enum encoding` — a silent renumbering sends the wrong wire format to radiod with no exception raised |
| `src/hf_timestd/core/recording_session.py:26` | `ChannelInfo` | opaque pass-through to `rtp_receiver.register_callback`/`.update_channel_info` | no field access here — only needs to remain a passable object |
| `src/hf_timestd/core/recording_session.py:26`, `core/__init__.py:55` | `RTPHeader` | `.sequence`, `.timestamp`, `.ssrc`, `.payload_type` read in `_handle_rtp_packet` | field names/order of the `NamedTuple` must stay stable; payload-type ints (120/97/11, hand-decoded independently of ka9q's own decode) mis-decode IQ silently if radiod ever changes which payload-type number maps to which encoding |
| `src/hf_timestd/core/stream_recorder_v2.py:49` | `RadiodStream` | `RadiodStream(channel=channel_info, on_samples=..., samples_per_packet=200, resequence_buffer_size=128)`; `.start()`, `.stop()` (must return final `StreamQuality`), `.get_quality()` | `on_samples(samples: np.ndarray[complex64], quality: StreamQuality)` callback shape must not change; `samples_per_packet`/`resequence_buffer_size` semantics feed the resequencer's gap-detection heuristics (comment at `core_recorder_v2.py:1368` explicitly warns a mismatch "would skew" it) |
| `src/hf_timestd/core/stream_recorder_v2.py:49` | `ChannelInfo` | reads `.ssrc`, `.multicast_address`, `.port` (`getattr(...,5004)` default), `.gps_time`, `.rtp_timesnap` | field names fixed; `gps_time`/`rtp_timesnap` is the sole timing anchor for `BinaryArchiveWriter`/offset judge — `None` degrades silently (logged warning, timing seed skipped, not a crash) |
| `src/hf_timestd/core/stream_recorder_v2.py:49`, `core_recorder_v2.py:52` | `StreamQuality` | reads `.completeness_pct`, `.total_gaps_filled`, `.rtp_packets_lost`, `.batch_gaps[].duration_samples`, `.last_rtp_timestamp`, `.first_rtp_timestamp`, `.total_samples_delivered`, `.has_gaps` | every attribute is load-bearing — `archive_writer.write_samples(rtp_timestamp=quality.last_rtp_timestamp, ...)` feeds on-disk sample labeling directly; `first_rtp_timestamp + total_samples_delivered - len(samples)` is an arithmetic identity the code assumes holds exactly (this repo's own recent fix, `StreamQuality.copy() dropped delivered_rtp_start`, shows exactly how fragile this class of field is across ka9q-python revisions) |
| `src/hf_timestd/core/stream_recorder_v2.py:49,894`, `core_recorder_v2.py:52,196,1444` | `RadiodControl` | `.ensure_channel(**kwargs)` (`frequency_hz`, `preset`, `sample_rate`, `agc_enable`, `gain`, `destination`, `encoding`, `timeout`, `frequency_tolerance`, `lifetime`, plus phase-engine extras `reception_mode`/`target`/`null_targets`/`combining_method`), `.get_capabilities()` (`hasattr`-guarded), `.send_command(cmdbuffer)` | `ensure_channel` kwarg names/semantics are load-bearing for channel lifecycle (esp. `lifetime`, `frequency_tolerance`); must return object with `.ssrc`; `get_capabilities()["backend"] == "phase-engine"` string sentinel gates optional kwargs — a rename degrades silently (falls back to plain kwargs, not a crash) |
| `src/hf_timestd/core/core_recorder_v2.py:52` | `Encoding` | dict of `S16BE/S16LE/F32/F32LE/F32BE/F16/F16LE/F16BE/OPUS/NO_ENCODING` (10 of 13 members — the broadest `Encoding` surface in the repo) → `ensure_channel(encoding=...)`, default `Encoding.F32` | same wire-value stability requirement as `channel_manager.py`, at greater breadth |
| `src/hf_timestd/core/core_recorder_v2.py:52,1364` | `MultiStream` | one shared socket via `MultiStream(control=..., samples_per_packet=200, resequence_buffer_size=128)`; `recorder.register_with(self._multi)` → `multi.add_channel(...)` | `add_channel` must be idempotent for an already-provisioned SSRC (comment: "re-runs `ensure_channel` internally... cheap status probe"); "add-before-start" ordering is relied on; `on_stream_dropped`/`on_stream_restored` forwarded through — comments reference `MultiStream._handle_drop`/`_attempt_restore` internal method names directly, i.e. the client team has read ka9q-python internals to reason about behavior even though only the public `add_channel` is called |
| `src/hf_timestd/core/core_recorder_v2.py:1443,1552` | `RadiodStream` | two dedicated (non-`MultiStream`) instances for T6 BPSK-PPS calibration and WWVB_60, own socket/thread to avoid archive-flush blocking | `resequence_buffer_size=256` was deliberately widened from the 128 default ("gave 0.8s tolerance... too tight") — a change to `PacketResequencer`'s half-full-buffer-declares-loss heuristic invalidates this tuning and risks reintroducing a documented BPSK Costas-loop unlock bug |
| `src/hf_timestd/core/core_recorder_v2.py:2388,2433,2557,2773,3034,4064` (8 sites, via `ka9q.rtp_recorder`) | `rtp_to_wallclock` | `rtp_to_wallclock(rtp_timestamp, channel_info) -> Optional[float]` for chain-delay calibration / T5-T6 cross-checks / offset computation | this is a **deprecated alias** (`ka9q/rtp_recorder.py:247`: `rtp_to_wallclock = rtp_to_utc`, renamed 2026-06-27, no `DeprecationWarning` emitted) — depends on (a) the alias continuing to exist, (b) the GPS-epoch↔Unix math + 32-bit RTP-wraparound disambiguation staying bit-identical, (c) returning `None` (not raising) on a missing anchor — every call site wraps in `try/except`, so a signature break would be caught, but silent numeric drift in the formula would not |
| `src/hf_timestd/stream/stream_manager.py:15` | `discover_channels`, `RadiodControl`, `ChannelInfo` | wraps ka9q calls behind hf-timestd's own SSRC-hiding `StreamManager`/`StreamHandle`; `.ensure_channel(...)`, `.remove_channel(ssrc)` | `ensure_channel` must return `.ssrc`/`.multicast_address`/`.port`; `remove_channel(ssrc)` must not raise on an already-removed SSRC |
| `scripts/*.py` (9 CLI/debug tools: `verify_ensure_behavior`, `verify_channel_count`, `inspect_channels[_full]`, `cleanup_channels`/`cleanup_all`, `check_channels`, `list_radiod_channels`, `monitor_radiod_health`, `wwvb_live_tap`, `test_real_data_pipeline`) | `RadiodControl`, `discover_channels`, `ChannelInfo`, `Encoding`, `RadiodStream` | ad-hoc operator tooling — same shape dependencies as above, lower stakes (not the production data path) | grouped as one row per the brief's dedup guidance |

Two dead imports noted in passing (zero risk, flagged for completeness):
`core_recorder_v2.py:1444` imports `StatusType` but never references it;
`scripts/inspect_channels_full.py:2` imports `Encoding` but never uses
it (only prints the raw `.encoding` int).

### wspr-recorder

| module | symbol | how used | load-bearing behavior |
|---|---|---|---|
| `inspect_ka9q*.py` (repo-root dev scripts) | `RadiodControl` | `inspect.signature()`/`dir()` introspection only, not shipped code | none — scratch tooling |
| `wspr_recorder/sync_strategy.py:252` | `rtp_to_wallclock` | converts RTP timestamp + `ChannelInfo` → Unix-seconds UTC to correlate the first packet against the minute boundary | `Optional[float]` Unix-epoch-seconds return (not `datetime`); `None` is an expected fallback path; `wallclock_hint_sec=` only needs ±half-period accuracy for RTP-wraparound disambiguation |
| `wspr_recorder/band_recorder.py:467` (`_anchor_utc_now`) | `rtp_to_wallclock` | re-projects the fixed anchor RTP sample onto radiod's *current* UTC mapping every minute ("slide-follow") | assumes the function re-reads `channel_info.gps_time/rtp_timesnap` **live** each call — relies on `StatusListener` keeping that object fresh (see below); this is what makes slide-follow meaningfully track drift here |
| `wspr_recorder/band_recorder.py:532` (`_on_minute_boundary`) | `rtp_to_wallclock` | compares a grid-projected fresh anchor vs. the slide-followed label to detect a frozen/bad anchor | same float/None contract; called every minute per band — must be cheap and side-effect-free |
| `wspr_recorder/configurator.py:298` | `discover_radiod_services` | interactive setup wizard, `discover_radiod_services(timeout=...) or []` | list of dicts with `name`/`hostname`/`address`/`port`; must not raise on avahi absence (already wrapped defensively client-side too) |
| `wspr_recorder/receiver_manager.py:30` | `MultiStream`, `RadiodControl` | `RadiodControl(status, client_id="wspr-recorder")`; one `MultiStream` per `(mcast_addr, port)`; `.add_channel(...)`, `.start()`, `.prune_frequency()`, lifetime management | `ensure_channel()` → `ChannelInfo` with `.ssrc/.multicast_address/.port/.encoding/.frequency/.sample_rate`; `on_samples(samples, quality)` callback shape; `client_id=` deterministic multicast derivation ("ka9q-python ≥ 3.14.0" per comment); `prune_frequency()` must actually null buffers on removed slots (relied on to prevent a documented ~1.3 GB/h ring-buffer leak); `set_channel_lifetime` on an unknown SSRC must be a silent no-op |
| `wspr_recorder/receiver_manager.py:31` | `ChannelInfo` | type annotation only (`ChannelState.channel_info: Optional[ChannelInfo]`) | none beyond the above |
| `wspr_recorder/receiver_manager.py:357` | `StatusListener` | `.start()`, `.register_channel(info)`, `.unregister_channel(old_ssrc)` | assumes the listener mutates the **same** `ChannelInfo` object in place on every ~2 Hz STATUS broadcast — this is the mechanism `rtp_to_wallclock`'s slide-follow depends on; `unregister_channel` on an already-removed SSRC must not raise |
| `wspr_recorder/receiver_manager.py:598,668` | `discover_channels` | post-provisioning verify/retry sweep, `Dict[int, ChannelInfo]` compared against expected `.frequency`/`.sample_rate` | tolerant of missing entries (`channels.get(ssrc)`) |

**Internals reach:** none — every submodule-path import (`ka9q.discovery.discover_radiod_services`, `.ChannelInfo`, `.discover_channels`, `ka9q.status_listener.StatusListener`) is in `ka9q/__init__.py`'s `__all__`; these are redundant-but-fine explicit paths, not internals reaches.

**Test-time coupling worth flagging (not a contract break, but fragile):**
`tests/test_band_recorder_ring.py:468,517` does
`monkeypatch.setattr(ka9q, "rtp_to_wallclock", fake_...)`, which only
works because `band_recorder.py` imports `rtp_to_wallclock` **inside**
the function body on every call rather than at module top. If that
import were ever hoisted to module scope (a plausible future cleanup),
this test would silently stop exercising the patched behavior.

### psk-recorder

| module | symbol | how used | load-bearing behavior |
|---|---|---|---|
| `src/psk_recorder/configurator.py:256` | `discover_radiod_services` | identical pattern to wspr-recorder's configurator | same contract |
| `src/psk_recorder/core/receiver_manager.py:176,393` | `MultiStream`, `RadiodControl` | `RadiodControl(status, client_id="psk-recorder")`; one `MultiStream` per `(mcast_addr, port)` via `_add_sink_to_multi()`; `add_channel(..., timeout=..., on_stream_dropped=..., on_stream_restored=...)` | `ensure_channel`/`add_channel` timeout behavior must raise `TimeoutError` specifically (used by `_provision_one_with_retry` to distinguish "not yet verified, retry" from fatal errors — a different exception type would misclassify a fatal condition as retryable); `on_stream_restored` must fire only on a genuine radiod-restart-class event (≥15s silence + successful re-provision) — psk-recorder nukes in-flight decode state (`SlotClock` anchor, ring) on every call, so spurious firing on a transient hiccup would be destructive |
| `src/psk_recorder/core/receiver_manager.py:201` | `StatusListener` | `.start()`, `.register_channel(ch_info)` | same in-place-mutation contract as wspr-recorder |
| `src/psk_recorder/core/stream.py:52` | `SlotClock` | one `SlotClock(cadence_sec, sample_rate, settle_sec=...)` per channel; `.anchor()`, `.anchored`, `.offset_of_rtp()`, `.reset()` under `self._clock_lock` | `cadence_sec * sample_rate` must be an exact integer sample count or the constructor raises `ValueError` (FT8=15s/FT4=7.5s @ 12000 Hz both exact in practice); `.anchor(rtp_timestamp, utc)` takes RTP first, `utc` float second, matching `rtp_to_utc`'s return type |
| `src/psk_recorder/core/stream.py:156,266` | `rtp_to_utc` | **not called directly** — handed as a callable to `hamsci_dsp.timing.acquire_anchor_utc(..., rtp_to_utc=rtp_to_utc, ...)` from `_anchor_utc_now()`/`_anchor_utc_for()` | assumes the exact signature `(rtp_timestamp, channel, wallclock_hint_sec=None) -> Optional[float]` so the shared third-party `acquire_anchor_utc` helper can call it by those exact keyword names — a signature change breaks silently inside that shared helper, not visibly at the psk-recorder call site |
| `src/psk_recorder/core/slot.py:31` | `SlotClock` | reads `.sample_rate`, `.cadence_samples`, `.settle_samples`; calls `.offset_of_rtp(latest_rtp)` under the shared lock | **verified directly** (`stream.py:250`, `slot.py:266`): neither call site wraps `offset_of_rtp()` in try/except, unlike `SlotClock.advance()` which catches `SlotClockDesyncError` internally (confirmed in `ka9q/slot_clock.py:204-236`) — a genuine desync propagates uncaught up through the `MultiStream` receive-thread callback. This is a psk-recorder/meteor-scatter robustness gap, not a ka9q-python contract violation, but worth flagging since it's exactly the kind of latent bug this audit exists to surface. |

**Internals reach:** none.

### meteor-scatter

Structurally near-identical to psk-recorder (same class names
`ChannelSink`/`SlotWorker`, same anchor-once/slide-follow design, same
`_clock_lock`/`offset_of_rtp` pattern, near-verbatim docstrings) —
confirmed by direct comparison, not assumed.

| module | symbol | how used | load-bearing behavior |
|---|---|---|---|
| `src/meteor_scatter/configurator.py:248` | `discover_radiod_services` | identical to psk-recorder/wspr-recorder | same contract |
| `src/meteor_scatter/core/receiver_manager.py:171,373` | `MultiStream`, `RadiodControl` | same provisioning pattern as psk-recorder | same contract; `on_stream_restored` reset semantics identical |
| `src/meteor_scatter/core/stream.py:46` | `SlotClock` | same pattern as psk-recorder | same unguarded `offset_of_rtp()` call (`stream.py:245`) — same latent desync-propagation gap noted above, present in both repos |
| `src/meteor_scatter/core/stream.py:150,261` | `rtp_to_utc` | same callable-passed-to-`acquire_anchor_utc` pattern | same contract |
| `src/meteor_scatter/core/slot.py:42` | `SlotClock` | same as psk-recorder's `slot.py` | same contract |

**Internals reach:** none.

**Genuine behavioral gap found (meteor-scatter-specific, not a
ka9q-python issue):** unlike wspr-recorder and psk-recorder,
meteor-scatter's `receiver_manager.py` never imports or starts
`ka9q.status_listener.StatusListener` — confirmed absent anywhere
under `meteor-scatter/src/`. Its `_anchor_utc_now()` slide-follow hook
(`core/stream.py:136-159`) calls `rtp_to_utc(self._anchor_rtp,
self._channel_info, ...)` every tick, but `self._channel_info` is set
once at provisioning and only replaced wholesale on
`on_stream_restored` — without a `StatusListener` mutating
`.gps_time`/`.rtp_timesnap` in place at ~2 Hz, the slide-follow
mechanism is present in code but effectively inert (it recomputes
nearly the same offset every tick rather than tracking radiod's live
RTP↔UTC drift the way its sibling recorders do). `ka9q-python` already
supports the intended behavior (`StatusListener.register_channel`
exists for exactly this); meteor-scatter simply never wires it up.
Flagged for the meteor-scatter maintainers, out of scope for this
repo's own remediation.

### Cross-client observations

- **`rtp_to_utc`/`rtp_to_wallclock` contract** is identical everywhere
  it's used across all four clients: `Optional[float]` Unix-epoch
  seconds, never a `datetime`, `None` on missing timing info,
  side-effect-free. `rtp_to_wallclock` is a deprecated alias for
  `rtp_to_utc` (`ka9q/rtp_recorder.py:247`, renamed 2026-06-27, no
  `DeprecationWarning`) — hf-timestd and wspr-recorder both still use
  the old name exclusively; psk-recorder/meteor-scatter already use the
  new name. A signature or return-type change here silently breaks
  every recorder's minute/slot correlation (wrong types, not
  exceptions raised).
- **`MultiStream.add_channel()`'s `on_samples(samples, quality)`
  callback** is the load-bearing backbone of sample delivery in all
  four clients. `quality` is consumed by direct attribute access in
  hf-timestd/wspr-recorder (`AttributeError` on a rename) vs.
  `getattr(quality, "last_rtp_timestamp", None)` in
  psk-recorder/meteor-scatter (silently degrades — batches just stop
  anchoring, no error logged). A `StreamQuality` field rename would be
  loud in two clients and silent in the other two.
- **`client_id=` kwarg on `RadiodControl.__init__`** is used
  identically by all four (plus `hfdl-recorder`/`codar-sounder`/
  `hf-tec`/`superdarn-sounder`, see Step 3), each citing "CONTRACT v0.3
  §7 / ka9q-python ≥ 3.14.0" verbatim in comments — an explicit,
  version-pinned expectation that `ensure_channel()` derives a
  per-(client_id, radiod) deterministic multicast destination when no
  explicit `destination=` is given. This is the mechanism that keeps
  every sigmond-suite recorder from colliding on multicast groups on a
  shared radiod.

---

## Step 2: Internals reaches

Definition per the brief: any import that reaches a `ka9q` submodule
for a symbol **not** re-exported from `ka9q/__init__.py` (i.e. not
reachable as a plain `ka9q.<name>` attribute via that file's own
`from .X import ...` lines / `__all__`). Importing a symbol that *is*
re-exported, just via its submodule path (e.g. `from ka9q.discovery
import ChannelInfo` instead of `from ka9q import ChannelInfo`), is
**not** counted — it's a style choice with zero drift risk beyond the
two names already being kept in sync inside `ka9q-python` itself.

Checked against `ka9q/__init__.py` directly (not inferred): confirmed
`ka9q.utils` is never imported into `__init__.py` at all (only
`ka9q.addressing.generate_multicast_ip` and `ka9q.monitor.ChannelMonitor`
are pulled in under a "Utilities" heading), and `ka9q.control`'s
wire-encoding primitives (`encode_int`, `encode_double`, `encode_eol`,
module-level `CMD`) are internal to `control.py` — only the
`RadiodControl` class and `allocate_ssrc` are re-exported from that
module.

### hf-timestd — the only client with true internals reaches

1. **`encode_int`, `encode_double`, `encode_eol`, `CMD`** — from
   `ka9q.control`, imported inline at
   `src/hf_timestd/core/stream_recorder_v2.py:894`, inside
   `_set_filter_edges(self, ssrc)` (lines 884-911, called from both
   `_create_channel()` and `register_with()` right after
   `ensure_channel()` returns, whenever `low_edge`/`high_edge` config is
   set — used to widen the demod passband for e.g. FSK).

   **This reach is unnecessary and not motivated by a genuine capability
   gap.** `ka9q/control.py:1209` already defines a public
   `RadiodControl.set_filter(self, ssrc, low_edge=None, high_edge=None,
   kaiser_beta=None)` — verified by reading it directly — that encodes
   the **identical set of TLV fields**, but not in the identical byte
   order: hf-timestd's `_set_filter_edges`
   (`stream_recorder_v2.py:884-911`) emits
   `CMD` → `OUTPUT_SSRC` → `COMMAND_TAG` → `LOW_EDGE` → `HIGH_EDGE` →
   `EOL`, while `set_filter()` (`control.py:1220-1235`) emits
   `CMD` → `LOW_EDGE` → `HIGH_EDGE` → `KAISER_BETA` → `OUTPUT_SSRC` →
   `COMMAND_TAG` → `EOL` — a different field order, and `set_filter()`
   additionally supports `KAISER_BETA` (a third parameter
   `_set_filter_edges` never sends). The two are **not** byte-for-byte
   identical wire output. They are semantically equivalent for the
   `low_edge`/`high_edge` fields both send: radiod's TLV decoder is a
   linear scan keyed by type tag, not a fixed-offset struct, so field
   order within one command packet does not affect how radiod
   interprets it — order-independence here is a property of the TLV
   decode loop (`decode_radio_commands`, confirmed structurally in
   `docs/audit/2026-08-12-alignment/control-surface.md`), not
   something re-verified byte-for-byte in this task. hf-timestd's
   `_set_filter_edges` reimplements the `LOW_EDGE`/`HIGH_EDGE` half of
   `set_filter()`'s TLV set by hand instead of calling
   `self._control.set_filter(ssrc, low_edge=low, high_edge=high)`. This
   looks like a reimplementation predating `set_filter`'s addition (or
   simply not noticed), not a case where ka9q-python lacked the
   capability. **Public API that already replaces it:**
   `RadiodControl.set_filter()` (note: adopting it would also make
   `KAISER_BETA` available to hf-timestd for free, which
   `_set_filter_edges` currently has no path to set at all).

2. **`resolve_multicast_address`** — from `ka9q.utils`, imported inline
   at `src/hf_timestd/core/core_recorder_v2.py:196`, inside
   `CoreRecorderV2.__init__`. Its return value is discarded; it's
   called purely for the side effect of raising when the configured
   status address (mDNS name or IP) can't be resolved, which triggers a
   fallback to `discover_radiod_services()`-based auto-discovery.
   `resolve_multicast_address` (verified: `ka9q/utils.py:17`, a
   legitimate top-level, non-underscore-prefixed public function — it
   tries a literal-IP check, then `avahi-resolve`, then `dns-sd`, then
   `socket.getaddrinfo`, raising a generic `Exception` on total
   failure) is simply never pulled into `__init__.py`. **What public
   API should cover this instead:** either re-export
   `resolve_multicast_address` from `ka9q/__init__.py` (it already sits
   conceptually beside `generate_multicast_ip`), or — better —
   expose a typed reachability/health-check method on `RadiodControl`
   itself (e.g. `is_reachable()`) so callers don't need a raise-on-failure
   utility function with no typed exception class and no re-export
   guarantee. This is a real, if minor, capability gap distinct from
   the four already-known ones (spectrum TLVs, `SETOPTS`, `set_lock`).

The fallback import in the same block,
`from ka9q.discovery import discover_radiod_services`
(`core_recorder_v2.py:201`), is **not** a true reach —
`discover_radiod_services` is re-exported at top level
(`ka9q/__init__.py`, both the `from .discovery import (...)` block and
`__all__`).

### wspr-recorder, psk-recorder, meteor-scatter — none

All submodule-path imports in these three repos
(`ka9q.discovery.discover_radiod_services`, `ka9q.discovery.ChannelInfo`,
`ka9q.discovery.discover_channels`, `ka9q.status_listener.StatusListener`)
resolve to symbols present in `ka9q/__init__.py`'s `__all__`. Verified
directly against the file, not assumed from the "known" list in the
brief.

---

## Step 3: Bypass sweep — all repos under `/opt/git/sigmond`

`ls /opt/git/sigmond` (confirmed, not assumed from the brief's
illustrative list): `callhash`, `codar-sounder`, `ft8_lib`,
`gpsdo-monitor`, `hamsci-dsp`, `hf-tec`, `hf-timestd`, `hfdl-recorder`,
`hs-uploader`, `igmp-querier`, `ka9q-python` (self, excluded),
`ka9q-radio` (excluded), `ka9q-web`, `mag-recorder`, `meteor-scatter`,
`onion`, `psk-recorder`, `sigmond`, `sigmond-rac`, `superdarn-sounder`,
`wsjtx`, `wspr-recorder`. All 20 non-excluded repos were swept, not just
the four named clients.

### Headline correction to the brief's "zero `ka9q` imports" premise

**Both `hfdl-recorder` and `codar-sounder` DO import `ka9q` — the
earlier scoping that found zero was almost certainly a shallow grep that
missed lazy, function-local imports** (both repos import `ka9q` inside
methods, not at module top-of-file, matching the same lazy-import style
already visible in psk-recorder/meteor-scatter's `stream.py`):

- `hfdl-recorder/src/hfdl_recorder/core/daemon.py:90` —
  `from ka9q import MultiStream, RadiodControl` (inside `_provision()`)
- `hfdl-recorder/src/hfdl_recorder/configurator.py:163` —
  `from ka9q.discovery import discover_radiod_services`
- `codar-sounder/src/codar_sounder/core/stream.py:254-255` —
  `from ka9q.stream import RadiodStream` /
  `from ka9q.control import RadiodControl` (inside `_import_ka9q()`)
- `codar-sounder/src/codar_sounder/core/stream.py:324` —
  `from ka9q import rtp_to_utc`
- `codar-sounder/src/codar_sounder/configurator.py:105` —
  `from ka9q.discovery import discover_radiod_services`

Both `RadiodControl`/`RadiodStream`, though imported via their
submodule paths (`ka9q.control`/`ka9q.stream`), **are** re-exported at
`ka9q` top level — verified directly (`ka9q/control.py:844` defines
`RadiodControl`, re-exported at `__init__.py:70`; `ka9q/stream.py:244`
defines `RadiodStream`, re-exported at `__init__.py:109-111`). Not an
internals reach, just a style choice.

Two more previously-unlisted repos also import `ka9q` cleanly:
`hf-tec/src/hf_tec/core/stream.py` (`RadiodControl`, `RadiodStream`,
`rtp_to_utc` — same lazy-import shape as codar-sounder/psk-recorder,
apparently a shared template) and
`superdarn-sounder/src/superdarn_sounder/core/stream.py` (identical
shape). `sigmond` itself (the suite's CLI/TUI meta-tool) also imports
`ka9q` properly in five places (`harmonize.py`,
`tui/screens/{sdr_inventory,radiod,receiver_channels}.py`,
`discovery/multicast.py`) for inventory/discovery/control, all via
`RadiodControl`/`discover_channels`/`discover_radiod_services`.

**Net finding:** every sigmond-authored Python repo that talks to
radiod's control plane at all does so exclusively through
`ka9q-python`'s public surface. No sigmond client hand-rolls TLV
encode/decode of control commands or opens a raw socket to send
commands to radiod's control port.

### `hfdl-recorder` — deep dive (special attention per the brief)

Read every non-archive `.py` file (4083 lines) plus `scripts/install.sh`.

- `core/daemon.py` provisions via
  `RadiodControl.ensure_channel`/`set_output_encoding`/`set_filter`,
  groups channels into a `MultiStream`, refreshes LIFETIME via
  `multi.set_channel_lifetime`. **Classification: COMPLIANT.**
- `core/radiod.py` — thin `ensure_channel` wrapper, documents an
  IQ-encoding parser gotcha; no independent radiod traffic. COMPLIANT.
- `core/band_pipeline.py` — subscribes via `multi.add_channel`, pipes
  received IQ samples to a `dumphfdl` **subprocess** over stdin.
  `dumphfdl` is a local HFDL *decoder* binary, not a ka9q-radio control
  tool, and never itself talks to radiod — not a CLI bypass.
  COMPLIANT.
- `core/ch_tailer.py`, `core/feed.py` — local file tailing / building
  `dumphfdl --output` args; no radiod networking. NOT-RADIOD-RELATED.
- The only `pcmrecord` grep hit, `scripts/install.sh:194`, is a
  **comment** explaining that the installer disables ka9q-radio's own
  upstream `hfdl.service` (which itself shells out to `pcmrecord`)
  because hfdl-recorder replaces it — hfdl-recorder itself never
  invokes `pcmrecord`. COMPLIANT (comment, not invocation).
- Only raw-socket use anywhere: `_sd_notify()` (`daemon.py:314`,
  `AF_UNIX SOCK_DGRAM` to systemd's `$NOTIFY_SOCKET`) — unrelated to
  radiod. NOT-RADIOD-RELATED.

**hfdl-recorder classification: COMPLIANT.** It consumes RTP via
`MultiStream`/ka9q-python exactly like the four named clients; the
earlier "how does it get its feed" question is answered: through
`ka9q-python`, same as everyone else — the prior "zero imports" finding
was a false negative from missing lazy imports, not evidence of a
bypass.

### `codar-sounder` — deep dive (special attention per the brief)

Read `core/daemon.py` (633 lines) and `core/stream.py` (516 lines) in
full, plus `configurator.py`/`cli.py`/`tdma_config_writer.py` context.

- `core/stream.py`'s `RadiodIQSource` provisions via
  `RadiodControl(...).ensure_channel(...)`, subscribes via
  `RadiodStream(channel=..., on_samples=self._on_samples)`, refreshes
  LIFETIME via `self._control.set_channel_lifetime(...)`, and derives
  CPI (coherent processing interval) start timestamps via
  `ka9q.rtp_to_utc` + the suite-shared `hamsci_dsp.timing.acquire_anchor_utc`
  helper (`_compute_anchor_utc`, read in full — docstring: "codar
  anchors the FIRST delivered sample... CPI frames free-run off this
  single anchor"). Exactly the sanctioned pattern. COMPLIANT.
- `core/daemon.py`'s only raw-socket use is `_sd_notify()` (line 79,
  `AF_UNIX SOCK_DGRAM`) — unrelated to radiod. NOT-RADIOD-RELATED.
- Test-file hits (`tests/test_tdma_config_writer.py`,
  `tests/test_config_roundtrip.py`) are all string-literal fixture data
  (`"bee1-status.local"`) — no networking. NOT-RADIOD-RELATED.

**codar-sounder classification: COMPLIANT.** Same answer as
hfdl-recorder — it gets its radiod feed entirely through
`ka9q-python`'s public `RadiodControl`/`RadiodStream` surface.

Also noteworthy: `codar-sounder/core/stream.py`'s `_on_samples`
callback sanitizes non-finite/absurd-magnitude values coming out of
`RadiodStream`'s resequencer gap-fill path (docstring: "ka9q-python's
resequencer occasionally produces garbage in gap-fill regions... NaN
poisons the entire range profile; large values overflow during FFT
accumulation... Real radiod s16-encoded IQ is normalised to roughly
[-1, 1]"). This is a client-side defensive workaround for a
ka9q-python behavior, not a bypass — flagged as a possible
ka9q-python-side fix candidate for a future task (gap-fill should
plausibly never emit non-finite/overflow-scale samples), but out of
scope for this task's read-only client-repo sweep.

### First sweep command — `pcmrecord|metadump|tune -|control -` — zero true bypasses

Every hit across all 20 repos was verified individually and is a false
positive:

- `mag-recorder/install.sh:166,295`, `gpsdo-monitor/install.sh:109,174`
  — `udevadm control --reload-rules`; matched only because the regex's
  `\bcontrol -` caught `control --reload`. NOT-RADIOD-RELATED.
- `wspr-recorder/wspr_recorder/__main__.py:908`,
  `receiver_manager.py:417,434` — comments documenting which
  `metadump` status-field index a `ChannelStatus` attribute mirrors
  (documentation only). NOT-RADIOD-RELATED.
- `ft8_lib/main.c:246` — comment describing pcmrecord's atomic-rename
  file convention; ft8_lib is a pure decoder library (verified zero
  `socket(`/`SOCK_DGRAM`/`sendto(`/`recvfrom(` anywhere in the repo).
  NOT-RADIOD-RELATED.
- `hf-timestd/src/hf_timestd/core/packet_resequencer.py:267` — comment
  citing "Phil Karn's pcmrecord.c approach" as an algorithm reference.
  NOT-RADIOD-RELATED.
- `hf-timestd/tools/t6_estimator_sweep.py` — comments describing the
  file format of previously hand-captured `pcmrecord -r` output;
  confirmed no `subprocess`/`Popen`/`os.system` calls anywhere in the
  file. NOT-RADIOD-RELATED.
- `meteor-scatter/.../core/wav.py:4,94`,
  `psk-recorder/.../core/wav.py:4,94` — comments noting WAV/xattr
  conventions "matching what pcmrecord produces" (interoperability
  note only). NOT-RADIOD-RELATED.

### Second sweep command — `SOCK_DGRAM|status\.local|5006` — breakdown

Roughly 140 hits, all verified by reading context, falling into these
buckets:

1. **Test fixtures / config-string literals** (`"bee1-status.local"`,
   `"h.local:5006"`, port `5006` as a plain dict value) across sigmond,
   meteor-scatter, psk-recorder, hf-timestd test suites — no sockets
   involved. NOT-RADIOD-RELATED / COMPLIANT (test data).
2. **`AF_UNIX SOCK_DGRAM` sd_notify/wake sockets** — hfdl-recorder
   `daemon.py:314`, codar-sounder `daemon.py:79`, superdarn-sounder
   `daemon.py:135`, hf-tec `daemon.py:44`, mag-recorder `cli.py:337`,
   hs-uploader `daemon.py:57` and `wake.py:58,102`. All talk to
   systemd's `$NOTIFY_SOCKET` or an internal wake-pipe. NOT-RADIOD-RELATED.
3. **NTP client** — `sigmond/lib/sigmond/discovery/ntp.py:23,75` — SNTPv4
   mode-3 query on port 123, unrelated protocol. NOT-RADIOD-RELATED.
4. **PSKReporter UDP spot submission** — vendored
   `meteor-scatter/vendor/pskreporter.py:210`,
   `psk-recorder/vendor/pskreporter.py:210` — third-party
   PSKReporter.info client, unrelated service. NOT-RADIOD-RELATED.
5. **IGMP querier** — `igmp-querier/igmp_querier.py` (read in full, 618
   lines) — a raw `IPPROTO_IGMP` socket sending RFC 2236 General
   Queries to `224.0.0.1` to keep IGMP-snooping switches forwarding
   multicast traffic. This is network/IGMP-layer, not radiod's
   application-layer TLV control protocol, and never touches radiod's
   control port. NOT-RADIOD-RELATED (different protocol layer
   entirely, not a disguised control-plane bypass).
6. **"Determine my own IP" trick** —
   `hf-timestd/src/hf_timestd/core/core_recorder_v2.py:77`,
   `scripts/detect_ip.py:10` — `connect(('8.8.8.8', 1))` then
   `getsockname()`; no packet actually sent, unrelated to radiod.
   NOT-RADIOD-RELATED.
7. **Hostname-label string formatting only** — `sigmond`'s
   `commands/radiod_config.py`, `site_profile.py`, `commands/config.py`,
   `tui/app.py` build/parse `"<id>-status.local"` config strings; actual
   network use happens later through `RadiodControl`/`discover_channels`
   (already verified compliant above). COMPLIANT.

### One RTP-only hand-rolled consumer — compliant per the brief's explicit carve-out

`hf-timestd/src/hf_timestd/audio_streamer.py:64-71` —
`AudioStreamer.start()` opens a plain
`socket.socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP)`, does
`IP_ADD_MEMBERSHIP` to join a multicast group, and `bind()`s to receive
RTP audio for re-streaming over HTTP. `multicast_address`/
`multicast_port` are plain CLI args
(`hf-timestd/src/hf_timestd/audio_stream.py`); it never provisions a
channel and never sends any control-plane packet — it only taps an
already-provisioned stream. Per the brief's explicit rule ("RTP-only
consumption without control is compliant"), this is **COMPLIANT**, not
a bypass — but it hand-rolls the multicast join instead of using
`ka9q.RadiodStream`/`MultiStream`, which is a duplication/consolidation
opportunity, not a capability gap (ka9q-python already does this).

### Archived/dead code — historical bypasses, confirmed not live

`hf-timestd/src/hf_timestd/__init__.py` explicitly documents these
archive paths as deprecated (e.g. "PipelineRecorder archived
2026-01-16 — used deprecated RTPReceiver"), and no live `src/` file
imports from `archive/`. Within `archive/` there are genuine
pre-ka9q-python-era bypass patterns preserved for historical reference
only:

- `archive/shell-scripts/compare-packets.sh:25` — invokes
  `control -v 239.251.200.193` (the ka9q-radio `control` CLI binary) as
  a packet-format comparison diagnostic.
- `archive/shell-scripts/check_radiod_bandwidth.sh:10,23` — calls
  `radctl 239.1.2.1:5006 dump 5000000` (another ka9q-radio CLI) and
  separately binds a raw `AF_INET SOCK_DGRAM` socket to port 5006 to
  sniff status packets by hand.
- `archive/shell-scripts/debug-radiod-packets.sh`,
  `test-channel-creation.sh` — tcpdump captures of a since-renamed
  predecessor tool; no radiod commands sent directly.
- `archive/legacy-code/{v2-recorder,grape-rtp-recorder}/*.py`,
  `archive/legacy-src/recorder.py` — pre-ka9q-python-era recorders.

None of this is live or importable from the shipped `src/` tree — noted
for completeness per the brief's exhaustiveness requirement, not
counted as a current-state bypass.

### Special case: `ka9q-web` and `onion` (vendored third-party C, not a sigmond client)

`ka9q-web` is a vendored third-party C web dashboard ("A web interface
for ka9q-radio by John Melton G0ORX", per its own README) with its own
independent, hand-built TLV status decoder (`decode_status.c`,
`status.c`) and a `control_set_frequency()` function
(`ka9q-web.c:2242`, invoked at line 857 and 3887) that sends real
tune/frequency commands directly to radiod's control socket — verified
directly by reading the function and its call sites. A
`ka9q-web.service` systemd unit is present in the repo, implying it is
part of the deployed operational stack. `onion` is purely the vendored
Onion C web framework build dependency required to compile `ka9q-web`
— it contains no radiod-related code of its own.

This **is** genuine direct control-plane traffic to radiod that bypasses
`ka9q-python` — but it is categorically different from the Python
sigmond-client bypasses the no-bypass policy is aimed at: it is
third-party, non-sigmond-authored C code (predates ka9q-python, cannot
import a Python library, is a UI dashboard maintained upstream by a
different author). It is **not classified as a client capability-gap
bypass** in the Step 3 sense — no ka9q-python change would fix it,
since it structurally cannot depend on ka9q-python at all — but it is
recorded here since a literal reading of "no repo under
`/opt/git/sigmond` may talk to radiod directly" is violated by its
presence in the tree. This is a scope/inventory question for whoever
owns the no-bypass policy (should `ka9q-web` be excluded from the
policy's repo list the way `ka9q-radio` already is, given it's
vendored upstream code?), not a remediation item for ka9q-python
itself.

### Repos with no radiod interaction at all

`mag-recorder`, `gpsdo-monitor`, `hs-uploader`, `callhash`, `wsjtx`,
`hamsci-dsp`, `sigmond-rac`, `ft8_lib`, `igmp-querier` (different
protocol layer, see above). Verified via targeted greps for
`radiod|ka9q|5006|SOCK_DGRAM` plus manual read of every hit — all
either unrelated commands/protocols, comments, or copyright headers
(`wsjtx`'s Reed-Solomon code citing "Copyright 2002 Phil Karn, KA9Q" —
the person, not the project).

### Confirmed bypass list

**Confirmed bypasses among sigmond-authored Python clients: NONE.**

Every sigmond-authored Python repo that touches radiod's control plane
(`hf-timestd`, `wspr-recorder`, `psk-recorder`, `meteor-scatter`,
`hfdl-recorder`, `codar-sounder`, `hf-tec`, `superdarn-sounder`,
`sigmond`) does so exclusively through `ka9q.RadiodControl`,
`ka9q.RadiodStream`/`ka9q.stream.RadiodStream`, `ka9q.MultiStream`,
`ka9q.discover_channels`, or `ka9q.discovery.discover_radiod_services`.
No hand-built TLV encode/decode of control commands, no raw socket
sending commands to port 5006, and no shelling out to
`tune`/`control`/`pcmrecord`/`metadump` was found in any live
sigmond-suite Python code path.

Two non-bypass items carried forward as follow-ups, not remediation
items for this task:

1. `hf-timestd/src/hf_timestd/audio_streamer.py:64` — RTP-only
   hand-rolled multicast consumer; compliant, but a consolidation
   candidate.
2. `ka9q-web` (C, vendored/third-party, not sigmond-authored) — sends
   real control commands to radiod directly; a policy-scope question,
   not a ka9q-python capability gap.

---

## What surprised me

1. **The brief's premise that hfdl-recorder/codar-sounder had "zero
   `ka9q` imports" was simply wrong** — both import `ka9q` via
   function-local (lazy) imports, which is exactly the same style
   pattern already used in psk-recorder/meteor-scatter's `stream.py`
   for the same symbols. A naive top-of-file-only grep produces a false
   negative here; the brief's own Step 1 command
   (`grep -rn ... '^\s*(from ka9q...'`) would have caught these too if
   run against these two repos, since the anchor `^\s*` matches
   indented lines just fine — the "zero imports" finding upstream of
   this task most likely came from a narrower/different search (e.g.
   restricted to `__init__.py`/top-level files only, or run before
   these lazy imports were added). Two more repos not named anywhere in
   the brief (`hf-tec`, `superdarn-sounder`) turned out to use the
   *exact same* lazy-import template as codar-sounder — apparently a
   shared boilerplate for "coherent RF sensor" style clients across the
   suite (codar-sounder, hf-tec, superdarn-sounder all build near-identical
   `RadiodIQSource`/`_import_ka9q()`/`rtp_to_utc`-via-`acquire_anchor_utc`
   scaffolding).
2. **Zero true bypasses anywhere in ~20 repos** is itself notable —
   this is a mature, well-enforced policy in practice, not just on
   paper. The only genuine direct-control-plane-traffic case
   (`ka9q-web`) is foreign vendored C code that predates and cannot use
   ka9q-python, not a policy violation by any sigmond-authored client.
3. **hf-timestd's only true "internals reach" (`ka9q.control`'s raw TLV
   encoders) turned out to be unmotivated by any actual capability
   gap** — `RadiodControl.set_filter()` already sends the same
   `LOW_EDGE`/`HIGH_EDGE` fields the hand-rolled code does (in a
   different TLV field order, which doesn't matter to radiod's decoder,
   plus a `KAISER_BETA` field hf-timestd never sends at all). This cuts
   against the assumption (reasonable going in) that internals reaches
   are usually evidence of a missing public API; here it's evidence of
   a reimplementation nobody migrated off of.
4. **A previously undocumented, minor capability gap**:
   `ka9q.utils.resolve_multicast_address` is a legitimate public
   utility (raise-on-unreachable address probe) that simply never got
   pulled into `ka9q/__init__.py`'s re-export list — distinct from the
   four already-known gaps (spectrum TLVs, `SETOPTS`, `set_lock`).
5. **A live, unguarded-exception robustness gap in two clients**
   (psk-recorder and meteor-scatter both call `SlotClock.offset_of_rtp()`
   directly, without the try/except that `SlotClock.advance()` applies
   internally to the same `SlotClockDesyncError`) — found while reading
   call sites for the symbol matrix, not the bypass sweep, but worth
   surfacing since it's a real latent bug in production recorder code.
6. **meteor-scatter's slide-follow timing correction is currently
   inert** (missing `StatusListener` wiring that its sibling
   psk-recorder has) — a functional gap in the client, not in
   ka9q-python, found only by reading the actual call sites rather than
   trusting that "same code shape" meant "same behavior."
