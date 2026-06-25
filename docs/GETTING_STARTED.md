# Getting Started with ka9q-python

Welcome to **ka9q-python**, a general-purpose Python library for controlling [ka9q-radio](https://github.com/ka9q/ka9q-radio). This guide will help you get up and running quickly, whether you're listening to AM radio, monitoring WSPR propagation, or building a custom SDR application.

---

## Prerequisites

Before you begin, ensure you have the following:

1. **Python 3.9 or later** installed on your system.
2. **A running instance of `ka9q-radio`** (specifically, the `radiod` daemon) accessible on your network.
3. **Basic knowledge of Python programming** (variables, functions, imports).

---

## Installation

Install `ka9q-python` using pip:

```bash
pip install ka9q-python
```

Or, if you want to install from source:

```bash
git clone https://github.com/HamSCI/ka9q-python.git
cd ka9q-python
pip install -e .
```

---

## Your First Program: Listening to AM Radio

Let's start with the simplest possible example: creating an AM radio channel to listen to the WWV time signal on 10 MHz.

### Step 1: Import the Library

```python
from ka9q import RadiodControl
```

The `RadiodControl` class is the primary interface for sending commands to `radiod`.

### Step 2: Connect to radiod

```python
control = RadiodControl("radiod.local")
```

Replace `"radiod.local"` with the mDNS name or IP address of your `radiod` instance. If you're on a multi-homed system (multiple network interfaces), you may need to specify which interface to use:

```python
control = RadiodControl("radiod.local", interface="192.168.1.100")
```

### Step 3: Create a Channel

```python
control.create_channel(
    ssrc=10000000,          # Channel identifier (we use the frequency in Hz)
    frequency_hz=10.0e6,    # 10 MHz
    preset="am",            # AM demodulation
    sample_rate=12000,      # 12 kHz audio output
    agc_enable=1,           # Enable automatic gain control
    gain=0.0                # Manual gain (not used when AGC is on)
)
```

This command tells `radiod` to create a new channel tuned to 10 MHz, demodulating in AM mode, with a 12 kHz audio output.

### Step 4: Confirm the Channel is Active

```python
print("✓ AM channel created on 10 MHz")
print("  SSRC: 10000000")
print("  RTP stream is now available on radiod's multicast address")
```

At this point, `radiod` is streaming the audio as RTP packets. You can use `radiod`'s built-in audio output, or you can consume the RTP stream programmatically (see the next section).

### Complete Example

Here's the full program:

```python
from ka9q import RadiodControl

# Connect to radiod
control = RadiodControl("radiod.local")

# Create AM channel for WWV 10 MHz
control.create_channel(
    ssrc=10000000,
    frequency_hz=10.0e6,
    preset="am",
    sample_rate=12000,
    agc_enable=1,
    gain=0.0
)

print("✓ AM channel created on 10 MHz")
print("  SSRC: 10000000")
print("  RTP stream is now available")
```

Save this as `my_first_radio.py` and run it:

```bash
python my_first_radio.py
```

---

## Understanding the Core Concepts

Now that you've created your first channel, let's understand the key concepts in `ka9q-python`.

### 1. RadiodControl

`RadiodControl` is the low-level interface for sending commands to `radiod`. It gives you fine-grained control over channel parameters like frequency, preset (demodulation mode), sample rate, gain, and more.

**Use `RadiodControl` when you need:**
- Direct control over channel creation and configuration.
- To send multiple commands to the same channel over time (e.g., tuning, adjusting gain).

### 2. ChannelInfo

A `ChannelInfo` object contains all the information about a channel: its SSRC, frequency, preset, sample rate, multicast address, and port. You typically get `ChannelInfo` objects by discovering existing channels.

### 3. Discovering Channels

You can discover what channels are currently active on `radiod`:

```python
from ka9q import discover_channels

channels = discover_channels("radiod.local")
for ssrc, info in channels.items():
    print(f"SSRC {ssrc}: {info.frequency/1e6:.3f} MHz, {info.preset}, {info.sample_rate} Hz")
```

This is useful for monitoring what's already running, or for connecting to a channel that was created by another application.

### 4. Stream Abstraction Layers

`ka9q-python` offers four ways to consume RTP streams. Pick the one that matches your use case:

| Layer | Class | Use Case |
| :--- | :--- | :--- |
| **Low-Level** | `RTPRecorder` | Direct access to RTP packets with precise GPS timing. For recorders and timing-critical work. |
| **Mid-Level** | `RadiodStream` | Continuous sample delivery with automatic gap filling and quality metrics. |
| **High-Level, one channel** | `ManagedStream` | Self-healing single-channel stream. Recovers from `radiod` restarts. Best for SWL-style apps. |
| **High-Level, many channels** | `MultiStream` | Shared-socket receiver for many SSRCs on the same multicast group. Best for fixed-channel pipelines (WSPR, PSK, FT8, timing). See [MULTI_STREAM.md](MULTI_STREAM.md). |

**Start with `ManagedStream` for one channel; switch to `MultiStream` once you need ~5+ channels on the same group.**

### 5. The `ka9q` CLI and TUI

For interactive work, the package ships a command-line tool:

```bash
ka9q list  radiod.local                     # enumerate active channels
ka9q query radiod.local --ssrc 14074000     # dump a channel's status
ka9q set   radiod.local --ssrc 14074000 frequency 14095600
ka9q tui   radiod.local                     # Textual terminal UI
```

See [CLI_GUIDE.md](CLI_GUIDE.md) and [TUI_GUIDE.md](TUI_GUIDE.md).

---

## Next Steps: Using ManagedStream

Let's enhance our example to actually receive and process the audio samples.

```python
from ka9q import RadiodControl, ManagedStream
import numpy as np

def on_samples(samples: np.ndarray, quality):
    """Called when new audio samples are available."""
    print(f"Received {len(samples)} samples, quality: {quality.completeness_pct:.2f}%")
    # Process samples here (e.g., save to file, analyze, play audio)

def on_stream_dropped(reason: str):
    """Called when the stream drops (e.g., radiod restart)."""
    print(f"⚠️  Stream dropped: {reason}")

def on_stream_restored(channel):
    """Called when the stream is restored."""
    print(f"✓ Stream restored: {channel.frequency/1e6:.3f} MHz")

# Connect to radiod
control = RadiodControl("radiod.local")

# Create a self-healing stream
stream = ManagedStream(
    control=control,
    frequency_hz=10.0e6,
    preset="am",
    sample_rate=12000,
    agc_enable=1,
    on_samples=on_samples,
    on_stream_dropped=on_stream_dropped,
    on_stream_restored=on_stream_restored,
)

# Start streaming
stream.start()

# Let it run for 60 seconds
import time
time.sleep(60)

# Stop streaming
stream.stop()
```

This example creates a channel and immediately starts receiving samples. The `on_samples` callback is invoked continuously with batches of audio data. If `radiod` restarts, the stream will automatically recover.

---

## Where to Go Next

Congratulations! You've created your first `ka9q-python` application and learned the core concepts.

**To continue your journey:**

1. **Explore the Examples**: The `examples/` directory contains many complete applications demonstrating different features. Start with `simple_am_radio.py` and `stream_example.py`.

2. **Read the API Reference**: The `docs/API_REFERENCE.md` file provides detailed documentation for all classes and methods.

3. **Learn About Advanced Features**: Check out `examples/advanced_features_demo.py` to see how to use Doppler tracking, PLL configuration, squelch, and more.

4. **Join the Community**: If you have questions or want to contribute, visit the [GitHub repository](https://github.com/HamSCI/ka9q-python).

---

## Common Issues and Troubleshooting

**Problem**: `ConnectionError: Unable to connect to radiod`  
**Solution**: Ensure `radiod` is running and accessible on your network. Check the status address (mDNS name or IP) is correct.

**Problem**: `No channels found` when using `discover_channels`  
**Solution**: Ensure at least one channel is active on `radiod`. Try creating a channel first, then discovering it.

**Problem**: `Interface not specified` error on multi-homed systems  
**Solution**: Specify the `interface` parameter when creating `RadiodControl`: `RadiodControl("radiod.local", interface="192.168.1.100")`.

---

## Summary

You've learned how to:
- Install `ka9q-python`.
- Connect to `radiod` and create a channel.
- Discover existing channels.
- Receive and process audio samples using `ManagedStream`.

With these fundamentals, you're ready to build powerful SDR applications. Happy experimenting!
