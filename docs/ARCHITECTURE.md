# Architecture

Internal design of ka9q-python: module layout, abstraction layers,
protocol, threading, and resource management.

For a task-oriented view of the library see [RECIPES.md](RECIPES.md).
For the CLI/TUI see [CLI_GUIDE.md](CLI_GUIDE.md) and
[TUI_GUIDE.md](TUI_GUIDE.md).

---

## Overview

ka9q-python is a pure-Python library that speaks ka9q-radio's TLV
(Type-Length-Value) multicast UDP protocol. It provides:

- **Control**: a single `RadiodControl` that implements every
  command verb radiod accepts.
- **Discovery**: mDNS service browsing and channel enumeration via
  multicast status packets.
- **Typed status**: dataclass decoders for the status wire format.
- **Streaming consumers**: four progressively higher-level patterns
  for consuming RTP audio.
- **Operator tools**: a console CLI (`ka9q`) and a Textual TUI.

Design goals: general-purpose (no application assumptions),
thread-safe, cross-platform, no C extensions.

---

## Module Structure

```
ka9q/
├── __init__.py         Package exports and version
├── control.py          RadiodControl — the central command class
├── discovery.py        discover_channels, discover_radiod_services, ChannelInfo
├── monitor.py          ChannelMonitor — restart detection + callbacks
├── addressing.py       Deterministic multicast IP and SSRC generation
├── utils.py            Cross-platform mDNS resolution, multicast socket setup
├── types.py            StatusType enum (110+), Encoding, DemodType, WindowType
├── status.py           Typed status decoders (ChannelStatus, FrontendStatus, …)
├── exceptions.py       Ka9qError hierarchy
├── compat.py           ka9q-radio commit compatibility marker
├── rtp_recorder.py     RTPRecorder — raw packet capture with GPS/RTP timestamps
├── resequencer.py      PacketResequencer — RTP reordering, gap detection
├── stream_quality.py   StreamQuality, GapEvent, GapSource
├── stream.py           RadiodStream — continuous sample delivery
├── managed_stream.py   ManagedStream — self-healing single-channel wrapper
├── multi_stream.py     MultiStream — shared-socket multi-SSRC receiver
├── spectrum_stream.py  SpectrumStream — real-time FFT bin data receiver
├── pps_calibrator.py   L6 BPSK PPS chain-delay calibration
├── cli.py              `ka9q` console script (list / query / set / tui)
└── tui.py              Textual TUI panels
```

---

## Abstraction Layers

The library exposes four consumer patterns for RTP audio, in order of
increasing abstraction:

### 1. `RTPRecorder` ([rtp_recorder.py](../ka9q/rtp_recorder.py))

Low-level raw packet capture with precise GPS/RTP timestamps. No
resequencing, no gap filling. Use this when timing fidelity matters
more than sample continuity — WSPR, scientific measurement,
propagation studies. Exposes `RTPHeader`, `RecordingMetrics`,
`parse_rtp_header()`, `rtp_to_wallclock()`.

### 2. `RadiodStream` ([stream.py](../ka9q/stream.py))

Mid-level continuous sample delivery with automatic gap filling.
Wraps `PacketResequencer` ([resequencer.py](../ka9q/resequencer.py))
to handle out-of-order RTP. Hands each batch to an
`on_samples(samples, quality)` callback. Does **not** heal radiod
restarts — a restart produces a lasting silence.

`stream.py` also exposes two payload-decoder helpers shared by every
stream class:

- `parse_rtp_samples(payload, encoding, is_iq)` — decodes every
  linear-PCM encoding radiod emits (`S16LE/BE`, `F32LE/BE`,
  `F16LE/BE`, `MULAW`, `ALAW`) to NumPy in pure Python, no
  `audioop` (gone in Py 3.13).
- `OpusDecoder(sample_rate, channels)` — decodes `OPUS` / `OPUS_VOIP`
  via the optional `opuslib` dependency (`pip install ka9q-python[opus]`).
  One instance per SSRC so codec state and PLC stay coherent.

### 3. `ManagedStream` ([managed_stream.py](../ka9q/managed_stream.py))

High-level self-healing wrapper around `RadiodStream`. A background
health thread detects silence beyond `drop_timeout_sec` and calls
`ensure_channel()` to re-provision the channel. Fires
`on_stream_dropped` / `on_stream_restored` callbacks. Best for one
or two long-running channels.

### 4. `MultiStream` ([multi_stream.py](../ka9q/multi_stream.py))

Shared-socket multi-SSRC receiver. One UDP socket, one receive
thread, N per-channel `on_samples` callbacks demultiplexed by SSRC.
Each slot has its own resequencer and quality block; the health
thread heals each slot independently. Scales to dozens of channels
on the same multicast group with O(1) socket resources. Production
users: wspr-recorder, psk-recorder, hf-timestd.

See [MULTI_STREAM.md](MULTI_STREAM.md) for depth.

### 5. `SpectrumStream` ([spectrum_stream.py](../ka9q/spectrum_stream.py))

Real-time FFT spectrum receiver. Spectrum data takes a different path
from audio: it arrives as `BIN_DATA` / `BIN_BYTE_DATA` TLV vectors
inside status packets on port 5006 (not RTP on port 5004).
`SpectrumStream` creates a `SPECT2_DEMOD` channel, polls radiod
periodically to trigger fresh FFT output, and delivers decoded
`ChannelStatus` objects (with `spectrum.bin_power_db` as a numpy
array) to an `on_spectrum` callback. Use this for spectrogram
displays, band activity monitors, and signal-search applications.

---

## Core: `RadiodControl`

[`control.py`](../ka9q/control.py) (~2800 lines) is the central class.
It implements the TLV command protocol and all setter verbs.

- 110+ StatusType constants in [types.py](../ka9q/types.py),
  mirroring `status.h` in ka9q-radio.
- TLV encoders: `encode_int64`, `encode_double`, `encode_float`,
  `encode_string`, `encode_eol`.
- TLV decoders: `decode_int`, `decode_double`, `decode_float`,
  `decode_string`, `decode_socket`.
- Input validation at every public API boundary.
- Deterministic SSRC generation via
  [addressing.py](../ka9q/addressing.py) (`allocate_ssrc`,
  `generate_multicast_ip`).

Key high-level method: `ensure_channel(frequency_hz, preset,
sample_rate, ...)` — idempotent, verifies the channel is alive before
returning its `ChannelInfo`. This is what `ManagedStream` and
`MultiStream` call internally.

---

## Typed Status Decoders

[`status.py`](../ka9q/status.py) provides dataclass decoders for
radiod's status packets:

| Class | Purpose |
|---|---|
| `FrontendStatus` | SDR / A-D / GPSDO fields (input_samprate, ad_bits_per_sample, isreal, lna_gain, mixer_gain, calibrate, rf_agc, if_power, …). Derived properties: `calibrate_ppm`, `gpsdo_reference_hz`, `input_power_dbm`. |
| `PllStatus`, `FmStatus`, `SpectrumStatus`, `Filter2Status`, `OpusStatus` | Per-demod optional sub-blocks. |
| `ChannelStatus` | Top-level channel status. Embeds `FrontendStatus` as `.frontend` and demod-specific blocks. Helpers: `to_dict()`, `get_field("dotted.path")`, `field_names()`. |
| `decode_status_packet(buf)` | Parse raw multicast bytes → `ChannelStatus`. |

`ChannelStatus.get_field()` is what the CLI's `--field` flag drives.

---

## Discovery

[`discovery.py`](../ka9q/discovery.py) has two responsibilities:

- **Service discovery** — `discover_radiod_services()` shells out to
  `avahi-browse -t _ka9q-ctl._udp`, decodes escape sequences, and
  returns deduplicated `{name, address}` dicts.
- **Channel discovery** — `discover_channels()` listens to a host's
  status multicast group for a few seconds, decodes each packet into
  a `ChannelInfo`, and returns an SSRC-keyed dict. Has two backends:
  `discover_channels_native()` (pure Python, preferred) and
  `discover_channels_via_control()` (shells out to the `control`
  utility from ka9q-radio).

Both are used by the CLI (`ka9q list`) and the TUI pickers.

---

## CLI & TUI

- [`cli.py`](../ka9q/cli.py) — `ka9q` console script. Subcommands
  `list`, `query`, `set`, `tui`. Every `set` verb maps to a
  `RadiodControl` setter via the `SET_VERBS` table. Registered as
  an entry point in `pyproject.toml`.
- [`tui.py`](../ka9q/tui.py) — Textual application. Panels for
  tuning, frontend/GPSDO, signal, filter, demod, input, output,
  options. Interactive pickers (`RadiodPickerScreen`,
  `SsrcPickerScreen`) use the same discovery functions the CLI uses.
  Optional dependency (`pip install ka9q-python[tui]`).

---

## Monitor and Calibrator

- [`monitor.py`](../ka9q/monitor.py) — `ChannelMonitor` watches
  status packets to detect radiod restarts and fire user callbacks.
- [`pps_calibrator.py`](../ka9q/pps_calibrator.py) — L6 BPSK PPS
  chain-delay calibration. Classes: `BpskPpsCalibrator`,
  `PpsCalibrationResult`, `NotchFilter500Hz`. Specialized for
  WB6CXC-style injector-based cable-delay measurement.

---

## Protocol

### TLV wire format

```
[Type: 1 byte][Length: 1–2 bytes][Value: variable]
```

- **Type**: `StatusType` enum value.
- **Length**: single byte if <128; two bytes (`0x80|hi`, `lo`) if
  larger. Length 0 is a valid "zero value" encoding (compressed).
- **Value**:
  - Integers: big-endian, leading zeros stripped.
  - Floats: IEEE 754 big-endian (4 or 8 bytes).
  - Strings: UTF-8 length-prefixed.

Every packet ends with `StatusType.EOL = 0`.

### Packet framing

- Command packet: `[CMD=1][params…][EOL]`
- Status packet: `[STATUS=0][params…][EOL]`

Status packets are multicast by radiod periodically (every ~1–2 s)
and in response to commands (matched by `COMMAND_TAG`).

### Addressing

- Transport: UDP multicast, standard radiod control/status port
  `5006`. RTP audio lives on separate per-group addresses.
- mDNS resolution via `avahi-resolve` (Linux), `dns-sd` (macOS), or
  `getaddrinfo()` fallback. See [`utils.py`](../ka9q/utils.py).
- Deterministic SSRCs: `allocate_ssrc()` hashes channel parameters
  so identical requests converge to the same SSRC across restarts.

---

## Threading Model

### Locks

`RadiodControl` serializes network I/O with reentrant locks
(`threading.RLock`):

```
_socket_lock       — protects control socket, send operations
_status_sock_lock  — protects status socket, tune() read path
```

`RLock` allows a thread holding the lock to re-enter; prevents
deadlock in nested calls (e.g. a setter that internally calls
`poll_status()`).

### Concurrent use

```python
with RadiodControl("radiod.local") as control:
    def worker(freq):
        control.set_frequency(ssrc=10000, frequency_hz=freq)
    threads = [Thread(target=worker, args=(f,)) for f in frequencies]
    for t in threads: t.start()
    for t in threads: t.join()
```

Safe: each `set_frequency` acquires `_socket_lock`, sends its TLV
packet atomically, releases.

### Stream threads

- `ManagedStream`: one health thread, one RTP receive thread.
- `MultiStream`: one health thread, one RTP receive thread total
  (shared across all slots).
- `RadiodStream`: one RTP receive thread.
- `SpectrumStream`: one status-channel receive thread, one poll thread.

All are daemon threads; `stop()` joins with a 5 s timeout.

---

## Error Handling

### Exception hierarchy

```
Exception
└── Ka9qError
    ├── ConnectionError
    ├── CommandError
    ├── ValidationError
    └── DiscoveryError
```

### Philosophy

- **Validate early**: every public `RadiodControl` method checks
  inputs (SSRC range, frequency sign, preset whitelist) before
  touching the network.
- **Fail fast** with a typed exception, not a generic one.
- **Preserve context**: `raise CommandError(...) from e` — the
  original traceback is kept.
- **Retry transient network errors**: `send_command()` retries up
  to `max_retries` times with exponential backoff (0.1, 0.2, 0.4 s).

---

## Network Operations

### Control socket (send)

```python
sock = socket.socket(AF_INET, SOCK_DGRAM)
sock.setsockopt(SOL_SOCKET, SO_REUSEADDR, 1)
sock.setsockopt(IPPROTO_IP, IP_MULTICAST_IF, inet_aton('0.0.0.0'))
sock.setsockopt(IPPROTO_IP, IP_ADD_MEMBERSHIP, mreq)
sock.setsockopt(IPPROTO_IP, IP_MULTICAST_LOOP, 1)
sock.setsockopt(IPPROTO_IP, IP_MULTICAST_TTL, 2)
```

### Status socket (receive)

```python
sock.bind(('0.0.0.0', 5006))   # must bind to the multicast port
sock.setsockopt(IPPROTO_IP, IP_ADD_MEMBERSHIP, mreq)
sock.settimeout(0.1)
```

### Multi-homed hosts

`interface=` on `RadiodControl` and the discovery functions maps to
`IP_ADD_MEMBERSHIP`'s interface field. On single-homed hosts,
`0.0.0.0` (INADDR_ANY) is fine.

---

## Resource Management

### Context manager

```python
with RadiodControl("radiod.local") as control:
    ...
# sockets closed, even on exception
```

### Robust `close()`

`close()` is idempotent: safe to call multiple times, handles
per-socket cleanup errors, logs warnings rather than raising, sets
socket attributes to `None` in `finally` blocks.

### Stream lifecycle

Every streaming class (`RadiodStream`, `ManagedStream`, `MultiStream`,
`SpectrumStream`, `RTPRecorder`) follows the same shape:

```
__init__ → start() → (threads run) → stop() → (threads join, sockets closed)
```

`stop()` is idempotent and joins with a 5 s timeout.

---

## Debugging

Enable hex-dump logging of all TLV traffic:

```python
import logging
logging.basicConfig(level=logging.DEBUG)
```

Common issues:

- **"Failed to resolve address"** — `avahi-resolve -n host.local`
  to verify mDNS; try the IP directly.
- **"Permission denied" on multicast** — firewall; verify UDP/5006.
- **No status packets received** — `interface=` may be needed on
  multi-homed hosts; see [GETTING_STARTED.md](GETTING_STARTED.md).

---

*API reference: [API_REFERENCE.md](API_REFERENCE.md).*
