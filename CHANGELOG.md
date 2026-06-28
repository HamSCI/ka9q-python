# Changelog

## [3.17.0] - 2026-06-28

First release marked **Production/Stable** (trove classifier 4 → 5). Folds in
23 commits of feature and resilience work that had accumulated on `main` past
the `v3.16.1` tag with no version bump.

### Added

- **`RadiodControl.poll_channel(ssrc)`** (`control.py`) — a targeted, O(1)
  status probe for a single channel. Replaces the previous reliance on a full
  channel discovery sweep when only one channel's state is needed.
- **`MultiStream.prune_frequency(...)`** (`multi_stream.py`) — releases a
  superseded channel slot *and its ring buffer*, fixing a ring leak when a
  frequency is retuned/replaced within a live `MultiStream`.
- **mDNS hostname + port surfaced by discovery** (`discovery.py`) — additive
  fields on the discovery result; existing consumers are unaffected.
- **RTP↔GPS offset-step detection (`anchor_epoch`)** (`stream.py`) — a
  `RadiodStream` now detects a step in radiod's RTP↔GPS offset and re-anchors
  its timing reference instead of carrying a stale anchor.

### Changed

- **`ensure_channel` now verifies via `poll_channel` (O(1))** instead of a dead
  discovery sweep (`control.py`). Faster, more reliable channel confirmation.
  Note a deliberate relaxation on the *create* path: the channel is now accepted
  on **frequency match alone** — a rate/preset divergence is logged as a warning
  (not raised) and the destination is no longer re-verified. Callers should treat
  the returned `ChannelInfo` as authoritative for the granted encoding/rate
  (the sigmond recorders already do). The reuse path still verifies strictly.
- **`RadiodStream` binds the channel's multicast group**, not `0.0.0.0`
  (`stream.py`) — avoids cross-talk on hosts carrying multiple multicast groups.
- **`rtp_to_wallclock` renamed to `rtp_to_utc`** (`rtp_recorder.py`). The name
  "wallclock" wrongly implied a system-clock dependency; the function is purely
  RTP/GPS-referenced. `rtp_to_wallclock` remains as a deprecated alias — no
  caller needs to change.
- **`anchor_step_threshold_sec` raised 0.25 → 0.75** (`stream.py`) to tolerate
  output-timing jitter on a busy radiod without spuriously re-anchoring.
- **8 MB receive buffer on `poll_channel`'s status listener** (`control.py`) to
  avoid drops while probing on a busy status multicast group.
- **ka9q-radio compatibility pin advanced to `9b742e6`** (no protocol drift;
  validated by `check_upstream_drift.py`).
- **Repository moved to the HamSCI org**; project/doc/URL references updated
  from `mijahauan/` to `HamSCI/`.

### Fixed

- **Gap storm now treated as a stream-health failure → re-subscribe**
  (`multi_stream.py`, 228a041). A stale `MultiStream` subscription after a
  radiod restart manifests as a sustained packet-gap storm (not silence); the
  health monitor now detects it and re-subscribes, restoring delivery without
  an external restart. See the sigmond `stale-subscription-gap-storm-protection`
  note.

### Packaging

- **Removed the redundant `setup.py`.** All project metadata lives in
  `pyproject.toml`'s PEP 621 `[project]` table (`setuptools.build_meta`
  backend). `setup.py` had drifted to a stale `3.10.0` / `4 - Beta` duplicate
  and was an unused second source of truth.

### Docs / Tests

- Added a **requirements baseline** (`docs/REQUIREMENTS.md`, `KQP-*` spec).
- `CLAUDE.md` documents `MultiStream` as the fourth abstraction layer.
- The **live channel-verification suite is now gated behind explicit opt-in**
  (`--radiod-host`), so the default `pytest` run no longer hangs waiting on a
  live radiod. Unit suite: 363 passed, 27 skipped.

## [3.16.1] - 2026-05-24

### Fixed

- **`ChannelInfo` anchor pair is now atomic** (`channel-info`).  Adds
  `ChannelInfo.get_anchor()` / `update_anchor()` — a tuple-based
  atomic snapshot of `(gps_time, rtp_timesnap)`.  `rtp_to_wallclock`
  now reads the pair via `get_anchor` (single GIL-atomic attribute
  access) instead of two separate reads; `StatusListener` writes via
  `update_anchor` (single tuple assignment).

  Why: the `StatusListener` introduced in 3.16.0 refreshes the anchor
  in place at sub-second cadence (~450 ms on a busy host).  Direct
  sequential reads of `channel.gps_time` followed by
  `channel.rtp_timesnap` could land between the listener's two writes
  and yield a torn pair off by one listener-cadence interval.
  `rtp_to_wallclock` then returned a wall-time off by that much —
  usually harmless, but consumers comparing against an external time
  reference with a tight gate (e.g. hf-timestd's T5 LB-1421 NMEA
  disambig at ±0.5 s) could be pushed across the threshold and fall
  back to a chrony walk that itself fails during a post-restart
  cascade.

  Backward compatible: constructor kwargs (`gps_time=`,
  `rtp_timesnap=`) and direct field reads are unchanged; consumers
  that need the pair transactionally must call `get_anchor`.  Adds
  5 new tests (atomic update, construction-time seed, mixed-None
  handling, listener path, 50-iteration consistency smoke test).

### Performance

- **`RadiodStream`: `SO_RCVBUF` raised 0 → 64 MB** (`stream.py`).
  Mirrors the 3.16.0-cycle `multi_stream.py` change on the
  single-channel path.  Previously `RadiodStream` sockets fell back
  to the kernel default (`rmem_default`, typically 16 MB on hosts
  with sigmond's `rule_kernel_rcvbuf_adequate` provisioning) and were
  vulnerable to GIL-stall packet loss with no other consumer competing
  to drain the buffer.  64 MB matches the `MultiStream` cap; sigmond
  provisions `net.core.rmem_max=128 MB` (after kernel doubling), so
  the request is honored.  Observed on bee1 2026-05-23 closing a
  140 ms-stall-induced `gap=13440` resequencer event on hf-timestd's
  T6 dedicated stream.

- **`MultiStream`: `SO_RCVBUF` raised 8 MB → 64 MB** (`multi_stream.py`).
  Observed 412 M UDP `RcvbufErrors` on B4-100 since boot, driven by
  GIL contention preventing Python receiver threads from draining the
  kernel-doubled 16 MB sockets.  Bigger absorber → more headroom
  across GIL stalls before packets are dropped.  After applying:
  socket `rb` shows 134217728 (128 MB visible after kernel doubling)
  and recv-Q sits at ~50 KB in steady state (was hitting 14 MB / 16
  MB before).  Requires `net.core.rmem_max >= 64 MB` to be honored —
  provisioned by sigmond in
  `/etc/sysctl.d/99-wspr-recorder.conf` alongside this change.

## [3.16.0] - 2026-05-23

### Added

- **`StatusListener` — continuous STATUS multicast listener.**  New
  `ka9q.status_listener.StatusListener` class subscribes to radiod's
  STATUS multicast (port 5006) in a background thread and refreshes
  `ChannelInfo.gps_time` / `.rtp_timesnap` on every broadcast.  Replaces
  the previous one-shot `discover_channels` anchor capture, which
  froze the timing anchor at SSRC discovery — leaving `rtp_to_wallclock`
  to project forward from a host-clock value that drifts at the
  chrony slew rate (~3.8 µs/s on a typical disciplined host).

  Mutates the registered `ChannelInfo` in place so callers holding a
  reference (e.g. hf-timestd's cached `_t6_channel_info`) see fresh
  values immediately.  Supports per-SSRC and wildcard callbacks for
  explicit notification.  Uses `SO_REUSEPORT` so it can coexist with
  `RadiodControl`'s own status socket without stealing tune/discover
  responses — multicast packets are delivered to every joined socket.

  Opt-in via `RadiodControl.start_status_listener()`; closing the
  control object stops the listener.  See class docstring for usage.

- **`RadiodControl.start_status_listener(...)` / `.stop_status_listener(...)`
  / `.status_listener` property** — convenience wiring to attach a
  `StatusListener` to an existing control session.  Listener is
  stopped automatically by `RadiodControl.close()`.

### Why this exists

Before this release, every ka9q-python consumer (hf-timestd, codar,
psk, hfdl, wspr, wsprdaemon-client, gpsdo-monitor) labeled data via
`rtp_to_wallclock(rtp, channel)` with a ChannelInfo whose
`gps_time`/`rtp_timesnap` were captured once at SSRC discovery.
For long-running services this meant labels drifted from GPSDO truth
at the host-clock slew rate — on bee1 (`chrony` slewing at
~3.8 µs/s), labels accumulated ~330 ms of drift per day.

The chrony SHM-push side of hf-timestd's BPSK PPS path (HPPS / HFPS)
manifested this most visibly: TS-1 source reported tracking with
1 ns standard deviation but drifted at the host slew rate, blocking
DASI2 grant deployment.  Faster anchor refresh closes the drift.

### Backwards compatibility

- All existing tests pass unchanged.  The listener is **opt-in** —
  consumers that don't call `start_status_listener()` see no behavior
  change.
- `ChannelInfo` is unchanged structurally; only its mutability
  contract is clarified (the timing fields were always documented as
  "the latest snapshot", which is now true continuously rather than
  once).

## [3.15.1] - 2026-05-21

### Fixed

- **`ka9q.__version__` was stale.**  v3.15.0's wheel correctly identified
  itself as `3.15.0` in dist-info, but the `__version__` string baked
  into `ka9q/__init__.py` still read `'3.14.2'` — the in-package
  introspection answer was wrong for every consumer.  The fix replaces
  the hardcoded literal with `importlib.metadata.version("ka9q-python")`,
  so `__version__` now always tracks the installed dist-info — drift
  between release version and code-reported version is no longer
  possible.  Falls back to `"0.0.0+unknown"` on import error or when
  the package is not installed (e.g. running from a source tree
  without an editable install).

## [3.15.0] - 2026-05-21

### Added

- **F16LE / F16BE RTP payload decoding** in `parse_rtp_samples` for radiod's
  float16 output mode (encodings 6 and 9).  Both audio and IQ paths.
- **G.711 µ-law / A-law decoders** for encodings 10 and 11 — pure-numpy
  table-based, no `audioop` dependency (which was removed in Python 3.13).
- **`OpusDecoder` class** in `ka9q.stream` for OPUS / OPUS_VOIP payloads
  (encodings 3 and 7).  Lazy-imports `opuslib`; install via
  `pip install ka9q-python[opus]`.  Maintains codec state across calls so
  packet-loss concealment works end-to-end — one instance per stream SSRC.
- New `opus` optional dependency in `pyproject.toml` (`opuslib>=3.0`).

### Fixed

- **`RadiodControl.set_agc(attack_rate=...)` was unreachable.**  It encoded
  `StatusType.AGC_ATTACK_RATE`, which does not exist in ka9q-radio's
  `status.h` or in `ka9q/types.py` — any call passing `attack_rate=` raised
  `AttributeError`.  Replaced with a working `threshold:` kwarg backed by
  `AGC_THRESHOLD` (which radiod actually decodes in
  `decode_radio_commands()`).
- **Removed 6 stale duplicate `set_*` methods.**  In a single class, second
  defs silently shadow firsts — so callers were already using the correct
  versions further down the file.  The dead first defs of `set_squelch`,
  `set_pll`, `set_output_channels`, `set_independent_sideband`,
  `set_envelope_detection`, and `set_opus_bitrate` are gone.  One of them
  (`set_opus_bitrate`) referenced a non-existent `StatusType.OPUS_BITRATE`
  — the working second def uses the correct `OPUS_BIT_RATE`.

### Tooling / pinning

- Pin advanced from ka9q-radio `f78cff9c` (1.0.0-22) to `d555f1853422`.
  Drift report confirms zero TLV-tag, encoding-enum, demod-type, or
  window-type changes across the 68 intervening upstream commits — only
  packaging, hydrasdr-driver, and internal C-struct refactors.  Existing
  ka9q-python state (`types.py`, `decode_status_packet`, `SpectrumStream`,
  every `set_*` method) was already covering the full HEAD protocol
  surface; this release brings the recorded compatibility tag in line.

### Tests

- Fixed pre-existing `tests/test_filter_edges.py::_bare_control` helper
  that did not initialise the `client_id` attribute added with v3.14.0
  per-(client,radiod) multicast destinations.

## [3.14.2] - 2026-05-14

### Added

- **`rtp_to_wallclock()` gains optional `wallclock_hint_sec` parameter.**
  When supplied, the function uses the hint to disambiguate the 32-bit
  RTP wrap epoch instead of calling `time.time()`.  Authority-aware
  callers (those with access to an hf-timestd `rtp_to_utc_offset_ns`)
  can now keep the labeling path off the chrony-disciplined system
  clock, per the METROLOGY.md §4.5 RTP-reference invariant.  The hint
  only needs ±period/2 accuracy (≥6 hours at typical sample rates).
  When omitted, the function falls back to `time.time()` for backward
  compatibility — existing callers are unaffected.

### Tests

- 3 new in `tests/test_rtp_recorder.py`: hint bypasses `time.time()`
  entirely; hint at a different wrap epoch correctly overrides system
  clock; default path still consults `time.time()` when no hint is
  given.

## [3.14.1] - 2026-05-14

### Fixed

- **`RadiodStream`: multi-interface multicast join (parity with
  `MultiStream`).**  Extends Rob Robinett's `e3acb6a` fix from
  `multi_stream.py` to `stream.py` so the per-stream `RadiodStream`
  socket also joins on every local IPv4 interface instead of the
  single one the kernel picks via `INADDR_ANY`.  Without this, a
  `RadiodStream` consumer of a co-located radiod with `ttl=0` (or any
  output that arrives on a non-default-route interface) silently
  received zero packets.  Most visible to clients that use the
  per-stream API directly (codar-sounder, hf-timestd's legacy T6
  path) rather than the shared `MultiStream` socket.

### Refactor

- The helper that enumerates IPv4 interfaces and calls
  `IP_ADD_MEMBERSHIP` on each is factored into a new package-private
  `ka9q/_multicast.py` module.  Both `multi_stream.py` and `stream.py`
  now import from it.  Same behaviour, one implementation.
  `rtp_recorder.RtpRecorder` still uses the single-interface join —
  separate fix.

### Tests

- 8 new in `tests/test_multicast_helpers.py`: enumerator behaviour,
  per-interface join failure handling (one interface failing doesn't
  abort the loop), empty-enumeration safety, both stream classes
  pulling the helper from the shared module.

## [3.14.0] - 2026-05-13

### Added

- **`RadiodControl(client_id=...)` for per-client deterministic multicast
  destinations.**  Closes the CONTRACT v0.3 §7 gap where the spec said
  "ka9q-python derives the multicast destination" but the implementation
  never did, so every client on a given radiod landed on radiod's
  config-file default group.  Two clients with no operator action are now
  guaranteed to use distinct multicast addresses without per-client
  derivation code.
  - New `client_id: Optional[str] = None` kwarg on
    `RadiodControl.__init__`.  When set, `ensure_channel(destination=None)`
    auto-derives a `239.x.y.z` address via
    `generate_multicast_ip(client_id, radiod_host=self.status_address)`.
  - Destination precedence inside `ensure_channel`:
    `(1) explicit destination=` >
    `(2) derived from (client_id, status_address)` >
    `(3) None → radiod's config default`.
  - Multi-radiod handling falls out of the hash: a `psk-recorder` instance
    pointing at `bee1-hf-status.local` derives a different address from
    one pointing at `bee3-hf-status.local`.
  - Multi-client handling falls out of the hash: `psk-recorder` and
    `wspr-recorder` on the same radiod derive different addresses.
  - `MultiStream._attempt_restore` inherits the behavior because it reuses
    the same `RadiodControl` instance (and thus the same `client_id`).
    Restoration after a radiod restart re-creates each channel on the
    same per-client multicast group it was on before.
  - Because `allocate_ssrc` already hashes `destination` into the SSRC,
    per-client destinations produce per-client SSRCs — radiod's channel
    table cleanly separates concurrent clients.

### Backward Compatibility

- Default behavior unchanged: `RadiodControl(status)` without `client_id`
  preserves pre-3.14 semantics — `destination=None` flows through, radiod
  uses its config-file default, every channel from that radiod shares one
  multicast group.  Clients opt in by passing `client_id="<name>"`.

### Tests

- 9 new unit tests in `tests/test_client_id_destination.py` cover:
  client_id default-None / set / stored; precedence (explicit wins over
  derived, no client_id → None); uniqueness invariants (same client on
  two radiods → distinct; two clients on one radiod → distinct;
  same client + same radiod → repeatable); SSRC divergence per client.
  134 offline unit tests pass.

## [3.13.0] - 2026-05-08

### Added

- **`MultiStream` channel-lifetime support**: closes the gap from 3.10.0 where
  `RadiodControl.{create_channel,ensure_channel,tune}` accepted a `lifetime=`
  kwarg but `MultiStream.add_channel()` did not, leaving MultiStream-based
  clients (psk-recorder, hfdl-recorder, hf-timestd) unable to opt into
  radiod's channel self-destruct timer for crash-resilient cleanup.
  - `MultiStream.add_channel(..., lifetime=None)` — optional kwarg, forwarded
    to the internal `ensure_channel` call. Stored per-slot.
  - **Drop/restore path now re-applies lifetime**: `_attempt_restore` reads
    the stored slot lifetime and passes it to `ensure_channel`. Previously,
    a channel that radiod self-destructed and MultiStream restored would
    silently lose its LIFETIME until the next external keep-alive — the
    most dangerous failure mode now closed.
  - `MultiStream.set_channel_lifetime(ssrc, lifetime)` — keep-alive method
    that updates both the wire (via `RadiodControl.set_channel_lifetime`)
    and the slot's stored lifetime, so the value survives subsequent
    drop/restore cycles.

### Backward Compatibility

- Default behavior unchanged: omitting `lifetime` produces a packet with no
  LIFETIME tag (ChannelSlot.lifetime defaults to None). Existing MultiStream
  callers see no change in wire behavior.

### Tests

- 5 new unit tests in `tests/test_lifetime.py::TestMultiStreamLifetime` cover:
  forward-on-add, lifetime=None default, restore-reapplies-lifetime,
  set_channel_lifetime updates slot+wire, set_channel_lifetime is a no-op for
  unknown SSRC. 258 unit tests still green.

---


## [3.12.0] - 2026-05-07

### Added

- **Spectrum bin vector decoding**: `decode_status_packet()` now decodes `BIN_DATA` (float32, `SPECT_DEMOD`) and `BIN_BYTE_DATA` (uint8, `SPECT2_DEMOD`) TLV vectors from radiod status packets. New fields on `SpectrumStatus`:
  - `bin_data: Optional[np.ndarray]` — float32 power values per FFT bin.
  - `bin_byte_data: Optional[np.ndarray]` — uint8 quantised log-power values.
  - `bin_power_db` property — returns dB values regardless of source format (10*log10 for float data, `base + byte * step` for byte data).

- **`SpectrumStream`**: new class for receiving real-time FFT spectrum data from radiod. Spectrum data flows over the status multicast channel (port 5006) as TLV vectors, not over RTP. `SpectrumStream` handles channel creation, periodic polling, SSRC filtering, and delivers decoded `ChannelStatus` objects (with populated `spectrum.bin_power_db`) to an `on_spectrum` callback. Supports retuning via `set_frequency()` and context manager usage.

### Documentation

- API_REFERENCE.md: SpectrumStream section, SpectrumStatus bin vector fields, quickstart table updated.
- ARCHITECTURE.md: module listing, abstraction layer description, threading model updated.
- RECIPES.md: Recipe 5 covering spectrum display, spectrogram accumulation, frequency axis reconstruction, FFT parameter tuning, and combined audio+spectrum patterns.
- New `examples/spectrum_example.py`: runnable CLI example for real-time spectrum reception.

### Tests

- 13 new unit tests in `tests/test_spectrum.py` covering float32 and uint8 bin decoding, multi-byte TLV lengths, `bin_power_db` property, combined metadata+bins, edge cases, and import verification.

---


## [3.11.0] - 2026-05-06

### Added

- **Channel filter overrides on create / ensure / add_channel**: optional `low_edge`, `high_edge`, and `kaiser_beta` kwargs on `RadiodControl.create_channel()`, `RadiodControl.ensure_channel()`, and `MultiStream.add_channel()` — when specified, the requested filter passband is applied inline with the channel-create command (no transient at preset BW), and on the reuse path of `ensure_channel`, `set_filter` is called so the requested edges are authoritative regardless of the channel's prior state. Previously, callers were stuck with the preset's `low`/`high` values and had to either author a custom radiod preset or call `set_filter` manually after channel creation. Filter edges are not part of the SSRC hash — multiple callers requesting the same channel with different filters reconfigure last-writer-wins, matching the existing model for `gain` / `agc_enable`.
  - Motivating clients: hf-timestd's BPSK PPS calibrator needs ~±500 Hz to match the TS1 injector's effectively-CW spectrum (the iq preset's ±5 kHz is wrong by an order of magnitude); planned SuperDARN and CODAR-sounder receivers need wider passbands than any single preset provides.

### Backward Compatibility

- Default behavior unchanged: omitting all three kwargs produces a wire packet with no LOW_EDGE / HIGH_EDGE / KAISER_BETA tags, so radiod uses the preset's defaults exactly as before. Reuse path also skips `set_filter` when no filter args are supplied.

### Tests

- 8 new unit tests in `tests/test_filter_edges.py` cover encode-presence on each entry point, encode-absence when omitted, ensure_channel forwarding on the create path, and ensure_channel reuse-path calling set_filter (only when filter args are supplied).

---

## [3.10.0] - 2026-04-30

### Added

- **Channel-lifetime keep-alive** (radiod 0f8b622+): support for ka9q-radio's new self-destruct timer, which lets a channel declare a lifetime in radiod main-loop frames (~50 Hz at the default 20 ms blocktime); 0 means infinite, >0 decrements every frame and destroys the channel at zero. This gives crash-resilient cleanup — if a client dies, its channels expire on their own instead of lingering on radiod.
  - `RadiodControl.set_channel_lifetime(ssrc, lifetime)` — explicit poll that sends the LIFETIME tag, suitable as a periodic keep-alive.
  - `RadiodControl.create_channel(..., lifetime=None)` — optional kwarg; sends LIFETIME on the creation packet when provided.
  - `RadiodControl.ensure_channel(..., lifetime=None)` — passes through to `create_channel`; on the reuse path, calls `set_channel_lifetime` so the value is enforced regardless of the channel's prior state.
  - `RadiodControl.tune(..., lifetime=None)` — optional kwarg; LIFETIME is included in the multi-parameter command buffer.
  - `_decode_status_response()` exposes received LIFETIME as `status['lifetime']`; `ChannelStatus.lifetime` field.
- **`StatusType.LIFETIME = 117`** in `ka9q/types.py` (synced from ka9q-radio commit `5498aef`).
- **`StatusType.DESCRIPTION` decode** in `_decode_status_response()`: incoming front-end / channel description strings are now surfaced as `status['description']` (the encode side already existed via `set_description`).

### Backward Compatibility

- Default behavior unchanged: omitting `lifetime` produces a wire packet with no LIFETIME tag, so pre-`0f8b622` radiod stays compatible and existing clients see no change. Channels created without LIFETIME inherit radiod's template default (0 = infinite) and live until destroyed.

### Tests

- 10 new unit tests in `tests/test_lifetime.py` cover encode-presence on each entry point, encode-absence when omitted, and validation of negative / non-int inputs.
- Refreshed stale `tests/test_encode_socket.py` to match the current 6-byte wire format that radiod's `decode_socket()` actually expects.
- Hardened `tests/test_native_discovery.py::test_native_discovery_with_valid_packet` to mock `RadiodControl._connect`, removing the DNS dependency on `test.local`.

---

## [3.9.0] - 2026-04-15

### Added

- **`ka9q` CLI** (`ka9q/cli.py`): new console entry point `ka9q` (declared via `[project.scripts]`), exposing channel discovery, tune, create, destroy, and status commands against a running radiod.
- **Textual TUI** (`ka9q/tui.py`): an interactive Textual app for browsing channels and tuning interactively, with radiod and SSRC pickers. Optional install via `pip install ka9q-python[tui]`.
- **Typed `ChannelStatus` decoder** (`ka9q/status.py`): a typed dataclass view over the dict returned by `_decode_status_response`, so callers can use attribute access and IDE-completable fields instead of dict keys.

---

## [3.8.0] - 2026-04-12

### Added

- **`MultiStream`**: Shared-socket multi-SSRC receiver. Opens a single UDP socket and receive thread for all channels on a given multicast group, demultiplexes RTP packets by SSRC, and dispatches per-channel sample callbacks identical in shape to `RadiodStream`/`ManagedStream`. Solves the kernel-copy scalability problem that arose when N single-channel streams each bound their own socket on the same multicast group — every packet was delivered to every socket, and each worker parsed/discarded 95% of traffic in Python. `MultiStream` drops that load to one socket and one full-header parse per received packet (SSRC is peeked pre-parse). Includes per-channel drop detection and automatic restoration via `ensure_channel()`.
- **`parse_rtp_samples()`** helper in `stream.py`: factored out of `RadiodStream._parse_samples` so `MultiStream` and any future receiver can share one implementation of encoding-aware payload decoding (F32LE/F32BE, S16LE/S16BE, IQ).

### Client migration

- **Multi-channel clients on one radiod**: consider replacing a loop of `ManagedStream` instances with a single `MultiStream`. API: `multi.add_channel(frequency_hz, preset, sample_rate, encoding, on_samples=..., on_stream_dropped=..., on_stream_restored=...)` per channel, then `multi.start()`. All channels must resolve to the same multicast group (enforced; raises `ValueError` on mismatch — caller can bucket into multiple `MultiStream`s).
- **Single-channel clients**: no change. `RadiodStream` / `ManagedStream` behavior is unchanged.

### Verified

- Live 2-channel smoke test (`examples/multi_stream_smoke.py`) against `bee3-status.local`: FT8+WSPR 20m on shared group, 99.1% sample completeness, zero gaps, no unknown-SSRC warnings.
- End-to-end validation via psk-recorder migration: 5 channels on one multicast group, identical per-sink sample counts, clean FT8/FT4 decodes.

---

## [3.7.1] - 2026-04-12

### Fixed

- **S16BE/S16LE audio parsing in `RadiodStream`**: `_parse_samples()` always decoded audio-mode RTP payloads as float32, regardless of the channel's actual encoding. When encoding was S16BE (used by FT4/FT8 channels at 12 kHz), a 240-byte payload containing 120 int16 samples was misinterpreted as 60 float32 values. The `PacketResequencer` then reported a 120-sample gap on every packet (~2500 gaps/minute), reducing stream completeness to ~37% and making the stream unusable for downstream consumers like `decode_ft8`. Now dispatches on `channel.encoding`: S16BE (`>i2`), S16LE (`<i2`), F32BE (`>f4`), and default F32LE (`np.float32`). All int16 formats are normalized to float32 (÷32768) in the callback.
- **`ManagedStream` now accepts `encoding` parameter**: Added `encoding: int = 0` to `__init__()`, passed through to both `ensure_channel()` call sites (initial provisioning and stream restoration). Without this, `ManagedStream` would re-provision a channel without encoding on restore, causing format mismatches. Clients using non-default encoding (S16BE, S16LE, F32BE) should pass `encoding=` to the constructor. This eliminates the need for hf-timestd's `RobustManagedStream` workaround.

### Client migration

- **Clients using `ManagedStream` with non-default encoding**: Pass `encoding=<int>` to the constructor. Example: `ManagedStream(control=ctl, frequency_hz=14.074e6, preset="usb", sample_rate=12000, encoding=2)` for S16BE.
- **Clients using bare `RadiodStream` with S16BE/S16LE**: No code change needed — samples are now correctly decoded to float32 in the callback. Note that if client code was compensating for the old bug (e.g., manually byte-swapping), that workaround should be removed.
- **hf-timestd**: Can replace `RobustManagedStream` (stream_recorder_v2.py lines 40-195) with `ka9q.ManagedStream(encoding=Encoding.F32)` and delete the wrapper class.

---

## [3.7.0] - 2026-04-12

### Changed

- **Radiod-aware multicast addressing**: `generate_multicast_ip()` now accepts a `radiod_host` keyword argument. When provided, the hash is computed over both the client identifier and the radiod host, producing a distinct multicast IP for each (client, radiod) pair. This prevents address collisions when the same client application (e.g., `hf-timestd`) connects to multiple radiod instances simultaneously.
- **Radiod-aware SSRC allocation**: `allocate_ssrc()` now accepts a `radiod_host` parameter, included in the deterministic hash. The same channel parameters on different radiod instances produce distinct SSRCs.
- **Automatic radiod identity propagation**: `RadiodControl.create_channel()` and `RadiodControl.ensure_channel()` automatically pass `self.status_address` as `radiod_host` when computing SSRCs. No API changes needed for callers using the `RadiodControl` methods — radiod-aware uniqueness is automatic.

### Backward Compatibility

- `generate_multicast_ip(unique_id)` without `radiod_host` produces identical results to v3.6.0.
- `allocate_ssrc()` without `radiod_host` changes the hash format (trailing separator added), so SSRC values will differ from v3.6.0 for existing deployments. This is intentional — channels will be re-created with new SSRCs on upgrade. The old SSRCs were not radiod-aware and could collide when a client talked to multiple radiod instances.
- `ManagedStream`, `RadiodStream`, and `RTPRecorder` require no changes — they receive the already-computed SSRC/address via `ChannelInfo`.

---

## [3.6.0] - 2026-04-09

### Added

- **L6 BPSK PPS chain-delay calibration** (`ka9q/pps_calibrator.py`): New utility module for measuring end-to-end RF-to-RTP chain delay using a local GPS-disciplined BPSK PPS injector (WB6CXC design). Three new public classes:
  - `BpskPpsCalibrator` — detects PPS edges in BPSK IQ streams via phase-transition detection, validates timing consistency, and reports the measured chain delay once locked. Algorithm ported from Scott Newell's wd-record.c `bpsk_state_machine()`.
  - `PpsCalibrationResult` — dataclass returned by the calibrator with `chain_delay_ns`, `chain_delay_samples`, edge counters, and lock status.
  - `NotchFilter500Hz` — biquad IIR notch filter at 500 Hz for interference rejection on the BPSK channel.
- **`ChannelInfo.chain_delay_correction_ns`** (`ka9q/discovery.py`): New optional field. When set, `rtp_to_wallclock()` automatically subtracts this correction from the computed wall time, compensating for the measured RF/ADC/DSP/RTP chain latency. Defaults to `None` (no correction; backward compatible).
- All three new classes exported from `ka9q/__init__.py`.

### Changed

- **`rtp_to_wallclock()`** (`ka9q/rtp_recorder.py`): Now applies `channel.chain_delay_correction_ns` when present. Existing callers are unaffected (the field defaults to `None`).

---

## [3.5.1] - 2026-04-09

### Added

- **`DemodType` enum** (`ka9q/types.py`): New auto-generated class mirroring `enum demod_type` from ka9q-radio `radio.h`. Exposes `LINEAR_DEMOD`, `FM_DEMOD`, `WFM_DEMOD`, `SPECT_DEMOD`, `SPECT2_DEMOD`, and `N_DEMOD`.
- **`WindowType` enum** (`ka9q/types.py`): New auto-generated class mirroring `enum window_type` from ka9q-radio `window.h`. Exposes all 9 FFT window types (`KAISER_WINDOW` through `HP5FT_WINDOW`) plus `N_WINDOW` sentinel.
- Both new types are exported from `ka9q/__init__.py` and tracked by the drift detection system.

### Fixed

- **`set_demod_type()` validation**: Was rejecting demod_type=4 (`SPECT2_DEMOD`), the spectrum v2 mode added in ka9q-radio. Validation range corrected from 0-3 to 0-4.

### Changed

- **`sync_types.py` expanded**: Now parses 4 C headers (`status.h`, `rtp.h`, `radio.h`, `window.h`) instead of 2. Drift detection covers `DemodType` and `WindowType` in addition to `StatusType` and `Encoding`. The `_check_enum()` helper replaces duplicated per-enum comparison logic.
- **Compatibility pin bumped** to ka9q-radio `d39fea8` (no protocol-level changes from previous pin `6b0fec7`; intervening commits were wd-record and bug fixes).

---

## [3.5.0] - 2026-03-31

### Added

- **Protocol Drift Detection** (`scripts/sync_types.py`): New tool that code-generates `ka9q/types.py` from the ka9q-radio C headers (`status.h`, `rtp.h`). Three modes:
  - `--check`: exits non-zero if `types.py` is out of sync (for CI and tests)
  - `--apply`: regenerates `types.py` and updates compatibility pins
  - `--diff`: dry-run showing what would change
- **Compatibility Pin** (`ka9q_radio_compat`): Tracked plain-text file recording the ka9q-radio commit hash that `types.py` was last validated against.
- **Importable Pin** (`ka9q/compat.py`): `KA9Q_RADIO_COMMIT` constant for use by deployment tooling (e.g. `ka9q-update`).
- **Drift Test** (`tests/test_protocol_compat.py`): Pytest test that runs `sync_types.py --check` and verifies the pin matches ka9q-radio HEAD. Auto-skips when ka9q-radio source is not present.
- **New StatusType**: `SPECTRUM_OVERLAP` (116) for FFT window overlap control.
- **New Encodings**: `MULAW` (10), `ALAW` (11) for telephony-grade audio.
- **`set_max_delay()`**: New control method replacing `set_packet_buffering()`. Sets maximum aggregation delay in blocks (0-5).

### Changed

- **StatusType renames** (matching ka9q-radio HEAD):
  - `MINPACKET` → `MAXDELAY`
  - `GAINSTEP` → `UNUSED4`
  - `CONVERTER_OFFSET` → `UNUSED3`
  - `COHERENT_BIN_SPACING` → `UNUSED2`
  - `BLOCKS_SINCE_POLL` → `UNUSED`
- **Encoding**: `UNUSED_ENCODING` sentinel shifted from 10 to 12.
- `types.py` is now auto-generated with C header comments preserved.

### Removed

- `compare_encodings.py` and `compare_status_types.py` (replaced by `scripts/sync_types.py`).

### Backward Compatibility

- `set_packet_buffering()` retained as a deprecated alias for `set_max_delay()`.
- `Encoding.F32` and `Encoding.F16` aliases retained for `F32LE` and `F16LE`.

---

## [3.4.2] - 2026-02-05

### Added

- **Comprehensive Getting Started Guide**: Added a new `docs/GETTING_STARTED.md` file. This guide provides a step-by-step tutorial for new users, covering installation, a simple first program, core concepts, and troubleshooting tips. It is now the recommended entry point for new users.
- **Examples README**: Added a new `examples/README.md` file to organize the examples directory. It categorizes examples by complexity (basic, intermediate, advanced) and provides a recommended learning path, making it easier for users to find relevant examples.

### Fixed

- **`stream_example.py` Bug**: Fixed a critical bug in `examples/stream_example.py` where the code incorrectly iterated over the dictionary returned by `discover_channels`. The example now correctly accesses `ChannelInfo` objects, preventing an `AttributeError` and allowing the example to run as intended.

### Changed

- **Updated API Documentation**: The documentation for `create_channel` in `docs/API_REFERENCE.md` has been updated to match the actual implementation. This includes adding the `destination` and `encoding` parameters, correcting the parameter order, and documenting the return type (`int` SSRC).
- **Updated README**: The main `README.md` has been updated to link to the new Getting Started guide.

---

## [3.4.1] - 2026-01-27

### Fixed

- **Stable SSRC Allocation**: Replaced unstable Python `hash()` with `hashlib.sha256` in `allocate_ssrc()`. This ensure SSRCs remain consistent across process restarts, allowing `radiod` to reuse existing receivers and preventing resource exhaustion.

## [3.4.0] - 2026-01-13

### Added

- **TTL Reporting**: Implemented decoding of `OUTPUT_TTL` (status type 19) from `radiod`.
- **TTL Warning**: Added a warning log when `radiod` reports a TTL of 0, indicating multicast restrictions.
- **Web API**: Exposed the `ttl` value in the `/api/channel/<address>/<ssrc>` endpoint.
- **Tests**: Added unit and integration tests for TTL decoding and warning logic.

### Fixed

- **Web UI Start Script**: Fixed `webui/start.sh` to correctly locate `app.py` when running from outside the `webui` directory.

## [3.2.7] - 2025-12-17

### Added

- **ChannelMonitor**: New service that provides automatic recovery from `radiod` restarts. It monitors registered channels and automatically invokes `ensure_channel` to restore them if they disappear.
  - Usage: `monitor = ChannelMonitor(control); monitor.start(); monitor.monitor_channel(...)`

## [3.2.6] - 2025-12-17

### Added

- **Output Encoding Support**: Added complete support for specifying output encoding (e.g., F32, S16LE, OPUS) in `ensure_channel` and `create_channel`.
  - `create_channel` now automatically sends the required follow-up `OUTPUT_ENCODING` command to `radiod`.
  - `ensure_channel` verifies the encoding of existing channels and reconfigures them if different from the requested encoding.
  - `ChannelInfo` now includes the `encoding` field for discovered channels.

## [3.2.5] - 2025-12-17

### Added

- **Destination-Aware Channels**: `ka9q-python` now supports unique destination IP addresses per client application. The `ensure_channel` and `create_channel` methods now accept a `destination` parameter.
- **Unique IP Generation**: Added `generate_multicast_ip(unique_id)` helper function to deterministically map application IDs to the `239.0.0.0/8` multicast range.
- **Improved SSRC Allocation**: `allocate_ssrc` now includes the destination address in its hash calculation, ensuring that streams with different destinations get unique SSRCs.

## [3.2.4] - 2025-12-16

### Fixed

- **Resequencer Fragmented IQ Support** - Fixed resequencer to correctly handle fragmented IQ packets from radiod. The resequencer now uses actual packet sample count for timestamp tracking instead of the fixed `samples_per_packet` value. This prevents false gap detection when radiod fragments large IQ payloads (e.g., IQ 20kHz F32 fragments into 1440+1440+320 byte packets). Affects `_try_output()`, `_handle_lost_packet()`, and `flush()` methods.

### Added

- **Channel Verification Test Suite** - Comprehensive test suite (`tests/test_channel_verification.py`) that verifies radiod channel creation, encoding, sample rate, and destination configuration. Demonstrates ka9q-python usage patterns and serves as integration test.

## [3.2.3] - 2025-12-16

### Fixed

- **Socket Reconnection Vulnerability** - Fixed vulnerability where socket errors would terminate the receive loop permanently. Both `RadiodStream` and `RTPRecorder` now implement automatic reconnection with exponential backoff (1s to 60s max). This provides robustness against network interface restarts, multicast group membership drops, and transient network errors.

## [3.2.2] - 2025-12-13

### Fixed

- **Time Calculation** - Fixed 18-second error in UTC timestamp calculation caused by leap seconds. `rtp_to_wallclock()` now correctly subtracts the current 18-second GPS-UTC offset.

## [3.2.1] - 2025-12-11

### Fixed

- **Time Calculation** - Fixed GPS-to-Unix time conversion in `rtp_to_wallclock()`. Previous formula incorrectly applied NTP epoch offset, resulting in future timestamps.
- **Documentation** - Updated `RTP_TIMING_SUPPORT.md` with the correct formula.

## [3.2.0] - 2025-12-01

### 🌊 RadiodStream API - Continuous Sample Delivery with Quality Tracking

This release adds a high-level streaming API that delivers continuous sample streams with comprehensive quality metadata. Designed for applications like GRAPE, WSPR, CODAR, and SuperDARN that need reliable data capture with gap detection and quality metrics.

### Added

**Stream Module (`ka9q.stream`):**

- `RadiodStream` - High-level sample stream with automatic resequencing and gap filling
  - Callback-based delivery: `on_samples(samples: np.ndarray, quality: StreamQuality)`
  - Handles IQ and audio modes automatically
  - Cross-platform multicast support (Linux, macOS, Windows)

**Quality Tracking (`ka9q.stream_quality`):**

- `StreamQuality` - Comprehensive quality metrics per batch and cumulative
  - `completeness_pct` - Percentage of expected samples received
  - `total_gap_events` / `total_gaps_filled` - Gap statistics
  - RTP packet metrics: received, lost, duplicate, resequenced
  - RTP timestamps: `first_rtp_timestamp`, `last_rtp_timestamp` for precise timing
- `GapEvent` - Individual gap details (position, duration, source)
- `GapSource` - Gap type classification (NETWORK_LOSS, RESEQUENCE_TIMEOUT, EMPTY_PAYLOAD, etc.)

**Resequencer (`ka9q.resequencer`):**

- `PacketResequencer` - Circular buffer resequencing with gap detection
  - Handles out-of-order packet delivery
  - KA9Q-style signed 32-bit timestamp arithmetic for wrap handling
  - Zero-fills gaps for continuous stream integrity
- `RTPPacket` - Packet data structure with samples
- `ResequencerStats` - Resequencing statistics

**Examples:**

- `examples/stream_example.py` - Basic streaming demonstration
- `examples/grape_integration_example.py` - Two-phase recording pattern (startup buffer → recording)

### Example Usage

```python
from ka9q import RadiodStream, StreamQuality, discover_channels

def on_samples(samples, quality: StreamQuality):
    print(f"Got {len(samples)} samples, {quality.completeness_pct:.1f}% complete")
    for gap in quality.batch_gaps:
        print(f"  Gap: {gap.source.value}, {gap.duration_samples} samples")

channels = discover_channels('radiod.local')
stream = RadiodStream(
    channel=channels[10000000],
    on_samples=on_samples,
    samples_per_packet=320,  # RTP timestamp increment at 16kHz
)
stream.start()
# ... run until done ...
final_quality = stream.stop()
```

### Architecture

```
radiod → Multicast → RadiodStream → PacketResequencer → App Callback
                          ↓
                    StreamQuality (per batch + cumulative)
```

**Core delivers:**

- Continuous sample stream (gaps zero-filled)
- Quality metadata with every callback

**Applications handle:**

- Segmentation (1-minute NPZ, 2-minute WAV, etc.)
- Format conversion
- App-specific gap classification (cadence_fill, late_start, etc.)

---

## [3.1.0] - 2025-12-01

### 🎯 SSRC Abstraction - SSRC-Free Channel Creation

This release removes SSRC from the application concern. Applications now specify **what they want** (frequency, mode, sample rate) and the system handles SSRC allocation internally.

### Added

**SSRC Allocation:**

- `allocate_ssrc(frequency_hz, preset, sample_rate, agc, gain)` - Deterministic SSRC allocation from channel parameters. Same parameters always produce the same SSRC, enabling stream sharing across applications.

**SSRC-Free `create_channel()`:**

- `ssrc` parameter is now **optional** (moved to end of parameters)
- When omitted, SSRC is auto-allocated using `allocate_ssrc()`
- Method now **returns the SSRC** (useful when auto-allocated)

### Changed

- `create_channel()` signature: `frequency_hz` is now the first (required) parameter
- `create_channel()` now returns `int` (the SSRC) instead of `None`

### Cross-Library Compatibility

The SSRC allocation algorithm matches signal-recorder's `StreamSpec.ssrc_hash()`:

```python
key = (round(frequency_hz), preset.lower(), sample_rate, agc, round(gain, 1))
return hash(key) & 0x7FFFFFFF
```

This ensures:

- Same parameters → same SSRC in both ka9q-python and signal-recorder
- Stream sharing works across applications using either library
- Deterministic allocation for coordination

### Example

```python
from ka9q import RadiodControl, allocate_ssrc

# SSRC-free API (recommended)
with RadiodControl("radiod.local") as control:
    ssrc = control.create_channel(
        frequency_hz=14.074e6,
        preset="usb",
        sample_rate=12000
    )
    print(f"Created channel with SSRC: {ssrc}")

# Or use allocate_ssrc() directly for coordination
ssrc = allocate_ssrc(10.0e6, "iq", 16000)
```

---

## [3.0.0] - 2025-12-01

### 🎉 Major Release: Complete RadioD Feature Exposure

This major release exposes **all remaining radiod features** through the Python interface, providing comprehensive control over every aspect of ka9q-radio operation.

### Added - 20 New Control Methods

**Tracking & Tuning:**

- `set_doppler(ssrc, doppler_hz, doppler_rate_hz_per_sec)` - Doppler frequency shift and rate for satellite tracking
- `set_first_lo(ssrc, frequency_hz)` - Direct hardware tuner frequency control

**Signal Processing:**

- `set_pll(ssrc, enable, bandwidth_hz, square)` - Phase-locked loop configuration for carrier tracking
- `set_squelch(ssrc, enable, open_snr_db, close_snr_db)` - SNR-based squelch with hysteresis
- `set_envelope_detection(ssrc, enable)` - Toggle between envelope (AM) and synchronous detection
- `set_independent_sideband(ssrc, enable)` - ISB mode (USB/LSB to separate L/R channels)
- `set_fm_threshold_extension(ssrc, enable)` - Improve FM reception at weak signal levels

**AGC & Levels:**

- `set_agc_threshold(ssrc, threshold_db)` - Set AGC activation threshold above noise floor

**Output Control:**

- `set_output_channels(ssrc, channels)` - Configure mono (1) or stereo (2) output
- `set_output_encoding(ssrc, encoding)` - Select output format (S16BE, S16LE, F32, F16, OPUS)
- `set_opus_bitrate(ssrc, bitrate)` - Configure Opus encoder bitrate (0=auto, typical 32000-128000)
- `set_packet_buffering(ssrc, min_blocks)` - Control RTP packet buffering (0-4 blocks)
- `set_destination(ssrc, address, port)` - Change RTP output multicast destination

**Filtering:**

- `set_filter2(ssrc, blocksize, kaiser_beta)` - Configure secondary filter for additional selectivity

**Spectrum Analysis:**

- `set_spectrum(ssrc, bin_bw_hz, bin_count, crossover_hz, kaiser_beta)` - Configure spectrum analyzer parameters

**System Control:**

- `set_status_interval(ssrc, interval)` - Set automatic status reporting rate
- `set_demod_type(ssrc, demod_type)` - Switch demodulator type (LINEAR/FM/WFM/SPECTRUM)
- `set_rf_gain(ssrc, gain_db)` - Control RF front-end gain (hardware-dependent)
- `set_rf_attenuation(ssrc, atten_db)` - Control RF front-end attenuation (hardware-dependent)
- `set_options(ssrc, set_bits, clear_bits)` - Set/clear experimental option bits

### Updated - Type Definitions

Fixed `StatusType` constants in `ka9q/types.py`:

- `SPECTRUM_FFT_N = 76` (was UNUSED16)
- `SPECTRUM_KAISER_BETA = 91` (was UNUSED20)
- `CROSSOVER = 95` (was UNUSED21)

### Documentation

**New Documentation Files:**

- `NEW_FEATURES.md` - Comprehensive documentation of all 20 new features with examples
- `QUICK_REFERENCE.md` - Quick reference guide with practical code examples
- `RADIOD_FEATURES_SUMMARY.md` - Complete implementation summary and verification
- `examples/advanced_features_demo.py` - Working demonstration script

### Feature Coverage

This release provides complete coverage of radiod's TLV command set:

- ✅ All 35+ radiod TLV commands now supported
- ✅ Doppler tracking for satellite reception
- ✅ PLL carrier tracking for coherent detection
- ✅ SNR squelch with hysteresis
- ✅ Independent sideband (ISB) mode
- ✅ FM threshold extension
- ✅ Secondary filtering
- ✅ Spectrum analyzer configuration
- ✅ Output encoding selection
- ✅ RF hardware controls
- ✅ Experimental option bits

### Use Cases Enabled

**Satellite Communications:**

```python
control.set_doppler(ssrc=12345, doppler_hz=-5000, doppler_rate_hz_per_sec=100)
```

**Coherent AM Reception:**

```python
control.set_pll(ssrc=12345, enable=True, bandwidth_hz=50)
control.set_envelope_detection(ssrc=12345, enable=False)
```

**Independent Sideband:**

```python
control.set_independent_sideband(ssrc=12345, enable=True)
control.set_output_channels(ssrc=12345, channels=2)
```

**Opus Audio Streaming:**

```python
control.set_output_encoding(ssrc=12345, encoding=Encoding.OPUS)
control.set_opus_bitrate(ssrc=12345, bitrate=64000)
```

**Spectrum Analysis:**

```python
control.set_spectrum(ssrc=12345, bin_bw_hz=100, bin_count=512)
```

### Breaking Changes

⚠️ **None** - This release is 100% backward compatible. All existing code will continue to work without modification.

### Implementation Details

- All new methods follow existing design patterns
- Comprehensive input validation with clear error messages
- Full logging support for debugging
- Proper docstrings with examples for all methods
- Thread-safe operation via existing infrastructure
- ~400 lines of new, tested code

### Verification

✅ All code compiles successfully  
✅ All imports validated  
✅ 20 new methods confirmed available  
✅ Pattern consistency verified  
✅ Documentation complete

### Migration Guide

No migration needed - all changes are additive. Simply update to v3.0.0 and start using the new features as needed.

For examples, see:

- `NEW_FEATURES.md` for detailed feature documentation
- `QUICK_REFERENCE.md` for quick code examples
- `examples/advanced_features_demo.py` for working demonstrations

---

## [2.5.0] - 2025-11-30

### Added - RTP Recorder Enhancement

- **`pass_all_packets` parameter** in `RTPRecorder.__init__()`
  - Allows bypassing internal packet resequencing logic
  - When `True`, passes ALL packets to callback regardless of sequence gaps
  - Metrics still track sequence errors, dropped packets, and timestamp jumps
  - Designed for applications with external resequencers (e.g., signal-recorder's PacketResequencer)
  - `max_packet_gap` parameter ignored when `pass_all_packets=True`
  - No resync state transitions - stays in RECORDING mode continuously

- **Updated `_validate_packet()` method** in `RTPRecorder`
  - Conditional resync triggering based on `pass_all_packets` flag
  - Metrics tracking independent of pass-through mode
  - Early return for pass-all mode to bypass resync state handling
  - Preserves original behavior when `pass_all_packets=False` (default)

### Documentation

- **API Reference** - Added `pass_all_packets` parameter documentation
  - Added example showing external resequencer usage
  - Updated parameter descriptions

- **Implementation Guide** - Usage patterns for external resequencing

### Backward Compatibility

100% backward compatible - `pass_all_packets` defaults to `False`, preserving existing behavior.

---

## [2.4.0] - 2025-11-29

### Added - RTP Destination Control

- **`encode_socket()` function** in `ka9q/control.py`
  - Encodes IPv4 socket addresses in radiod's TLV format (6-byte format)
  - Validates IP addresses and port numbers
  - Matches radiod's `decode_socket()` expectations exactly

- **`_validate_multicast_address()` function** in `ka9q/control.py`
  - Validates multicast address format (IP or hostname)
  - Allows both IP addresses and DNS names

- **`destination` parameter** to `create_channel()` method
  - Accepts format: "address" or "address:port"
  - Examples: "239.1.2.3", "wspr.local", "239.1.2.3:5004"
  - Enables per-channel RTP destination control

- **Full `destination` support** in `tune()` method
  - Removed "not implemented" warning
  - Properly encodes destination socket addresses
  - Supports port specification (defaults to 5004)

- **Comprehensive documentation**:
  - `RTP_DESTINATION_FEATURE.md` - Feature documentation with examples
  - `CONTROL_COMPARISON.md` - Command-by-command comparison with control.c
  - `IMPLEMENTATION_STATUS.md` - Complete status of ka9q-python vs control/tune
  - `WEBUI_SUMMARY.md` - Web UI implementation details

- **Test suite** for socket encoding
  - `tests/test_encode_socket.py` - Comprehensive tests for encode_socket()
  - Tests roundtrip encoding/decoding
  - Tests error handling and validation

### Added - Web UI

- **Complete web-based control interface** (`webui/` directory)
  - `webui/app.py` - Flask backend with REST API
  - `webui/templates/index.html` - Modern responsive UI
  - `webui/static/style.css` - Dark theme styling
  - `webui/static/app.js` - Frontend logic with auto-refresh
  - `webui/start.sh` - Quick start script
  - `webui/requirements.txt` - Python dependencies
  - `webui/README.md` - Complete usage documentation

- **Web UI Features**:
  - Auto-discovery of radiod instances on LAN
  - Pull-down selector for radiod instances
  - Channel list with frequency, mode, sample rate, destination
  - Real-time channel monitoring (1-second auto-refresh)
  - 4-column layout: Tuning, Filter, Output, Signal
  - Full-width Gain & AGC section
  - Live SNR, baseband power, noise density
  - Error handling with auto-stop after 3 consecutive failures
  - Responsive design (desktop, tablet, mobile)
  - No framework dependencies (vanilla JavaScript)

- **Web UI API Endpoints**:
  - `GET /api/discover` - Discover radiod instances
  - `GET /api/channels/<address>` - List channels
  - `GET /api/channel/<address>/<ssrc>` - Get channel status
  - `POST /api/tune/<address>/<ssrc>` - Tune channel

### Fixed

- **Deduplication** in `discover_radiod_services()`
  - Uses dict with address as key to remove duplicates
  - Sorts results by name for consistency
  - Fixes multiple entries from avahi-browse (IPv4/IPv6, multiple interfaces)

- **Timeout handling** in web UI
  - Increased backend timeout from 2s to 10s
  - Added consecutive error tracking (stops after 3 failures)
  - Better error messages for inactive channels
  - Prevents refresh interval overlap

- **Channel selection** in web UI
  - Fixed bouncing between multiple channels
  - Only one channel refreshes at a time
  - Proper cleanup when switching channels
  - Added `isRefreshing` flag to prevent race conditions

### Changed

- **Control comparison** - Documented 52.5% coverage of control.c commands
  - 21 commands fully implemented (all core functionality)
  - 19 advanced commands not yet implemented (PLL, squelch, etc.)
  - 100% coverage of tune.c commands

- **Implementation status** - Confirmed ka9q-python is production-ready
  - Complete frequency control
  - Complete mode/preset control
  - Complete filter control (including Kaiser beta)
  - Complete AGC control (all 5 parameters)
  - Complete gain control (manual, RF gain, RF atten)
  - Complete output control (sample rate, encoding, destination)
  - Complete status interrogation

## Summary of Changes

This release adds:

1. **Per-channel RTP destination control** - Clients can now specify unique RTP destinations for each channel
2. **Web UI** - Modern browser-based interface for monitoring and controlling radiod
3. **Better discovery** - Deduplicated radiod instance discovery
4. **Comprehensive documentation** - Full comparison with control.c and implementation status

### Files Modified

- `ka9q/control.py` - Added encode_socket(), updated tune() and create_channel()
- `ka9q/discovery.py` - Fixed deduplication in discover_radiod_services()
- `tests/test_encode_socket.py` - New test suite

### Files Added

- `webui/` - Complete web UI implementation (7 files)
- `RTP_DESTINATION_FEATURE.md` - Feature documentation
- `CONTROL_COMPARISON.md` - Command comparison
- `IMPLEMENTATION_STATUS.md` - Implementation status
- `WEBUI_SUMMARY.md` - Web UI documentation
- `CHANGELOG.md` - This file

### Backward Compatibility

All changes are backward compatible. Existing code continues to work without modification.
The `destination` parameter is optional in both `create_channel()` and `tune()`.
