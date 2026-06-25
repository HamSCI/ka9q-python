# ka9q-python Examples

This directory contains a comprehensive collection of examples demonstrating the capabilities of `ka9q-python`. The examples are organized by complexity and use case to help you learn progressively.

---

## Getting Started

If you're new to `ka9q-python`, we recommend following this learning path:

1. **Read the [Getting Started Guide](../docs/GETTING_STARTED.md)** first to understand the basics.
2. **Start with the Basic Examples** below to see simple, working code.
3. **Move to Intermediate Examples** once you're comfortable with the fundamentals.
4. **Explore Advanced Examples** when you need specialized features.

---

## Basic Examples

These examples demonstrate fundamental operations with minimal code. Perfect for beginners.

### `simple_am_radio.py`

**What it does**: Creates a simple AM radio channel tuned to WWV 10 MHz.

**Concepts demonstrated**:
- Connecting to `radiod`
- Creating a channel with `create_channel()`
- Basic channel configuration

**Run it**:
```bash
python3 examples/simple_am_radio.py
```

**Expected output**: Confirmation that an AM channel has been created. The RTP stream will be available on `radiod`'s multicast address.

---

### `discover_example.py`

**What it does**: Discovers all active channels on `radiod` and displays their information.

**Concepts demonstrated**:
- Using `discover_channels()` to find existing channels
- Iterating over discovered channels
- Displaying channel metadata (frequency, preset, sample rate, etc.)

**Run it**:
```bash
python3 examples/discover_example.py
```

**Expected output**: A list of all active channels with their SSRCs, frequencies, presets, and other details.

---

### `channel_cleanup_example.py`

**What it does**: Demonstrates proper channel cleanup by setting frequency to 0 Hz.

**Concepts demonstrated**:
- Creating a channel
- Removing a channel with `remove_channel()`
- Understanding the channel cleanup mechanism

**Run it**:
```bash
python3 examples/channel_cleanup_example.py
```

**Expected output**: Creates a channel, uses it briefly, then removes it cleanly.

---

## Intermediate Examples

These examples show more sophisticated usage patterns and introduce the stream APIs.

### `stream_example.py`

**What it does**: Receives continuous audio samples from a channel using `RadiodStream`.

**Concepts demonstrated**:
- Using `RadiodStream` for sample delivery
- Handling quality metrics (`StreamQuality`)
- Gap detection and reporting
- Resequencing out-of-order packets

**Run it**:
```bash
# First discover available channels
python3 examples/discover_example.py

# Then stream from a specific SSRC
python3 examples/stream_example.py --ssrc 10000000 --duration 30 --discover
```

**Expected output**: Real-time stream statistics including samples delivered, completeness percentage, and RTP packet metrics.

---

### `rtp_recorder_example.py`

**What it does**: Records RTP packets with precise timing information.

**Concepts demonstrated**:
- Using `RTPRecorder` for low-level packet access
- Handling RTP headers and timing
- State machine for recording control
- Recording metrics and statistics

**Run it**:
```bash
python3 examples/rtp_recorder_example.py radiod.local --duration 30
```

**Expected output**: Packet-by-packet information with timestamps, sequence numbers, and final recording statistics.

---

### `tune_example.py`

**What it does**: Demonstrates programmatic channel tuning (changing frequency).

**Concepts demonstrated**:
- Using `tune()` method for frequency changes
- Waiting for channel verification
- Dynamic frequency control

**Run it**:
```bash
python3 examples/tune_example.py
```

**Expected output**: Creates a channel and tunes it to different frequencies, showing the tuning process.

---

### `tune.py`

**What it does**: Interactive command-line utility for tuning channels (Python implementation of ka9q-radio's `tune`).

**Concepts demonstrated**:
- Interactive channel control
- Real-time frequency adjustment
- Command-line argument parsing

**Run it**:
```bash
python3 examples/tune.py --help
```

---

## Advanced Examples

These examples demonstrate specialized features for specific applications and advanced use cases.

### `advanced_features_demo.py`

**What it does**: Showcases all advanced `radiod` features exposed by `ka9q-python`.

**Concepts demonstrated**:
- Doppler tracking for satellites
- PLL configuration for carrier tracking
- SNR-based squelch
- Independent Sideband (ISB) mode
- Secondary filter configuration
- Opus encoding
- Spectrum analyzer mode
- RF hardware controls
- And many more advanced features

**Run it**:
```bash
python3 examples/advanced_features_demo.py
```

**Note**: Many features are commented out to prevent unintended changes. Uncomment and adjust as needed for your specific hardware and use case.

---

### `superdarn_recorder.py`

**What it does**: Records ionospheric radar signals from SuperDARN stations.

**Concepts demonstrated**:
- Application-specific channel configuration
- Long-running recording sessions
- Scientific data collection

**Use case**: Ionospheric research, space weather monitoring.

---

### `codar_oceanography.py`

**What it does**: Monitors ocean current radar (CODAR) signals.

**Concepts demonstrated**:
- Oceanographic radar signal reception
- Multi-frequency monitoring

**Use case**: Ocean current mapping, coastal monitoring.

---

### `hf_band_scanner.py`

**What it does**: Dynamically scans HF bands and creates channels for active frequencies.

**Concepts demonstrated**:
- Dynamic channel creation
- Frequency scanning
- Multi-channel management

**Use case**: Band monitoring, signal hunting, propagation studies.

---

### `grape_integration_example.py`

**What it does**: Integrates with the HamSCI GRAPE (Grape Radio Amateur Propagation Experiment) project.

**Concepts demonstrated**:
- Integration with external data collection systems
- Precise timing for scientific applications
- Data formatting and upload

**Use case**: Citizen science, propagation research.

---

### `test_timing_fields.py`

**What it does**: Verifies GPS_TIME and RTP_TIMESNAP timing fields in RTP packets.

**Concepts demonstrated**:
- Precise timing extraction
- GPS time synchronization
- Timing validation

**Use case**: Applications requiring microsecond-level timing accuracy.

---

### `spectrum_example.py`

**What it does**: Receives real-time FFT spectrum data from radiod and prints bin statistics.

**Concepts demonstrated**:
- Using `SpectrumStream` for spectrum data
- Accessing `bin_power_db` for dB-scaled FFT bins
- Spectrum channel creation and polling
- Frequency axis reconstruction from bin metadata

**Run it**:
```bash
python3 examples/spectrum_example.py bee1-hf-status.local --freq 14.1e6
```

**Expected output**: Per-frame spectrum statistics (bin count, peak power, noise floor) updated ~10 times per second.

---

## Diagnostics

The `diagnostics/` subdirectory contains utilities for troubleshooting and testing.

---

## Tips for Using Examples

1. **Replace the radiod address**: Most examples use `"bee1-hf-status.local"` as the default. Replace this with your own `radiod` instance address.

2. **Check for active channels**: Before running stream examples, ensure at least one channel is active on `radiod`. Use `discover_example.py` to check.

3. **Multi-homed systems**: If you have multiple network interfaces, you may need to specify the `interface` parameter when creating `RadiodControl`.

4. **Read the code**: Each example is heavily commented. Reading the source code is the best way to understand how things work.

5. **Experiment**: Modify the examples to suit your needs. Change frequencies, presets, sample rates, and other parameters to see how they affect behavior.

---

## Need Help?

- **Getting Started Guide**: [docs/GETTING_STARTED.md](../docs/GETTING_STARTED.md)
- **API Reference**: [docs/API_REFERENCE.md](../docs/API_REFERENCE.md)
- **GitHub Issues**: [https://github.com/HamSCI/ka9q-python/issues](https://github.com/HamSCI/ka9q-python/issues)

---

## Contributing Examples

Have an interesting use case or application? We welcome contributions! Please submit a pull request with:
- Your example code (well-commented)
- A brief description of what it does
- Any special requirements or dependencies

Happy experimenting!
