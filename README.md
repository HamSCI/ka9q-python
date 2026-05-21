# ka9q-python

[![PyPI version](https://badge.fury.io/py/ka9q-python.svg)](https://badge.fury.io/py/ka9q-python)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

**General-purpose Python library for controlling [ka9q-radio](https://github.com/ka9q/ka9q-radio)**

Control radiod channels for any application: AM/FM/SSB radio, WSPR monitoring, SuperDARN radar, CODAR oceanography, HF fax, satellite downlinks, and more.

**Note:** Package name is `ka9q-python` out of respect for KA9Q (Phil Karn's callsign). Import as `import ka9q`.

## Table of Contents

- [Features](#features)
- [Installation](#installation)
- [Getting Started](docs/GETTING_STARTED.md)
- [Quick Start](#quick-start)
- [ka9q-radio Compatibility](#ka9q-radio-compatibility)
- [Documentation](#documentation)
- [Examples](#examples)
- [Use Cases](#use-cases)
- [License](#license)

## Features

- **Complete radiod API** — all 117 TLV status/command parameters exposed, generated from ka9q-radio's C headers
- **Every radiod RTP encoding decoded** — `S16LE/BE`, `F32LE/BE`, `F16LE/BE`, `MULAW`, `ALAW` via pure-NumPy `parse_rtp_samples()`; `OPUS` / `OPUS_VOIP` via the optional `OpusDecoder` (install with `[opus]` extra)
- **Four stream abstractions** — `RTPRecorder` (raw packets), `RadiodStream` (samples + gap handling), `ManagedStream` (self-healing single channel), `MultiStream` (shared socket, many SSRCs)
- **Typed status decoder** — `ChannelStatus`, `FrontendStatus`, `PllStatus`, etc. with dotted-path field access
- **Precise RTP timing** — GPS_TIME / RTP_TIMESNAP for sample-accurate wallclock timestamps
- **LAN discovery** — enumerate radiod instances and their active channels via mDNS
- **CLI + TUI** — `ka9q list / query / set / tui` for interactive and scripted control
- **Multi-homed** — explicit interface selection for hosts with multiple NICs
- **Protocol drift detection** — pinned to a specific ka9q-radio commit, with a sync script
- **Pure Python** — NumPy is the only runtime dependency

## Installation

```bash
pip install ka9q-python
```

Optional extras:

| Extra | Adds | Needed for |
|-------|------|------------|
| `tui` | `textual` | `ka9q tui` interactive terminal UI |
| `opus` | `opuslib` | decoding `OPUS` / `OPUS_VOIP` RTP payloads via `OpusDecoder` |
| `dev`  | `pytest`, `pytest-cov` | running the test suite |

```bash
pip install "ka9q-python[opus]"          # one extra
pip install "ka9q-python[tui,opus]"      # multiple
```

Or install from source:

```bash
git clone https://github.com/mijahauan/ka9q-python.git
cd ka9q-python
pip install -e .
```

## Quick Start

> **Host selection:** All examples reference `bee1-hf-status.local`, which is the default integration test radiod in this repo. Replace it with your own radiod host or set `RADIOD_HOST`, `RADIOD_ADDRESS`, or the `--radiod-host` pytest option when running in other environments.

### Listen to AM Broadcast

```python
from ka9q import RadiodControl

# Connect to radiod (default test host: bee1-hf-status.local)
control = RadiodControl("bee1-hf-status.local")

# Create AM channel on 10 MHz WWV
control.create_channel(
    ssrc=10000000,
    frequency_hz=10.0e6,
    preset="am",
    sample_rate=12000
)

# RTP stream now available with SSRC 10000000
```

### Request Specific Output Encoding

```python
from ka9q import RadiodControl, Encoding

control = RadiodControl("bee1-hf-status.local")

# Create a channel with 32-bit float output (highest quality)
control.ensure_channel(
    frequency_hz=14.074e6,
    preset="usb",
    sample_rate=12000,
    encoding=Encoding.F32
)
```

### Monitor WSPR Bands

```python
from ka9q import RadiodControl

control = RadiodControl("bee1-hf-status.local")

wspr_bands = [
    (1.8366e6, "160m"),
    (3.5686e6, "80m"),
    (7.0386e6, "40m"),
    (10.1387e6, "30m"),
    (14.0956e6, "20m"),
]

for freq, band in wspr_bands:
    control.create_channel(
        ssrc=int(freq),
        frequency_hz=freq,
        preset="usb",
        sample_rate=12000
    )
    print(f"{band} WSPR channel created")
```

### Discover Existing Channels

```python
from ka9q import discover_channels

channels = discover_channels("bee1-hf-status.local")
for ssrc, info in channels.items():
    print(f"{ssrc}: {info.frequency/1e6:.3f} MHz, {info.preset}, {info.sample_rate} Hz")
```

### Record RTP Stream with Precise Timing

```python
from ka9q import discover_channels, RTPRecorder
import time

# Get channel with timing info
channels = discover_channels("bee1-hf-status.local")
channel = channels[14074000]

# Define packet handler
def handle_packet(header, payload, wallclock):
    print(f"Packet at {wallclock}: {len(payload)} bytes")

# Create and start recorder
recorder = RTPRecorder(channel=channel, on_packet=handle_packet)
recorder.start()
recorder.start_recording()
time.sleep(60)  # Record for 60 seconds
recorder.stop_recording()
recorder.stop()
```

### Multi-Homed Systems

For systems with multiple network interfaces, specify which interface to use:

```python
from ka9q import RadiodControl, discover_channels

# Specify your interface IP address
my_interface = "192.168.1.100"

# Create control with specific interface
control = RadiodControl("bee1-hf-status.local", interface=my_interface)

# Discovery on specific interface
channels = discover_channels("bee1-hf-status.local", interface=my_interface)
```

### Automatic Channel Recovery

ensure your channels survive radiod restarts:

```python
from ka9q import RadiodControl, ChannelMonitor

control = RadiodControl("bee1-hf-status.local")
monitor = ChannelMonitor(control)
monitor.start()

# This channel will be automatically re-created if it disappears
monitor.monitor_channel(
    frequency_hz=14.074e6,
    preset="usb",
    sample_rate=12000
)
```

### Channel Cleanup (frequency = 0)

`radiod` removes channels by polling for streams whose frequency is set to `0 Hz`. Always call `remove_channel(ssrc)` (or explicitly set `set_frequency(ssrc, 0.0)` if you build TLVs yourself) when tearing down a stream so the background poller can reclaim it:

```python
with RadiodControl("bee1-hf-status.local") as control:
    info = control.ensure_channel(
        frequency_hz=10e6,
        preset="iq",
        sample_rate=16000
    )

    # ... use channel ...

    control.remove_channel(info.ssrc)  # marks frequency=0
```

> Note: `remove_channel()` finishes instantly on the client; radiod’s poller typically purges the channel within the next second.

## ka9q-radio Compatibility

`ka9q-python` tracks a specific git commit of [ka9q-radio](https://github.com/ka9q/ka9q-radio) to ensure its protocol definitions (`StatusType`, `Encoding`) match the C headers exactly. This prevents subtle bugs from protocol drift between the two projects.

### How It Works

| File | Role |
|------|------|
| `ka9q_radio_compat` | Plain-text pin recording the validated ka9q-radio commit hash |
| `ka9q/compat.py` | Importable `KA9Q_RADIO_COMMIT` constant for deployment tooling |
| `ka9q/types.py` | Auto-generated from ka9q-radio's `status.h` and `rtp.h` |
| `scripts/sync_types.py` | The tool that parses C headers and regenerates `types.py` |
| `tests/test_protocol_compat.py` | Drift test (runs automatically if `../ka9q-radio` exists) |

### Checking for Drift

If you have the ka9q-radio source tree at `../ka9q-radio`:

```bash
python scripts/sync_types.py --check    # CI mode: exits non-zero on drift
python scripts/sync_types.py --diff     # Preview changes without modifying anything
```

### Syncing After ka9q-radio Updates

```bash
python scripts/sync_types.py --apply    # Regenerates types.py, updates pins
git diff ka9q/types.py                  # Review the changes
python -m pytest tests/                 # Verify nothing broke
```

The `--apply` mode updates three files atomically:
1. `ka9q/types.py` — regenerated from the C headers
2. `ka9q_radio_compat` — updated with the new commit hash
3. `ka9q/compat.py` — updated with the new commit hash (importable)

### For Deployment Tooling

`ka9q-update` (or any deployment tool) can read the pinned commit to ensure the correct `radiod` version is running:

```python
from ka9q.compat import KA9Q_RADIO_COMMIT

print(f"This ka9q-python requires ka9q-radio at {KA9Q_RADIO_COMMIT[:12]}")
```

### Running the Drift Test

The pytest drift test runs automatically as part of the test suite:

```bash
python -m pytest tests/test_protocol_compat.py -v
```

It auto-skips if `../ka9q-radio` is not present, so CI environments without the C source tree are unaffected.

## Documentation

- **[Getting Started](docs/GETTING_STARTED.md)** — first-run walkthrough
- **[Recipes](docs/RECIPES.md)** — task-oriented cookbook: LAN probing, fixed-channel pipelines (WSPR/PSK/FT8/timing), nimble SWL-style retuning, and using ka9q-python across different SDRs
- **[API Reference](docs/API_REFERENCE.md)** — every public class, method, and function
- **[Architecture](docs/ARCHITECTURE.md)** — module layout, threading, protocol
- **[CLI Guide](docs/CLI_GUIDE.md)** — `ka9q list / query / set / tui` command reference
- **[TUI Guide](docs/TUI_GUIDE.md)** — the Textual terminal UI
- **[MultiStream Guide](docs/MULTI_STREAM.md)** — shared-socket multi-channel receiver
- **[RTP Timing](docs/RTP_TIMING_SUPPORT.md)** — GPS_TIME / RTP_TIMESNAP for sample-accurate timestamps
- **[Installation](docs/INSTALLATION.md)** · **[Testing](docs/TESTING_GUIDE.md)** · **[Security](docs/SECURITY.md)**
- **[Changelog](CHANGELOG.md)**

## Examples

See [`examples/`](examples/) for runnable scripts:

- [`simple_am_radio.py`](examples/simple_am_radio.py) — minimal AM listener
- [`discover_example.py`](examples/discover_example.py) — channel discovery on the LAN
- [`stream_example.py`](examples/stream_example.py) — `RadiodStream` sample callback
- [`rtp_recorder_example.py`](examples/rtp_recorder_example.py) — precise-timing RTP recorder
- [`multi_stream_smoke.py`](examples/multi_stream_smoke.py) — `MultiStream` multi-SSRC receiver
- [`hf_band_scanner.py`](examples/hf_band_scanner.py) — dynamic band scanner
- [`channel_cleanup_example.py`](examples/channel_cleanup_example.py) — teardown via frequency=0
- [`tune.py`](examples/tune.py) — interactive tuning utility
- [`superdarn_recorder.py`](examples/superdarn_recorder.py), [`codar_oceanography.py`](examples/codar_oceanography.py), [`grape_integration_example.py`](examples/grape_integration_example.py) — domain-specific recorders

## Use Cases

See [docs/RECIPES.md](docs/RECIPES.md) for worked examples of:

- **LAN probing** — enumerate radiod instances and their active channels
- **Fixed-channel pipelines** — WSPR, PSK/FT8, HF timing (bundled band plans, `MultiStream`); see companion projects `wspr-recorder`, `psk-recorder`, `hf-timestd`
- **Nimble channel switching** — single-channel SWL-style retuning driven from the CLI or an app
- **SDR portability** — ka9q-python talks to `radiod`, which talks to the SDR; reporting frontend capabilities via `FrontendStatus`. Primary tested frontend is the RX888; AirspyR2 and Airspy HF+ support is in development.

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
