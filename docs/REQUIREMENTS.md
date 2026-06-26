# ka9q-python — Requirements Specification

**Status:** v0.1 baseline (retroactive). **Owner:** Michael Hauan (AC0G).
**Last reconciled against code:** ka9q-python `3.16.1` (`v3.16.1-17-g10f7e47`, ka9q-radio pin `9b742e60`) (2026-06-25).
**Prefix:** `KQP`.

> Retroactive, greenfield-grade application of
> [sigmond/docs/REQUIREMENTS-TEMPLATE.md](https://github.com/HamSCI/sigmond/blob/main/docs/REQUIREMENTS-TEMPLATE.md)
> to a **library**, not a contract client. ka9q-python is the shared Python
> binding to ka9q-radio's `radiod` (TLV status/command API + RTP MultiStream
> subscription) that *every* RF client in the suite imports. It therefore has
> **no** sigmond client-contract surface of its own (no `inventory`/`validate`,
> no `deploy.toml`, no systemd unit). Its "interface" is its **public Python
> API** plus the **radiod wire protocol** it tracks; §8.3 documents API
> stability and the upstream-ka9q-radio compatibility surface rather than the
> [client contract](https://github.com/HamSCI/sigmond/blob/main/docs/CLIENT-CONTRACT.md).
> Provenance tags: `[DOC]` documented · `[CODE]` implicit-in-code · `[NEW]`
> surfaced by this review. Status: ✅ implemented · 🟡 partial/unverified · ⬜ planned.

## 1. Context & problem statement

Every RF client in the HamSCI/DASI2 suite consumes its IQ from one place:
`radiod` (the ka9q-radio daemon), which publishes demodulated/IQ channels as
RTP MultiStreams over multicast UDP and exposes a binary TLV (Type-Length-Value)
status/command protocol for provisioning those channels. Without a shared,
correct Python binding, each client (wspr-recorder, psk-recorder, hf-timestd,
hfdl-recorder, codar-sounder, superdarn-sounder, meteor-scatter, hf-tec) would
re-implement RTP receive, packet resequencing, status decode, and channel
provisioning — duplicating subtle, timing-critical code and drifting against
the C protocol independently. ka9q-python is that single binding: it owns the
RTP receive path and the radiod control path on behalf of the whole suite.

This is the integration substrate identified as **PSWS charette issue #6:30**
("ka9q-python / radiod interface"). Because it is imported, not invoked, its
objectives/inputs/outputs **are an API contract**: a breaking change to
`RadiodControl.ensure_channel(...)`, `MultiStream`, the `ChannelStatus` decode,
or the `StatusType`/`Encoding` enums ripples into every downstream service at
once. The library's defining design principle follows from that: it tracks a
*pinned* ka9q-radio commit, regenerates its protocol enums from the C headers,
and ships a drift watcher so the suite learns about a wire-protocol change
*before* deploying a radiod that would silently break RTP delivery.

The library is **mature**: 39 test files (~375 collected cases), four layered
stream abstractions, a typed status decoder covering 117 TLV parameters, CLI +
TUI, published to PyPI (`Development Status :: 4 - Beta`), and in production
under every suite recorder. Most of its requirements are therefore `[CODE]✅` —
the honest retroactive picture of a binding that was built before its
requirements were written down.

## 2. Goals & objectives

- Provide one **correct, shared** Python receive path for radiod RTP
  (multicast, resequenced, gap-aware) so no client re-implements it.
- Provide one **radiod control path** (TLV) for channel create/tune/ensure/
  remove, including wideband channels via `ensure_channel(low_edge, high_edge)`.
- Decode **every radiod RTP encoding** and the full typed **status** surface so
  clients read frequency/preset/sample-rate/timing fields without parsing bytes.
- Deliver **sample-accurate RTP↔wallclock** timing (GPS_TIME/RTP_TIMESNAP) — the
  substrate hf-timestd's whole timing hierarchy stands on.
- Keep the **public API stable** across the suite, and keep its protocol
  definitions **provably in sync** with a pinned ka9q-radio commit (drift
  detectable before deploy).
- Stay **pure-Python, NumPy-only** at runtime so it installs as an editable
  sibling into every consumer venv with no compiled/optional weight on the core.

## 3. Non-goals / out of scope

- **Being radiod.** It does not demodulate, tune hardware, or run an SDR — it
  talks to `radiod`, which talks to the SDR. (Owner: ka9q-radio.)
- **Being a sigmond client.** It has no `inventory`/`validate`/`deploy.toml`/
  systemd unit and is never lifecycle-managed; sigmond consumes it transitively
  through the clients that import it. (Owner: each client + sigmond.)
- **Domain science / decoding.** WSPR/FT8/timing/Doppler logic lives in the
  consuming clients and in `hamsci-dsp`, never here.
- **Owning the ka9q-radio pin policy at deploy time.** The library *declares*
  the validated commit and *detects* drift; deciding when to advance a
  deployed radiod is sigmond's `smd watch ka9q` / operator concern.
- **Cross-host orchestration.** Multicast discovery is per-LAN/per-interface;
  fleet coordination is sigmond/PSWS scope.

## 4. Stakeholders & actors

ka9q-python's "actors" are its consumers and the protocol it tracks:

- **`radiod`** (ka9q-radio) — the controlled peer: RTP IQ/audio source and TLV
  status/command server. The pinned C headers are the authority for the wire
  protocol.
- **Consuming suite clients** (the API consumers) — wspr-recorder, psk-recorder,
  hf-timestd, hfdl-recorder, codar-sounder, superdarn-sounder, meteor-scatter,
  hf-tec. Each imports `from ka9q import …`; their RTP receive + provisioning is
  this library's API.
- **`hamsci-dsp`** — sibling shared lib; pairs with ka9q-python (timing/DSP) in
  the same consumer venvs.
- **sigmond** — does not import the runtime package, but wraps
  `scripts/check_upstream_drift.py` as `smd watch ka9q` (drift watcher) and
  installs ka9q-python as an editable sibling into every consumer venv.
- **Operators / developers** — `ka9q` CLI + TUI for interactive probing/tuning;
  `sync_types.py` maintainer regenerating enums on a ka9q-radio bump.
- **The PyPI ecosystem** — published package; the `ka9q-radio` upstream project
  is the moving target the compat surface defends against.

## 5. Assumptions & constraints

- `KQP-C-001` `[DOC]` ✅ Runtime SHALL be **pure Python with NumPy as the only
  hard dependency** (`numpy>=1.24.0`); `textual`/`opuslib` are opt-in extras
  (`tui`/`opus`) and `pytest` is `dev`.
- `KQP-C-002` `[DOC]` ✅ SHALL support **Python ≥3.9** (classifiers through 3.12).
- `KQP-C-003` `[CODE]` ✅ The transport SHALL be **multicast UDP**; there is no
  unicast radiod fallback. Multi-homed hosts SHALL select an interface explicitly.
- `KQP-C-004` `[DOC]` ✅ Protocol enums (`StatusType`, `Encoding`, `DemodType`,
  `WindowType`) SHALL be **generated from ka9q-radio's C headers** (`status.h`,
  `rtp.h`), never hand-edited, and pinned to a recorded commit.
- `KQP-C-005` `[CODE]` ✅ SHALL install as an **editable sibling**
  (`[tool.uv.sources] path=…, editable=true`) into each consumer venv so a
  `git pull` propagates without reinstall; the library's `uv.lock` is gitignored
  (a library does not bind downstream consumers — they pin it in their own lock).
- `KQP-C-006` `[CODE]` ✅ Import name SHALL be `ka9q`; PyPI/distribution name
  SHALL be `ka9q-python` (KA9Q attribution). These SHALL NOT be conflated.

## 6. Functional requirements

### 6.1 radiod control (TLV command path)
- `KQP-F-001` `[DOC]` ✅ `RadiodControl` SHALL implement the ka9q-radio TLV
  binary protocol over multicast and expose channel create/tune/configure/
  destroy, guarded by an `RLock` for concurrent use.
- `KQP-F-002` `[DOC]` ✅ SHALL provide `ensure_channel(frequency_hz, preset,
  sample_rate, encoding, …)` — idempotent provision-or-reuse keyed on a
  frequency tolerance — returning the channel info incl. SSRC.
- `KQP-F-003` `[DOC]` ✅ `ensure_channel`/`create_channel` SHALL accept
  **`low_edge`/`high_edge`** (and `kaiser_beta`) so wideband consumers
  (codar/superdarn) provision an un-clipped filter rather than the ±audio path.
- `KQP-F-004` `[DOC]` ✅ SHALL remove channels by setting **frequency = 0**
  (`remove_channel(ssrc)` / `set_frequency(ssrc, 0.0)`) so radiod's poller
  reclaims them; teardown SHALL be the documented contract.
- `KQP-F-005` `[CODE]` ✅ SHALL derive a **deterministic SSRC** (`allocate_ssrc`)
  and **deterministic multicast IP/destination** (`generate_multicast_ip`,
  radiod-host-aware) from channel parameters so identities survive restarts and
  don't collide.
- `KQP-F-006` `[DOC]` ✅ SHALL support explicit per-channel `lifetime` (TTL) and
  output `encoding`/`destination` selection.

### 6.2 RTP receive abstractions (four layers)
- `KQP-F-010` `[DOC]` ✅ SHALL expose **`RTPRecorder`** — raw-packet capture with
  precise GPS/RTP timestamps for timing-critical consumers.
- `KQP-F-011` `[DOC]` ✅ SHALL expose **`RadiodStream`** — continuous sample
  delivery with gap handling, built on `PacketResequencer` for out-of-order
  packets; SHALL bind to the **channel's multicast group**, not `0.0.0.0`.
- `KQP-F-012` `[DOC]` ✅ SHALL expose **`ManagedStream`** — self-healing
  single-channel wrapper that recovers across radiod restarts / network drops.
- `KQP-F-013` `[DOC]` ✅ SHALL expose **`MultiStream`** — one socket per
  multicast group demultiplexing many SSRCs, the substrate every multi-band
  recorder uses (avoids kernel over-subscription from per-channel sockets);
  channels added via `add_channel(...)`, with pruning of dead SSRCs.
- `KQP-F-014` `[DOC]` ✅ SHALL expose **`SpectrumStream`** for spectrum/`powers`
  consumers.

### 6.3 RTP payload decode
- `KQP-F-020` `[DOC]` ✅ `parse_rtp_samples()` SHALL decode **every native
  radiod encoding** — S16LE/BE, F32LE/BE, F16LE/BE, MULAW, ALAW — in pure NumPy.
- `KQP-F-021` `[DOC]` ✅ SHALL decode **OPUS / OPUS_VOIP** via an optional
  `OpusDecoder` (`[opus]` extra), degrading to ImportError-guarded absence when
  not installed (core stays NumPy-only).
- `KQP-F-022` `[DOC]` ✅ SHALL parse RTP headers (`parse_rtp_header`) and expose
  IQ (`complex64`) vs real sample framing per `OUTPUT_CHANNELS`.

### 6.4 Typed status decode
- `KQP-F-030` `[DOC]` ✅ SHALL decode radiod TLV status packets
  (`decode_status_packet`) into typed objects — `ChannelStatus`,
  `FrontendStatus`, `PllStatus`, `FmStatus`, `SpectrumStatus`, `Filter2Status`,
  `OpusStatus` — with dotted-path field access, covering all 117 TLV parameters.
- `KQP-F-031` `[DOC]` ✅ SHALL provide a `StatusListener` that refreshes the
  per-channel timing anchor at sub-second cadence for live consumers.

### 6.5 Timing
- `KQP-F-040` `[DOC]` ✅ SHALL map RTP timestamps to wallclock
  (`rtp_to_wallclock`) using radiod's **GPS_TIME / RTP_TIMESNAP**, sample-accurate.
- `KQP-F-041` `[CODE]` ✅ `ChannelInfo` SHALL expose an **atomic anchor pair**
  (`get_anchor`/`update_anchor` on `(gps_time, rtp_timesnap)`) so a live
  `StatusListener` refresh cannot yield a torn anchor to `rtp_to_wallclock`.

### 6.6 Discovery
- `KQP-F-050` `[DOC]` ✅ SHALL enumerate radiod instances and their active
  channels over the LAN: `discover_channels()` primary, with
  `discover_channels_native()` / `discover_channels_via_control()` fallbacks and
  `discover_radiod_services()`; SHALL accept an explicit `interface`.
- `KQP-F-051` `[DOC]` ✅ SHALL provide `ChannelMonitor` to detect radiod restarts
  and fire channel-recreation callbacks.

### 6.7 CLI / TUI
- `KQP-F-060` `[DOC]` ✅ SHALL ship a `ka9q` console entry point: `list / query /
  set / tune / tui` for scripted and interactive control.
- `KQP-F-061` `[DOC]` ✅ The TUI (`ka9q tui`, `[tui]` extra) SHALL be an additive
  Textual lazy-import; absence of `textual` SHALL NOT break the core/CLI.

### 6.8 Protocol-drift tooling (dev/repo surface, not runtime API)
- `KQP-F-070` `[DOC]` ✅ `scripts/sync_types.py` SHALL regenerate `ka9q/types.py`
  and the pin files (`ka9q_radio_compat`, `ka9q/compat.py`) from a local
  ka9q-radio checkout, with `--check` (exit 1 on drift) / `--diff` / `--apply`,
  updating the three files **atomically**.
- `KQP-F-071` `[DOC]` ✅ `scripts/check_upstream_drift.py` SHALL compare the
  *pinned* commit against `origin/main` and classify the delta **pass / warn /
  fail**, where `fail` = a **stream-critical** field removed or its TLV/enum
  value shifted (RTP delivery would break). This is the script sigmond's
  `smd watch ka9q` wraps.
- `KQP-F-072` `[CODE]` ✅ The stream-critical allowlist SHALL enumerate the
  fields whose change is breaking — `OUTPUT_DATA_DEST_SOCKET`,
  `OUTPUT_DATA_SOURCE_SOCKET`, `OUTPUT_SSRC`, `OUTPUT_TTL`, `OUTPUT_SAMPRATE`,
  `OUTPUT_ENCODING`, `OUTPUT_CHANNELS`, `RTP_PT`, `RTP_TIMESNAP`, `GPS_TIME`,
  `STATUS_INTERVAL`, `RADIO_FREQUENCY`, `PRESET`, `DEMOD_TYPE`, `LIFETIME` — plus
  the all-values-critical enums `Encoding`, `DemodType`. It SHALL live in the
  repo dev tool, **not** the `ka9q/` runtime package.
- `KQP-F-073` `[DOC]` ✅ `tests/test_protocol_compat.py` SHALL fail on drift when
  `../ka9q-radio` is present and **auto-skip** when it is not (CI without the C
  tree unaffected).

## 7. Quality / non-functional requirements

- `KQP-Q-001` `[DOC]` ✅ **API stability:** the public surface re-exported from
  `ka9q/__init__.py` (`__all__`) SHALL be treated as a versioned contract;
  breaking changes SHALL bump the version and be reconciled across the named
  consumers before release (see §8.3).
- `KQP-Q-002` `[CODE]` ✅ All public `RadiodControl` methods SHALL be
  thread-safe (`RLock`); `ManagedStream`/`MultiStream` SHALL be safe for
  concurrent long-running use.
- `KQP-Q-003` `[CODE]` ✅ Receive sockets SHALL set **`SO_RCVBUF` = 64 MB** on
  both `RadiodStream` and `MultiStream` to resist GIL-stall packet loss.
- `KQP-Q-004` `[CODE]` ✅ `ManagedStream` SHALL recover automatically from
  radiod restart and network interruption without consumer intervention.
- `KQP-Q-005` `[DOC]` ✅ Protocol definitions SHALL be **provably in sync** with
  the pinned ka9q-radio commit (drift detectable by `--check` in CI and by
  `check_upstream_drift.py` against upstream HEAD).
- `KQP-Q-006` `[CODE]` ✅ The anchor refresh SHALL be **atomic** so timing
  consumers (hf-timestd's tight ±0.5 s gates) never read a torn pair.
- `KQP-Q-007` `[DOC]` ✅ Multi-homed operability: every receive/discovery/control
  entry point SHALL accept an explicit interface selector.
- `KQP-Q-008` `[CODE]` ✅ Optional capabilities (OPUS, TUI) SHALL degrade to a
  guarded absence and SHALL NEVER hard-fail the NumPy-only core import.
- `KQP-Q-009` `[NEW]` 🟡 **Public-API regression guard:** there is no automated
  test asserting `ka9q/__init__.__all__` is stable (no symbol silently dropped/
  renamed). A breaking export change today would only surface in a consumer.
  SHALL add an `__all__` snapshot test. *(gap.)*

## 8. External interfaces

> This is a library: §8.1/§8.2 describe the **Python API** it provides and the
> **radiod wire I/O** it speaks, not files/sinks. §8.3 is the stability +
> upstream-compat surface in place of a client-contract conformance statement.

### 8.1 Inputs (API consumed by callers; wire consumed from radiod)
- **From callers:** `RadiodControl(host, interface=…)`; channel params
  (`frequency_hz`, `preset`, `sample_rate`, `encoding`, `low_edge`/`high_edge`,
  `lifetime`, `destination`); stream construction (`MultiStream` + `add_channel`,
  `RadiodStream`, `RTPRecorder(channel=…, on_packet=…)`); discovery
  (`discover_channels(host, interface=…)`).
- **From radiod (wire):** TLV status/command packets over multicast UDP; RTP
  payload streams (S16/F32/F16/MULAW/ALAW/OPUS) carrying GPS_TIME/RTP_TIMESNAP.
- **Env:** `RADIOD_HOST` / `RADIOD_ADDRESS` (test/CLI host selection);
  `--radiod-host` pytest option for integration tests.

### 8.2 Outputs (API returned to callers; wire emitted to radiod)
- **To callers:** decoded samples (`complex64` IQ / real), typed status objects
  (`ChannelStatus`, `FrontendStatus`, …), `ChannelInfo` (SSRC, freq, preset,
  sample_rate, anchor pair), gap events / stream quality, wallclock timestamps,
  discovered-channel maps; typed exceptions (`Ka9qError`, `ConnectionError`,
  `CommandError`, `ValidationError`).
- **To radiod (wire):** TLV command packets (create/tune/ensure/remove,
  frequency=0 teardown) on the channel's multicast group; deterministic SSRC /
  multicast IP.
- **CLI stdout:** `ka9q list/query/set/tune` human + scriptable output.
- **Pins (repo artifacts):** `ka9q_radio_compat` + `ka9q/compat.py`
  (`KA9Q_RADIO_COMMIT`) declaring the validated ka9q-radio commit.

### 8.3 Contract / API stability & upstream-compat surface (reference, not restated)

> **The HamSCI client contract does NOT apply to ka9q-python.** It is a library,
> not a client: no `inventory --json`, no `validate --json`, no `deploy.toml`,
> no systemd unit, no shared-sink writes. It is not lifecycle-managed by sigmond
> and does not self-describe to the contract adapter. The two interfaces it DOES
> own are below.

- `KQP-I-001` `[CODE]` ✅ **Public Python API contract.** The stable surface is
  the `ka9q/__init__.__all__` re-exports — Control (`RadiodControl`,
  `allocate_ssrc`); Discovery (`discover_channels*`, `ChannelInfo`); Streams
  (`RTPRecorder`, `RadiodStream`, `ManagedStream`, `MultiStream`,
  `SpectrumStream`); Decode (`parse_rtp_samples`, `decode_status_packet`, the
  `*Status` types); Types (`StatusType`, `Encoding`, `DemodType`, `WindowType`);
  Exceptions; Utilities (`generate_multicast_ip`, `ChannelMonitor`,
  `rtp_to_wallclock`). The eight named consumers depend on these symbols and on
  `from ka9q.types import StatusType, Encoding` (enum **names and values**).
  Breaking changes are coordinated with those consumers (§10) and versioned.
- `KQP-I-002` `[DOC]` ✅ **radiod wire-protocol compat surface.** The library is
  pinned to ka9q-radio commit `9b742e60` (`ka9q_radio_compat` /
  `KA9Q_RADIO_COMMIT`); `types.py` is generated from that commit's `status.h`/
  `rtp.h`. The stream-critical field set (`KQP-F-072`) defines what a wire change
  must not silently break: the **fail** classification means RTP delivery to the
  whole suite would break if the deployed radiod advanced past a value shift
  without a coordinated ka9q-python regen. Upstream tracking is sigmond
  `smd watch ka9q` → `check_upstream_drift.py`.
- `KQP-I-003` `[DOC]` ✅ **sigmond seam.** sigmond consumes ka9q-python only
  (a) transitively, through clients that import it (editable sibling install per
  the fleet-upgrade pattern), and (b) as the drift watcher wrapper. There is no
  direct runtime import of `ka9q` by `smd` core (which is stdlib-only).

## 9. Data requirements

ka9q-python is **stateless and persists nothing** — no database, no on-disk
products, no retention. Its in-flight data structures: `ChannelInfo` (SSRC,
frequency, preset, sample_rate, encoding, atomic `(gps_time, rtp_timesnap)`
anchor); the typed `*Status` decode objects (117 TLV fields, dotted-path);
`complex64` IQ / real sample buffers; `RTPHeader` / `RTPPacket` /
`ResequencerStats` / `StreamQuality` / `GapEvent` runtime telemetry. The only
durable artifacts are the **dev pins** (`ka9q_radio_compat`, `compat.py`) and
the **generated** `ka9q/types.py` — provenance-labeled with the ka9q-radio
commit they were validated against. Wire timing provenance (GPS_TIME/
RTP_TIMESNAP) is passed through, never stored.

## 10. Dependencies & development sequence

**Runtime deps:** `numpy>=1.24.0` (only hard dep). **Optional extras:** `tui`
(`textual>=0.50`), `opus` (`opuslib>=3.0`), `dev` (`pytest`, `pytest-cov`).
**External peer:** a running `radiod` (ka9q-radio) at the pinned commit; the
ka9q-radio C source tree at `../ka9q-radio` enables `sync_types.py` regen and
the drift test (both auto-skip without it).

**Must exist first:** ka9q-radio (radiod + headers) — ka9q-python is the binding
to it, so it cannot precede it. Everything downstream (`hamsci-dsp`, all eight
RF clients) depends on this library, so it sits at the **base** of the suite
dependency graph; a breaking change here is the highest-blast-radius change in
the suite.

**Development sequence (intended, recovered as requirement):**
1. **Control + decode core** — `RadiodControl` TLV path, `parse_rtp_samples`,
   typed status decode, generated `types.py` + the compat pin.
2. **Stream abstraction ladder** — `RTPRecorder` → `RadiodStream`
   (+ resequencer) → `ManagedStream` (self-heal) → `MultiStream`
   (shared-socket multi-SSRC, the recorder substrate).
3. **Timing hardening** — GPS_TIME/RTP_TIMESNAP `rtp_to_wallclock`, then the
   `StatusListener` sub-second anchor refresh + atomic anchor pair (3.16.x).
4. **Drift defense** — `sync_types.py` + `check_upstream_drift.py` +
   `test_protocol_compat.py`, wired into sigmond as `smd watch ka9q`.
5. **Ergonomics** — discovery, CLI, TUI, multi-homed selection, `[opus]` decode.
6. **Performance** — 64 MB `SO_RCVBUF` on both stream paths; multicast-group
   bind fix (g10f7e47).

Ongoing maintenance cadence: when ka9q-radio advances, run the watcher; regen
on green/yellow; coordinate consumers on red (§KQP-F-071/072).

## 11. Acceptance criteria & verification

- **Protocol sync** → `python scripts/sync_types.py --check` exits 0 against
  `../ka9q-radio`; `tests/test_protocol_compat.py` passes (or skips absent the
  tree). `check_upstream_drift.py` classification surfaced via `smd watch ka9q`.
- **Stream correctness** → the unit suite (39 files, ~375 cases): resequencer,
  multistream prune, ensure-channel encoding, SSRC/destination derivation,
  RTP-sample parse (incl. IQ 20 kHz F32), managed-stream recovery, timing fields.
- **Live integration** → `uv run pytest --radiod-host=<host>` against a real
  radiod (e.g. `bee1-hf-status.local`).
- **API stability** → `KQP-Q-001` is today verified only by downstream breakage;
  acceptance is the proposed `__all__` snapshot test (`KQP-Q-009`).
- **Decode coverage** → per-encoding parse tests for S16/F32/F16/MULAW/ALAW;
  OPUS path gated on `[opus]`.
- **Real-world acceptance** → in production under all suite recorders; a clean
  RTP receive (no USB/packet drops attributable to the binding) is the standing
  field check.

## 12. Risks & open questions

- `KQP-Q-009` `[NEW]` 🟡 **No `__all__` regression guard** — the API contract
  (§8.3 `KQP-I-001`) every client depends on has no automated stability test; a
  dropped/renamed export ships silently. *(candidate #18 issue.)*
- `KQP-D-001` `[NEW]` ⬜ **No machine-readable consumer-compat matrix.** Which
  ka9q-python version each client requires lives only in eight separate
  `uv.lock`/pyproject pins; there is no single declared "client X needs API ≥N"
  map, so a breaking bump's blast radius must be reasoned out by hand. SHALL
  publish a compat matrix (or a contract-style version floor per consumer).
- `KQP-F-074` `[NEW]` ⬜ **Drift watcher is operator-triggered only.** No
  scheduler runs `check_upstream_drift.py`; a stream-critical upstream change can
  sit undetected until someone reruns it before a deploy. SHALL either schedule
  it (sigmond timer) or document the manual-before-deploy gate as the accepted
  control.
- `KQP-Q-010` `[NEW]` ⬜ **Beta classifier vs production reality.** pyproject
  declares `Development Status :: 4 - Beta` though the library is the production
  substrate for the whole suite; either promote to `5 - Production/Stable` or
  document why it's held at Beta (API still mutating).
- **Doc/code surface drift:** README/CLAUDE list `RadiodControl` and the four
  stream layers consistently, but the API-surface ground truth is
  `__init__.__all__`; keep `docs/API_REFERENCE.md` reconciled against it (no
  enforced check today).
- **Pin policy clarity:** `ka9q_radio_compat` (`9b742e60`) is the *validated*
  commit; the deployed radiod commit is sigmond/operator-controlled. The
  library cannot enforce that the running radiod matches its pin — it can only
  detect upstream drift. This boundary SHALL stay explicit (§KQP-I-002/003).

## 13. Traceability

| Requirement | #18 issue | Verification | PSWS #6 |
|---|---|---|---|
| KQP-I-001 (public API contract) | Clients: ka9q-python API stability | downstream import + (proposed) `__all__` test | #6:30 |
| KQP-I-002 (radiod wire compat / pin) | ka9q-watch | `sync_types --check`, `test_protocol_compat` | #6:30 |
| KQP-F-071/072 (drift classification) | smd watch ka9q | `check_upstream_drift.py` pass/warn/fail | #6:30 |
| KQP-F-013 (MultiStream substrate) | — | `test_multistream_prune`, recorder field use | #6:31 (sensor integ.) |
| KQP-F-040/041 (RTP↔wallclock, atomic anchor) | Clients: hf-timestd timing | timing-fields test; 3.16.1 anchor tests | #6:50 (timing tiering) |
| KQP-Q-009 (`__all__` guard) | *(new — file)* | snapshot test | — |
| KQP-D-001 (consumer-compat matrix) | *(new — file)* | published matrix | #6:30 |
| KQP-F-074 (scheduled drift watch) | *(new — file)* | sigmond timer / documented gate | #6:30 |
| KQP-Q-010 (Beta→Stable classifier) | *(new — file)* | pyproject review | — |

*New rows (KQP-Q-009, KQP-D-001, KQP-F-074, KQP-Q-010) are this review's surfaced
gaps; promote to #18 under the ka9q-python / PSWS #6:30 interface epic.*
