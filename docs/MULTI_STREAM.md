# MultiStream Guide

[`MultiStream`](../ka9q/multi_stream.py) is a shared-socket,
multi-SSRC receiver: one UDP socket, one receive thread, N per-channel
callbacks demultiplexed by SSRC. Use it when you want to listen to
five or more channels that share a multicast group.

---

## Motivation

A `ManagedStream` opens its own UDP socket and joins the multicast
group independently. That is fine for one or two channels. But every
socket joined to the same group causes the kernel to copy every packet
into every socket's receive buffer. Ten `ManagedStream`s on one
group = 10× packet duplication in kernel memory, 10× wakeups, 10×
context switches.

`MultiStream` opens **one** socket, runs **one** receive thread, and
dispatches packets to per-channel state by SSRC in user space. The
per-channel interface (sample callback, quality metadata, drop/restore
callbacks) is identical to `ManagedStream`.

---

## When to prefer MultiStream

Pick `MultiStream` when:

- Five or more channels share the same multicast address and port.
- You are on a CPU- or memory-constrained host (RPi-class).
- You care about predictable per-packet latency across many channels.

Keep `ManagedStream` when:

- You only have one or two channels.
- Channels live on different multicast groups (can't be combined).
- You specifically want each channel's receive path isolated.

Production users:
[psk-recorder](https://github.com/mijahauan/psk-recorder) runs 20
channels (10 FT4 + 10 FT8) on bee3 through a single `MultiStream`.
[wspr-recorder](https://github.com/mijahauan/wspr-recorder) and
[hf-timestd](https://github.com/mijahauan/hf-timestd) use the same
pattern.

---

## API

### Construction

```python
from ka9q import RadiodControl, MultiStream

control = RadiodControl("bee3-status.local")
multi = MultiStream(
    control=control,
    drop_timeout_sec=15.0,          # silence → declare channel dropped
    restore_interval_sec=5.0,       # retry cadence for dropped channels
    deliver_interval_packets=10,    # batch size for on_samples
    samples_per_packet=320,
    resequence_buffer_size=64,
)
```

The defaults are tuned for `ManagedStream`-compatible behavior at
standard RTP sample rates.

### Adding channels

```python
info = multi.add_channel(
    frequency_hz=14.074e6,
    preset="usb",
    sample_rate=12000,
    encoding=2,                    # 2 = S16BE
    agc_enable=0,
    gain=0.0,
    on_samples=my_callback,
    on_stream_dropped=lambda reason: log.warn(reason),
    on_stream_restored=lambda ch: log.info(f"restored SSRC={ch.ssrc}"),
)
```

`add_channel()`:

1. Calls `control.ensure_channel()` — deterministic SSRC, reuses an
   existing matching channel, or creates a new one.
2. Verifies the returned `ChannelInfo.multicast_address` / `port`
   matches the group the `MultiStream` is already bound to. The first
   `add_channel()` sets the group; subsequent calls must match or
   `ValueError` is raised.
3. Registers a per-SSRC slot holding a `PacketResequencer` and a
   `StreamQuality` block.

Must be called **before** `start()`. Adding channels after `start()`
is not supported in the current implementation.

### Starting and stopping

```python
multi.start()    # opens socket, spawns receive + health threads
# ...
multi.stop()     # stops threads, flushes resequencers, closes socket
```

### Per-channel callback

`on_samples(samples: np.ndarray, quality: StreamQuality)` — same
signature as `ManagedStream` / `RadiodStream`. Called roughly every
`deliver_interval_packets` packets. `quality.batch_gaps` lists any
gap events detected by the resequencer since the last delivery;
`quality.total_samples_delivered` and `quality.sample_rate` are
populated for book-keeping.

Exceptions in the callback are caught and logged — they do not kill
the receive thread.

---

## Example: 2-channel smoke test

Adapted from [`examples/multi_stream_smoke.py`](../examples/multi_stream_smoke.py):

```python
import time
from collections import defaultdict
import numpy as np
from ka9q import MultiStream, RadiodControl, StreamQuality

freqs = {
    "FT8-20m":  14.074e6,
    "WSPR-20m": 14.0956e6,
}
stats = defaultdict(lambda: {"callbacks": 0, "samples": 0, "gaps": 0})

def make_cb(label):
    def cb(samples: np.ndarray, q: StreamQuality):
        s = stats[label]
        s["callbacks"] += 1
        s["samples"] += len(samples)
        s["gaps"] += len(q.batch_gaps)
    return cb

with RadiodControl("bee3-status.local") as control:
    multi = MultiStream(control=control)
    for label, fhz in freqs.items():
        multi.add_channel(
            frequency_hz=fhz,
            preset="usb",
            sample_rate=12000,
            encoding=2,
            on_samples=make_cb(label),
        )
    multi.start()
    time.sleep(20.0)
    multi.stop()

for label, s in stats.items():
    print(f"{label}: cbs={s['callbacks']} samples={s['samples']} gaps={s['gaps']}")
```

Run the full version:

```
python examples/multi_stream_smoke.py --host bee3-status.local --duration 20
```

---

## Example: 10-channel FT8 band scanner

```python
import time, logging
from ka9q import RadiodControl, MultiStream, StreamQuality
import numpy as np

logging.basicConfig(level=logging.INFO)

FT8 = [
    ("160m",  1.840e6),
    ( "80m",  3.573e6),
    ( "60m",  5.357e6),
    ( "40m",  7.074e6),
    ( "30m", 10.136e6),
    ( "20m", 14.074e6),
    ( "17m", 18.100e6),
    ( "15m", 21.074e6),
    ( "12m", 24.915e6),
    ( "10m", 28.074e6),
]

def make_sink(label):
    def on_samples(samples: np.ndarray, q: StreamQuality):
        rms = float(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))
        print(f"{label:5s}  n={len(samples):5d}  rms={rms:.4f}  "
              f"gaps={len(q.batch_gaps)}")
    return on_samples

with RadiodControl("bee3-status.local") as control:
    multi = MultiStream(control=control, deliver_interval_packets=25)
    for label, fhz in FT8:
        multi.add_channel(
            frequency_hz=fhz,
            preset="usb",
            sample_rate=12000,
            encoding=2,
            on_samples=make_sink(label),
        )
    multi.start()
    try:
        time.sleep(60 * 15)     # 15 minutes
    finally:
        multi.stop()
```

All 10 channels ride the same socket. On a Pi 4 this runs at a few
percent of one core.

---

## Health monitoring and self-healing

After `start()`, a background `MultiStream-Health` thread wakes
roughly every `drop_timeout_sec / 4` seconds and:

1. Marks any slot silent longer than `drop_timeout_sec` as `dropped`
   and fires `on_stream_dropped(reason)`.
2. For already-dropped slots, calls
   `control.ensure_channel(...)`. On success, swaps in a fresh
   `PacketResequencer` and `StreamQuality`, updates the slot's
   `channel_info`, and fires `on_stream_restored(channel_info)`.
3. If `ensure_channel()` re-allocates a different SSRC (rare — only
   if channel parameters changed), the slot is re-keyed to the new
   SSRC.

The health thread idles for 10 seconds after `start()` to let packets
start flowing before arming the drop detector.

---

## Caveats and limitations

Based on the current source ([`ka9q/multi_stream.py`](../ka9q/multi_stream.py)):

- **One multicast group per MultiStream.** `add_channel()` raises
  `ValueError` if a channel's `ensure_channel()` resolves to a
  different `(address, port)`. Use one `MultiStream` per group if
  your channels span groups.
- **No `add_channel()` after `start()`.** The slot dict is not
  guarded for concurrent mutation; adding after start is not a
  supported call pattern.
- **No `remove_channel()` in the public API.** To remove a channel,
  stop the `MultiStream`, rebuild it with the remaining channels, and
  start again. For long-lived recorders this has been acceptable.
- **Linear-PCM encodings** are decoded by `parse_rtp_samples()` in
  pure NumPy: `S16LE/BE`, `F32LE/BE`, `F16LE/BE`, `MULAW`, `ALAW`.
  All radiod-emitted sample encodings work out of the box.
- **Opus** (`OPUS`, `OPUS_VOIP`) is a framed codec, not raw samples
  — `parse_rtp_samples()` returns `None` for those payloads. Wrap
  the stream with `ka9q.stream.OpusDecoder` (requires the `[opus]`
  install extra) to recover float32 PCM, keeping one decoder
  instance per SSRC so packet-loss concealment works.
- **AX25** (encoding 5) is framed protocol data, also not samples —
  `parse_rtp_samples()` returns `None`. Handle the bytes yourself.
- **`samples_per_packet=320` default** assumes the typical 12 kHz /
  26.67 ms RTP packetization used by radiod. If your channels run
  at a different packet cadence, set it explicitly.
- **Resequence buffer is per-channel** with `buffer_size=64` packets
  default. Tune up for high jitter, down for low-latency applications.

---

## Related

- [`ManagedStream`](../ka9q/managed_stream.py) — single-channel
  self-healing wrapper, same callback shape.
- [`RadiodStream`](../ka9q/stream.py) — raw continuous-sample stream
  without self-healing.
- [`PacketResequencer`](../ka9q/resequencer.py) — handles out-of-order
  RTP, emits gap events.
- [`StreamQuality`](../ka9q/stream_quality.py) — per-delivery metadata
  passed to every `on_samples` call.
- [RECIPES.md § Recipe 2](RECIPES.md#recipe-2--fixed-sets-of-same-type-channels-wspr-psk-ft8-timing)
  — end-to-end recorder pattern.
