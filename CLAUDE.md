# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Package Overview

**ka9q-python** is a Python library for controlling [ka9q-radio](https://github.com/ka9q/ka9q-radio)'s `radiod` daemon. The package is named `ka9q-python` on PyPI (out of respect for KA9Q / Phil Karn's callsign) but imported as `import ka9q`.

## Commands

### Install for development
```bash
uv sync --extra dev                    # standard; creates .venv/
# or: pip install -e ".[dev]"          # pip fallback
```

### Run tests
```bash
# All tests (~415 collected; unit tests run without a live radiod)
uv run pytest

# Integration tests against a live radiod host
uv run pytest --radiod-host=bee1-status.local
# or via environment variable
RADIOD_HOST=bee1-status.local uv run pytest

# Single test file / one test
uv run pytest tests/test_control.py -v
uv run pytest tests/test_control.py::TestClass::test_X
uv run pytest -k addressing -v                # by keyword

# With coverage
uv run pytest --cov=ka9q --cov-report=html
```

### Build distribution
```bash
uv build                               # produces sdist + wheel in dist/
```

### Library lockfile policy
`uv.lock` is gitignored for this library; a library's lockfile does not bind
downstream consumers. Clients pin ka9q-python via their own `uv.lock`.

## Architecture

### Abstraction Layers

The library exposes four progressively higher-level abstractions for consuming RTP audio streams:

1. **`RTPRecorder`** (`rtp_recorder.py`) — Low-level raw packet capture with precise GPS/RTP timestamps. Use when timing accuracy is critical (e.g., WSPR, scientific measurement).

2. **`RadiodStream`** (`stream.py`) — Mid-level continuous sample delivery with automatic gap filling. Built on `PacketResequencer` (`resequencer.py`) for out-of-order packet handling.

3. **`ManagedStream`** (`managed_stream.py`) — High-level self-healing wrapper around `RadiodStream` that automatically recovers from radiod restarts and network interruptions.

4. **`MultiStream`** (`multi_stream.py`) — Multi-channel multiplexer: one socket per multicast group, demultiplexes by SSRC across many channels. This is the substrate the sigmond recorders that multiplex many bands over one multicast group use (hf-timestd, wspr-recorder, psk-recorder, meteor-scatter, hfdl-recorder) because radiod publishes many bands into one multicast group and a per-channel socket would over-subscribe the kernel. (codar-sounder, hf-tec, and superdarn-sounder instead take a single `RadiodStream` per channel — they don't multiplex.)

### Core Components

- **`RadiodControl`** (`control.py`, ~2800 lines) — The central control class. Implements the TLV (Type-Length-Value) binary protocol used by ka9q-radio. All channel operations (create, tune, configure, destroy) go through this class. 110+ protocol constants are defined in `types.py` as `StatusType` enum values, mirroring `status.h` in ka9q-radio.

- **`discovery.py`** — Channel and service discovery via multicast UDP. `discover_channels()` is the primary entry point; it has fallbacks to `discover_channels_native()` and `discover_channels_via_control()`.

- **`monitor.py`** — `ChannelMonitor` detects radiod restarts and triggers channel recreation callbacks.

- **`addressing.py`** — Deterministic multicast IP and SSRC generation from frequency/parameters.

- **`utils.py`** — Cross-platform mDNS resolution and multicast socket configuration.

### Protocol Notes

- All radiod communication uses multicast UDP with TLV-encoded status packets
- SSRC (Synchronization Source) identifies each channel; `allocate_ssrc()` generates deterministic SSRCs to avoid collisions across restarts
- The default integration test radiod is `bee1-status.local`; tests use `--radiod-host` or `RADIOD_HOST` env var to override
- ka9q-python is the mandatory control path: no sigmond-suite client may talk to radiod directly (own sockets, hand-built TLVs, or ka9q-radio CLI tools).
- Exception: `ka9q-web` (a vendored third-party C dashboard) sends control commands to radiod directly and is excluded from this policy, the same as `ka9q-radio` itself — it predates the policy, it's not sigmond-authored, and it cannot import a Python library. Its exposure is operational (an upstream C tool with independent, hand-built TLV decoding), not an architectural gap in ka9q-python.

### Public API Surface

All public symbols are re-exported from `ka9q/__init__.py`. Key exports:
- Control: `RadiodControl`, `allocate_ssrc`
- Discovery: `discover_channels`, `ChannelInfo`
- Streams: `RadiodStream`, `ManagedStream`, `RTPRecorder`
- Types: `StatusType`, `Encoding`
- Exceptions: `Ka9qError`, `ConnectionError`, `CommandError`, `ValidationError`
- Utilities: `generate_multicast_ip`, `ChannelMonitor`

### Thread Safety

All public `RadiodControl` methods are protected by `RLock`. `ManagedStream` is safe for concurrent use. The library is designed for long-running applications with multiple concurrent channels.

### Upstream drift watcher

`ka9q-radio` is under active development. Two scripts watch it:

- **`scripts/sync_types.py`** — regenerates `ka9q/types.py` and the pin
  files (`ka9q_radio_compat`, `ka9q/compat.py`) from the local
  `ka9q-radio` checkout. Modes: `--check` (exit 1 on drift),
  `--diff` (dry-run), `--apply` (write).

- **`scripts/check_upstream_drift.py`** — compares the *pinned* commit
  against `origin/main` (or any `--remote`/`--branch`) and classifies
  the delta:
  - **pass** — no upstream commits, or upstream advanced but no header
    file was touched (contract intact).
  - **warn** — header touched but no stream-critical field affected
    (review for new capabilities or rename impact).
  - **fail** — a stream-critical field was removed or its TLV/enum value
    shifted. RTP delivery to clients would break if the pin advanced
    without code changes.

  The stream-critical allowlist (with rationale) is inlined at the top
  of `check_upstream_drift.py`. It is intentionally **not** part of the
  `ka9q/` runtime package — it's a repo-level dev tool concern, not API
  surface. Adding a field there raises removals/value-changes from
  yellow to red.

Operator workflow when the watcher is yellow/red:

1. Read the per-field detail in the report.
2. For *added* fields: extend ka9q-python (and downstream clients) to
   expose the new capability, then `sync_types.py --apply` to regenerate
   `types.py` and advance the pin.
3. For *removed* or *value-shifted* critical fields: coordinate with
   downstream sigmond-suite clients (hf-timestd, wspr-recorder,
   psk-recorder, meteor-scatter, hfdl-recorder, codar-sounder, hf-tec,
   superdarn-sounder, sigmond) *before* regenerating, since they
   hard-code enum names and values via
   `from ka9q.types import StatusType, Encoding`. (wsprdaemon-client
   is deprecated; no longer a coordination concern.)
4. After `--apply`: run `pytest`, then commit `types.py`,
   `ka9q_radio_compat`, and `ka9q/compat.py` together.

Sigmond exposes the watcher as `smd ka9q-watch` and as a TUI screen
(Observe → ka9q-watch).
