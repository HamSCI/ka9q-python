# API Reference — ka9q-python

Reference for every public symbol exported from `ka9q/__init__.py`
(version 3.9.0).

The package is installed as `ka9q-python` and imported as `ka9q`:

```python
import ka9q
from ka9q import RadiodControl, ManagedStream, ChannelStatus
```

Source files are linked inline; only public, re-exported symbols are
documented here.

---

## Contents

1. [Quickstart](#quickstart)
2. [RadiodControl](#radiodcontrol)
3. [Discovery](#discovery)
4. [Types & Enums](#types--enums)
5. [Typed Status Decoders](#typed-status-decoders)
6. [Streams](#streams)
7. [RTP Recording](#rtp-recording)
8. [L6 BPSK PPS Calibration](#l6-bpsk-pps-calibration)
9. [Utilities](#utilities)
10. [Exceptions](#exceptions)
11. [CLI: `ka9q`](#cli-ka9q)

---

## Quickstart

ka9q-python provides four layers for consuming a radiod RTP stream.
Pick the highest layer that fits:

| Layer | Class | Use when... |
|---|---|---|
| Raw RTP packets | [`RTPRecorder`](../ka9q/rtp_recorder.py) | Timing accuracy matters (WSPR/FT8/science) |
| Continuous samples | [`RadiodStream`](../ka9q/stream.py) | You want numpy arrays with gap-filling |
| Self-healing samples | [`ManagedStream`](../ka9q/managed_stream.py) | Long-running client that must survive radiod restarts |
| Many channels, one socket | [`MultiStream`](../ka9q/multi_stream.py) | 10+ channels on the same multicast group |
| Spectrum / FFT data | [`SpectrumStream`](../ka9q/spectrum_stream.py) | Spectrogram display, band monitoring, signal search |

All layers sit on top of [`RadiodControl`](../ka9q/control.py), which
speaks the TLV protocol to radiod over multicast UDP.

```python
from ka9q import RadiodControl, ManagedStream

def on_samples(samples, quality):
    print(f"{len(samples)} samples, {quality.completeness_pct:.1f}% complete")

with RadiodControl("radiod.local") as control:
    stream = ManagedStream(
        control=control,
        frequency_hz=14.074e6,
        preset="usb",
        sample_rate=12000,
        encoding=1,                # S16LE
        on_samples=on_samples,
    )
    stream.start()
    # ... runs through radiod restarts, reports via callbacks ...
    stream.stop()
```

---

## RadiodControl

Central control class; speaks ka9q-radio's TLV binary protocol over
multicast. All public methods are guarded by an `RLock` and are safe
to call from multiple threads. Source: [control.py](../ka9q/control.py).

### Construction

```python
RadiodControl(
    status_address: str,
    max_commands_per_sec: int = 100,
    interface: Optional[str] = None,
)
```

- `status_address` — mDNS name (e.g. `"radiod.local"`) or multicast
  IPv4 of the radiod status group.
- `max_commands_per_sec` — token-bucket rate limit (default 100).
- `interface` — IP address of the NIC to bind multicast to. Required
  on multi-homed systems; `None` uses `INADDR_ANY`.

Raises [`ConnectionError`](#exceptions) on connect failure.

Supports `with` (context manager) and will `close()` on exit.

```python
with RadiodControl("bee1-hf.local", interface="192.168.1.100") as c:
    ...
```

### Channel creation & lifecycle

| Method | Purpose |
|---|---|
| `ensure_channel(frequency_hz, preset="iq", sample_rate=16000, agc_enable=0, gain=0.0, destination=None, encoding=0, timeout=5.0, frequency_tolerance=1.0) -> ChannelInfo` | Recommended high-level entry point. Computes a deterministic SSRC, creates the channel if it doesn't already exist, verifies it via discovery, and returns a [`ChannelInfo`](#channelinfo). Safe to call repeatedly. |
| `create_channel(frequency_hz, preset="iq", sample_rate=None, agc_enable=0, gain=0.0, destination=None, encoding=0, ssrc=None) -> int` | Lower-level: sends a single atomic TLV create packet and returns the SSRC. Does not verify. |
| `verify_channel(ssrc, expected_freq=None) -> bool` | Poll radiod and check the channel exists (and matches `expected_freq` if given). |
| `remove_channel(ssrc)` | Destroy a channel. |
| `tune(ssrc, frequency_hz=None, preset=None, sample_rate=None, low_edge=None, high_edge=None, gain=None, agc_enable=None, rf_gain=None, rf_atten=None, encoding=None, destination=None, timeout=5.0) -> dict` | Multi-parameter tune in a single round-trip, matching ka9q-radio's `tune.c`. Returns a status dict. |

```python
with RadiodControl("radiod.local") as control:
    ch = control.ensure_channel(
        frequency_hz=14.074e6, preset="usb",
        sample_rate=12000, encoding=1,
    )
    print(f"SSRC={ch.ssrc} at {ch.multicast_address}:{ch.port}")
    control.remove_channel(ch.ssrc)
```

### Status queries

- `poll_status(ssrc, timeout=2.0) -> ChannelStatus` — Send an
  SSRC-only command and return the typed [`ChannelStatus`](#channelstatus).
  Does not change channel state.
- `listen_status(callback, duration=None, ssrcs=None)` — Passively
  receive radiod's periodic status multicast and fan out to
  `callback(status: ChannelStatus)`. Optionally filter by an `ssrcs`
  set. Blocks for `duration` seconds (or forever).
- `get_metrics() -> dict` / `reset_metrics()` — Library-side counters
  (commands sent, retries, etc.).

### Parameter setters

All setters validate their inputs and raise [`ValidationError`](#exceptions)
or [`CommandError`](#exceptions). SSRC is always the first argument.

Tuning & filter:
- `set_frequency(ssrc, frequency_hz)`
- `set_preset(ssrc, preset)`  (preset names: `"iq"`, `"usb"`, `"lsb"`, `"am"`, `"fm"`, `"cw"`, ...)
- `set_sample_rate(ssrc, sample_rate)`
- `set_shift_frequency(ssrc, shift_hz)`
- `set_first_lo(ssrc, frequency_hz)`
- `set_doppler(ssrc, doppler_hz=0.0, doppler_rate_hz_per_sec=0.0)`
- `set_filter(ssrc, low_edge=None, high_edge=None, kaiser_beta=None)`
- `set_kaiser_beta(ssrc, beta)`
- `set_filter2(ssrc, blocksize, kaiser_beta=None)`

Gain / AGC:
- `set_gain(ssrc, gain_db)`
- `set_agc(ssrc, enable, hangtime=None, headroom=None, recovery_rate=None, threshold=None)`
- `set_agc_hangtime(ssrc, seconds)`
- `set_agc_recovery_rate(ssrc, db_per_sec)`
- `set_agc_threshold(ssrc, threshold_db)`
- `set_headroom(ssrc, headroom_db)`
- `set_output_level(ssrc, level)`
- `set_rf_gain(ssrc, gain_db)`
- `set_rf_attenuation(ssrc, atten_db)`

Demod-specific:
- `set_demod_type(ssrc, demod_type)`  (see [`DemodType`](#demodtype))
- `set_pll(ssrc, enable, bandwidth_hz=None, square=False)`
- `set_squelch(ssrc, enable=True, open_snr_db=None, close_snr_db=None)`
- `set_output_channels(ssrc, channels)`  (1 or 2)
- `set_independent_sideband(ssrc, enable)`
- `set_envelope_detection(ssrc, enable)`
- `set_fm_threshold_extension(ssrc, enable)`
- `set_pl_tone(ssrc, freq_hz)`

Output / RTP / Opus:
- `set_destination(ssrc, address, port=5004)`
- `set_output_encoding(ssrc, encoding)`  (see [`Encoding`](#encoding))
- `set_opus_bitrate(ssrc, bitrate)`
- `set_opus_dtx(ssrc, enable)`
- `set_opus_application(ssrc, application)`
- `set_opus_bandwidth(ssrc, bandwidth)`
- `set_opus_fec(ssrc, loss_percent)`

Spectrum / misc:
- `set_spectrum(ssrc, bin_bw_hz=None, bin_count=None, ...)`  (see source)
- `set_status_interval(ssrc, interval)`
- `set_max_delay(ssrc, max_blocks)`
- `set_packet_buffering(ssrc, min_blocks)`
- `set_lock(ssrc, lock)`  (freeze retune)
- `set_description(ssrc, description)`
- `set_options(ssrc, set_bits=0, clear_bits=0)`  (bitmask)

For full signatures and protocol constants see
[control.py](../ka9q/control.py).

### Low-level escape hatches

- `send_command(cmdbuffer, max_retries=3, retry_delay=0.1)` — Send a
  pre-built TLV command buffer. Rate-limited.
- `close()` — Release sockets. Safe to call more than once; invoked
  automatically by the context manager and destructor.

### `allocate_ssrc()` (module-level)

```python
allocate_ssrc(frequency_hz, preset="iq", sample_rate=16000,
              agc=False, gain=0.0, destination=None,
              encoding=0, radiod_host=None) -> int
```

Deterministic 31-bit SSRC hashed from channel parameters. The same
inputs always yield the same SSRC, enabling stream sharing across
restarts and processes. Hash algorithm matches `signal-recorder`'s
`StreamSpec.ssrc_hash()`.

---

## Discovery

Source: [discovery.py](../ka9q/discovery.py). All discovery calls are
read-only — they listen passively and, if needed, send a single poll.

```python
discover_channels(status_address, listen_duration=2.0,
                  use_native=True, interface=None) -> Dict[int, ChannelInfo]
```
Primary entry point. Tries native Python discovery first, falls back to
the external `control` utility from ka9q-radio on failure.

```python
discover_channels_native(status_address, listen_duration=2.0,
                         interface=None) -> Dict[int, ChannelInfo]
```
Pure-Python: joins the status multicast group, sends a broadcast poll,
and decodes the TLV responses for `listen_duration` seconds.

```python
discover_channels_via_control(status_address, timeout=30.0)
    -> Dict[int, ChannelInfo]
```
Shells out to the `control -v` utility. Fallback only; requires
ka9q-radio installed locally.

```python
discover_radiod_services(timeout=10.0) -> list[dict]
```
Find all `_ka9q-ctl._udp` services on the LAN via `avahi-browse`.
Returns `[{"name": ..., "address": ...}, ...]` sorted by name.

### `ChannelInfo`

Dataclass returned by discovery and `ensure_channel()`.

```python
@dataclass
class ChannelInfo:
    ssrc: int
    preset: str
    sample_rate: int
    frequency: float                      # Hz
    snr: float                            # dB
    multicast_address: str                # RTP destination
    port: int                             # RTP destination port
    gps_time: Optional[int] = None        # GPS ns at rtp_timesnap
    rtp_timesnap: Optional[int] = None    # RTP ts aligned to gps_time
    encoding: int = 0                     # see Encoding
    chain_delay_correction_ns: Optional[int] = None  # L6 BPSK PPS correction
```

The `gps_time` / `rtp_timesnap` pair is what
[`rtp_to_wallclock()`](#rtp-recording) uses to convert RTP timestamps
to UTC.

---

## Types & Enums

Source: [types.py](../ka9q/types.py). Auto-generated from ka9q-radio's
C headers by `scripts/sync_types.py`.

### `StatusType`

110+ integer constants for TLV type tags (e.g.
`StatusType.RADIO_FREQUENCY = 33`, `StatusType.OUTPUT_SSRC = 18`).
Used internally by the TLV encoder/decoder. See [types.py](../ka9q/types.py)
for the full list — they mirror ka9q-radio's `status.h`.

### `Encoding`

RTP output encoding (integer values match ka9q-radio's `rtp.h`):

| Name | Value | Decoded by | Notes |
|---|---|---|---|
| `NO_ENCODING` | 0 | `parse_rtp_samples` | radiod default → treated as F32LE |
| `S16LE` | 1 | `parse_rtp_samples` | Signed 16-bit little-endian PCM |
| `S16BE` | 2 | `parse_rtp_samples` | Signed 16-bit big-endian PCM |
| `OPUS` | 3 | `OpusDecoder` (extra) | Opus audio — needs `[opus]` install extra |
| `F32LE` | 4 | `parse_rtp_samples` | 32-bit float LE (also `Encoding.F32`) |
| `AX25` | 5 | (caller handles bytes) | Packet radio — framed protocol, not samples |
| `F16LE` | 6 | `parse_rtp_samples` | 16-bit float LE (also `Encoding.F16`) |
| `OPUS_VOIP` | 7 | `OpusDecoder` (extra) | Opus with `APPLICATION_VOIP` |
| `F32BE` | 8 | `parse_rtp_samples` | 32-bit float BE |
| `F16BE` | 9 | `parse_rtp_samples` | 16-bit float BE |
| `MULAW` | 10 | `parse_rtp_samples` | G.711 µ-law (table-based, pure NumPy) |
| `ALAW` | 11 | `parse_rtp_samples` | G.711 A-law (table-based, pure NumPy) |

### `DemodType`

`LINEAR_DEMOD=0`, `FM_DEMOD=1`, `WFM_DEMOD=2`, `SPECT_DEMOD=3`,
`SPECT2_DEMOD=4`.

### `WindowType`

FFT window constants: `KAISER_WINDOW=0`, `RECT_WINDOW=1`,
`BLACKMAN_WINDOW=2`, `EXACT_BLACKMAN_WINDOW=3`, `GAUSSIAN_WINDOW=4`,
`HANN_WINDOW=5`, `HAMMING_WINDOW=6`, `BLACKMAN_HARRIS_WINDOW=7`,
`HP5FT_WINDOW=8`.

---

## Typed Status Decoders

Source: [status.py](../ka9q/status.py). These dataclasses mirror the
C `struct channel` / `struct frontend` that radiod serialises into
each status packet. They are a typed superset of the dict returned by
`RadiodControl._decode_status_response()`.

```python
decode_status_packet(buffer: bytes) -> Optional[ChannelStatus]
```
Decode a raw TLV status packet. Returns `None` if the first byte
isn't 0 (i.e. not a status packet). Unknown TLV tags are skipped.

### `ChannelStatus`

Top-level dataclass with nested sub-structures. Selected fields,
grouped by category:

- **Identification** — `ssrc`, `description`, `preset`, `demod_type`,
  `rtp_pt`, `command_tag`, `gps_time`, `rtp_timesnap`.
- **Tuning** — `frequency`, `first_lo`, `second_lo`, `shift`,
  `doppler`, `doppler_rate`.
- **Filter** — `low_edge`, `high_edge`, `kaiser_beta`,
  `filter_blocksize`, `filter_fir_length`, `filter_drops`, `noise_bw`.
- **Squelch / AGC / gain** — `snr_squelch_enable`, `squelch_open`,
  `squelch_close`, `agc_enable`, `gain`, `headroom`, `agc_hangtime`,
  `agc_recovery_rate`, `agc_threshold`, `output_level`,
  `baseband_power`, `noise_density`, `envelope`.
- **Output / RTP** — `output_ssrc`, `output_samprate`,
  `output_channels`, `output_encoding`, `output_data_dest_socket`,
  `output_data_source_socket`, `output_ttl`, `output_samples`,
  `output_data_packets`, `output_metadata_packets`, `output_errors`,
  `maxdelay`.
- **Nested sub-structures** — `pll`, `fm`, `spectrum`, `filter2`,
  `opus`, `frontend` (see below).

Derived properties:

- `bandwidth` — `|high_edge − low_edge|`.
- `snr` — dB, from baseband and noise density.
- `snr_per_hz` — dB-Hz.
- `demod_name` — human-readable demodulator (`"Linear"`, `"FM"`, ...).
- `encoding_name` — symbolic `Encoding` name.

Helpers:

- `to_dict()` — flattens to nested JSON-safe dict.
- `get_field(path)` — dotted-path accessor (e.g.
  `status.get_field("pll.lock")` or `status.get_field("frontend.calibrate")`).
- `field_names()` — all dotted paths that are currently populated.
  This is what the CLI `query --field` and TUI autocompletion use.

```python
status = control.poll_status(ssrc)
print(status.frequency, status.pll.lock, status.frontend.calibrate_ppm)
for path in status.field_names():
    print(path, "=", status.get_field(path))
```

### `FrontendStatus`

State of the SDR front-end (RX888, etc.), embedded as
`ChannelStatus.frontend`. Key fields: `description`, `input_samprate`,
`ad_bits_per_sample`, `ad_over`, `calibrate`, `first_lo`, `lock`,
`fe_low_edge`, `fe_high_edge`, `lna_gain`, `mixer_gain`, `if_gain`,
`rf_gain`, `rf_atten`, `rf_agc`, `if_power`, `rf_level_cal`,
`dc_i_offset`, `dc_q_offset`, `iq_imbalance`, `iq_phase`.

Derived properties:

- `calibrate_ppm` — GPSDO clock error in ppm.
- `gpsdo_reference_hz` — implied reference frequency (10 MHz × (1 + calibrate)).
- `input_power_dbm` — absolute power if the front-end is calibrated.

### Sub-status dataclasses

- `PllStatus` — `enable`, `lock`, `square`, `phase`, `bw`, `snr`,
  `wraps`, `freq_offset`.
- `FmStatus` — `peak_deviation`, `fm_snr`, `pl_tone`, `pl_deviation`,
  `deemph_tc`, `deemph_gain`, `threshold_extend`.
- `SpectrumStatus` — `avg`, `base`, `step`, `shape`, `fft_n`,
  `overlap`, `resolution_bw`, `noise_bw`, `bin_count`, `crossover`,
  `window_type`, `bin_data`, `bin_byte_data`.

  **Bin vectors** (populated when the status packet contains spectrum
  data from a `SPECT_DEMOD` or `SPECT2_DEMOD` channel):

  - `bin_data: Optional[np.ndarray]` — float32 I²+Q² power per bin
    (SPECT_DEMOD). Bin 0 = DC, 1..N/2 = positive, N/2+1..N-1 = negative.
  - `bin_byte_data: Optional[np.ndarray]` — uint8 quantised log-power
    (SPECT2_DEMOD). Reconstruct dB with `base + byte * step`.
  - `bin_power_db -> Optional[np.ndarray]` — **property** that returns
    dB values regardless of source format (10*log10 for float,
    base + byte*step for byte). Returns `None` if no bin data is present.
- `Filter2Status` — `blocking`, `blocksize`, `fir_length`,
  `kaiser_beta`.
- `OpusStatus` — `bit_rate`, `dtx`, `application`, `bandwidth`, `fec`.

---

## Streams

### `parse_rtp_samples()`

Source: [stream.py](../ka9q/stream.py). Decodes one RTP payload to a
NumPy sample array. Shared by `RadiodStream` and `MultiStream`.

```python
parse_rtp_samples(
    payload: bytes,
    encoding: int,        # any Encoding.* value
    is_iq: bool,          # True → complex64 output; False → float32
) -> np.ndarray | None
```

Covers every linear-PCM encoding radiod's `pt_from_info()` can grant:
`NO_ENCODING`, `S16LE`, `S16BE`, `F32LE`, `F32BE`, `F16LE`, `F16BE`,
`MULAW`, `ALAW`. G.711 µ-law / A-law use table-based NumPy decoders
(no `audioop` dependency — that module is removed in Python 3.13).
Unsupported encodings log a warning and fall back to F32LE.

Returns `None` for `OPUS`, `OPUS_VOIP` (use `OpusDecoder` below), and
`AX25` (framed protocol data, handle the bytes yourself).

### `OpusDecoder`

Source: [stream.py](../ka9q/stream.py). Optional Opus payload decoder
for radiod's `OPUS` / `OPUS_VOIP` streams. Requires the `[opus]`
install extra (`pip install ka9q-python[opus]`, which pulls
`opuslib`).

```python
OpusDecoder(sample_rate: int = 48000, channels: int = 1)
```

- `decode(payload: bytes, *, fec: bool = False) -> np.ndarray` —
  one Opus frame → float32 PCM, normalised to ±1.0. Stereo output is
  interleaved L,R,L,R,…. Empty payload triggers one frame of
  packet-loss concealment.
- `sample_rate` / `channels` properties expose the codec config.

Maintain one `OpusDecoder` instance per stream SSRC so the internal
codec state — and therefore PLC — works correctly across packets.
`sample_rate` must be one of 8000/12000/16000/24000/48000.

```python
from ka9q.stream import OpusDecoder

dec = OpusDecoder(sample_rate=48000, channels=1)
for payload in opus_payloads:
    samples = dec.decode(payload)  # float32, mono
```

### `RadiodStream`

Source: [stream.py](../ka9q/stream.py). Continuous-sample consumer
with packet resequencing and gap-filling.

```python
RadiodStream(
    channel: ChannelInfo,
    on_samples: Callable[[np.ndarray, StreamQuality], None] | None = None,
    samples_per_packet: int = 320,
    resequence_buffer_size: int = 64,
    deliver_interval_packets: int = 10,
)
```

- `start()` — open the multicast socket and begin the receive thread.
- `stop() -> StreamQuality` — stop and return the final quality report.
- `is_running() -> bool`
- `get_quality() -> StreamQuality` — snapshot of the live quality state.

Sample dtype depends on the channel: IQ modes (`"iq"`, `"spectrum"`)
produce `complex64`; audio modes produce `float32`. The callback is
invoked every `deliver_interval_packets` packets with a concatenated
numpy array and a `StreamQuality` snapshot.

```python
def on_samples(samples, quality):
    print(f"{len(samples)} sa, complete={quality.completeness_pct:.1f}%")

stream = RadiodStream(channel, on_samples=on_samples)
stream.start(); time.sleep(10); stream.stop()
```

### `ManagedStream`

Source: [managed_stream.py](../ka9q/managed_stream.py). Wraps
`RadiodStream` with health monitoring and automatic re-establishment
through radiod restarts.

```python
ManagedStream(
    control,                     # RadiodControl
    frequency_hz,
    preset="iq",
    sample_rate=16000,
    agc_enable=0,
    gain=0.0,
    destination=None,
    encoding=0,
    on_samples=None,             # (np.ndarray, StreamQuality) -> None
    on_stream_dropped=None,      # (reason: str) -> None
    on_stream_restored=None,     # (ChannelInfo) -> None
    drop_timeout_sec=3.0,
    restore_interval_sec=1.0,
    max_restore_attempts=0,      # 0 = unlimited
    samples_per_packet=320,
    resequence_buffer_size=64,
    deliver_interval_packets=10,
)
```

- `start() -> ChannelInfo` — establish channel and begin streaming.
- `stop() -> ManagedStreamStats` — halt threads and return aggregate stats.
- `state -> StreamState` / `channel -> ChannelInfo` / `is_healthy -> bool`.
- `get_stats() -> ManagedStreamStats` / `get_quality() -> StreamQuality`.
- Supports the `with` protocol (`__enter__`/`__exit__`).

#### `StreamState`

Enum: `STOPPED`, `STARTING`, `HEALTHY`, `DROPPED`, `RESTORING`.

#### `ManagedStreamStats`

Dataclass with `state`, `total_drops`, `total_restorations`,
`last_drop_time`, `last_restore_time`, `last_drop_reason`,
`current_healthy_duration_sec`, `total_healthy_duration_sec`,
`total_dropped_duration_sec`. `copy()` returns a snapshot.

### `MultiStream`

Source: [multi_stream.py](../ka9q/multi_stream.py). A single receive
socket demultiplexes many SSRCs; every channel on the same multicast
group shares one kernel copy and one thread. Use this for 10+
channels to avoid the N-socket per-packet-copy cost.

```python
MultiStream(
    control,
    drop_timeout_sec=15.0,
    restore_interval_sec=5.0,
    deliver_interval_packets=10,
    samples_per_packet=320,
    resequence_buffer_size=64,
)
```

- `add_channel(frequency_hz, preset="usb", sample_rate=12000, encoding=0, agc_enable=0, gain=0.0, on_samples=None, on_stream_dropped=None, on_stream_restored=None) -> ChannelInfo`
  — provisions one channel (via `ensure_channel()`) and registers
  its callbacks. Must be called before `start()`. Raises `ValueError`
  if the new channel resolves to a different multicast group from
  already-added channels.
- `start()` — bind the shared socket, launch receive + health threads.
- `stop()` — stop threads, flush per-channel resequencers, close socket.

```python
multi = MultiStream(control)
for freq in (14.074e6, 7.074e6, 3.573e6):
    multi.add_channel(frequency_hz=freq, preset="usb",
                      sample_rate=12000, encoding=1,
                      on_samples=lambda s, q, f=freq: handle(f, s, q))
multi.start()
```

### `SpectrumStream`

Source: [spectrum_stream.py](../ka9q/spectrum_stream.py). Receives
real-time FFT spectrum data from radiod via the status multicast
channel (port 5006). Unlike audio streams which use RTP, spectrum
data arrives as `BIN_DATA` or `BIN_BYTE_DATA` TLV vectors inside
status packets.

```python
SpectrumStream(
    control,                     # RadiodControl
    frequency_hz,                # center frequency (Hz)
    bin_count=1024,              # number of FFT bins
    resolution_bw=100.0,         # bin bandwidth (Hz)
    *,
    demod_type=DemodType.SPECT2_DEMOD,  # SPECT_DEMOD or SPECT2_DEMOD
    window_type=None,            # see WindowType (default: Kaiser)
    kaiser_beta=None,            # Kaiser window shape parameter
    averaging=None,              # FFTs averaged per response
    overlap=None,                # window overlap ratio (0.0–1.0)
    poll_interval_sec=0.1,       # seconds between poll commands
    on_spectrum=None,            # (ChannelStatus) -> None
)
```

- `start() -> int` — create the spectrum channel and begin receiving.
  Returns the SSRC.
- `stop()` — stop threads, close socket, remove the channel from radiod.
- `set_frequency(frequency_hz)` — retune the spectrum center frequency.
- `ssrc -> Optional[int]` — the allocated SSRC.
- `frames_received -> int` — count of spectrum frames delivered.
- Supports `with` (context manager).

The `on_spectrum` callback receives a fully-decoded `ChannelStatus`
whose `.spectrum.bin_data` or `.spectrum.bin_byte_data` is populated.
Use `.spectrum.bin_power_db` for a format-independent numpy array of
dB values.

```python
from ka9q import RadiodControl, SpectrumStream

def on_spectrum(status):
    db = status.spectrum.bin_power_db
    freq = status.frequency
    rbw = status.spectrum.resolution_bw
    print(f"{len(db)} bins at {freq/1e6:.3f} MHz, "
          f"peak {db.max():.1f} dB, floor {db.min():.1f} dB")

with RadiodControl("radiod.local") as ctl:
    with SpectrumStream(
        control=ctl,
        frequency_hz=14.1e6,
        bin_count=2048,
        resolution_bw=50.0,
        on_spectrum=on_spectrum,
    ) as stream:
        time.sleep(30)  # receive for 30 seconds
```

#### How spectrum data flows

Spectrum data uses a completely different path from audio:

| | Audio | Spectrum |
|---|---|---|
| Transport | RTP on data multicast (port 5004) | TLV inside status multicast (port 5006) |
| Packet type | RTP with audio payload | Status packet with `BIN_DATA`/`BIN_BYTE_DATA` vectors |
| Trigger | Continuous (radiod pushes) | Poll-driven (`SpectrumStream` sends periodic COMMAND packets) |
| Demod type | `LINEAR_DEMOD`, `FM_DEMOD`, `WFM_DEMOD` | `SPECT_DEMOD` (float32) or `SPECT2_DEMOD` (uint8) |

`SpectrumStream` handles the polling, socket management, SSRC
filtering, and TLV decoding internally. The callback receives a
ready-to-use `ChannelStatus` with numpy arrays.

---

### `StreamQuality`, `GapSource`, `GapEvent`

Source: [stream_quality.py](../ka9q/stream_quality.py). Delivered to
every `on_samples` callback.

```python
@dataclass
class StreamQuality:
    # per-batch
    batch_start_sample: int
    batch_samples_delivered: int
    batch_gaps: List[GapEvent]
    # cumulative
    total_samples_delivered: int
    total_samples_expected: int
    total_gaps_filled: int
    total_gap_events: int
    # RTP stats
    rtp_packets_received: int
    rtp_packets_expected: int
    rtp_packets_lost: int
    rtp_packets_late: int
    rtp_packets_duplicate: int
    rtp_packets_resequenced: int
    # timing
    stream_start_utc: str
    last_packet_utc: str
    first_rtp_timestamp: int
    last_rtp_timestamp: int
    sample_rate: int
```

Properties: `completeness_pct`, `has_gaps`. Methods: `to_dict()`,
`copy()`.

`GapSource` enum: `NETWORK_LOSS`, `RESEQUENCE_TIMEOUT`,
`EMPTY_PAYLOAD`, `STREAM_START`, `STREAM_INTERRUPTION`. Applications
may define their own gap kinds separately.

`GapEvent(source, position_samples, duration_samples, timestamp_utc,
packets_affected=0)` — one contiguous zero-fill region.

### `PacketResequencer`, `RTPPacket`, `ResequencerStats`

Source: [resequencer.py](../ka9q/resequencer.py). Lower-level
building block that `RadiodStream` and `MultiStream` share. You'll
only use it directly if you're writing a new stream layer.

```python
PacketResequencer(buffer_size=64, samples_per_packet=320, sample_rate=16000)

# per packet:
samples, gap_events = reseq.process_packet(RTPPacket(...))
# shutdown:
final_samples, final_gaps = reseq.flush()
```

Also: `reset()`, `get_stats() -> dict`. Signed 32-bit arithmetic
tolerates RTP timestamp wrap. Fragmented IQ packets are handled by
using the *actual* sample count per packet, not the nominal one.

`RTPPacket(sequence, timestamp, ssrc, samples, wallclock=None)` —
parsed packet ready for the resequencer.

`ResequencerStats` — `packets_received`, `packets_resequenced`,
`packets_duplicate`, `gaps_detected`, `samples_output`,
`samples_filled`. Use `.to_dict()` for JSON.

---

## RTP Recording

Source: [rtp_recorder.py](../ka9q/rtp_recorder.py). Packet-oriented
interface for applications that need per-packet control and precise
GPS-referenced timing (WSPR, FT8, scientific capture).

### `RTPRecorder`

```python
RTPRecorder(
    channel: ChannelInfo,
    on_packet: Callable[[RTPHeader, bytes, float], None] | None = None,
    on_state_change: Callable[[RecorderState, RecorderState], None] | None = None,
    on_recording_start: Callable[[], None] | None = None,
    on_recording_stop: Callable[[RecordingMetrics], None] | None = None,
    max_packet_gap: int = 10,
    resync_threshold: int = 5,
    pass_all_packets: bool = False,
)
```

- `start()` / `stop()` — open/close the multicast socket and thread.
- `start_recording()` / `stop_recording()` — arm/disarm the state
  machine (IDLE → ARMED → RECORDING → RESYNC).
- `get_metrics() -> dict` / `reset_metrics()`.

`on_packet(header, payload, wallclock)` is called for every validated
packet. `wallclock` is a Unix-time float computed from radiod's
`GPS_TIME`/`RTP_TIMESNAP`, minus any chain-delay correction on
`ChannelInfo`.

### `RecorderState`

Enum: `IDLE`, `ARMED`, `RECORDING`, `RESYNC`.

### `RTPHeader`

`NamedTuple`: `version, padding, extension, csrc_count, marker,
payload_type, sequence, timestamp, ssrc`.

### `RecordingMetrics`

Dataclass: `packets_received`, `packets_dropped`,
`packets_out_of_order`, `bytes_received`, `sequence_errors`,
`timestamp_jumps`, `state_changes`, `recording_start_time`,
`recording_stop_time`. `to_dict()` adds `recording_duration`.

### Module-level helpers

```python
parse_rtp_header(data: bytes) -> Optional[RTPHeader]
rtp_to_wallclock(
    rtp_timestamp: int,
    channel: ChannelInfo,
    wallclock_hint_sec: Optional[float] = None,
) -> Optional[float]
```

`rtp_to_wallclock()` returns `None` unless `channel.gps_time` and
`channel.rtp_timesnap` are both populated. When
`channel.chain_delay_correction_ns` is set (see below), it is
subtracted from the computed wallclock.

The `wallclock_hint_sec` parameter (v3.14.2+) lets callers supply an
approximate UTC reference for 32-bit RTP wrap-epoch disambiguation
without falling back to `time.time()`. Authority-aware callers
(e.g. those reading `rtp_to_utc_offset_ns` from an `hf-timestd`
authority.json) should pass this hint to keep the labeling path off
the chrony-disciplined system clock (METROLOGY.md §4.5 RTP-reference
invariant). The hint only needs ±period/2 accuracy (≥6 hours at
typical sample rates), so even a coarse value is sufficient. When
omitted, the function falls back to `time.time()` for backward
compatibility.

---

## L6 BPSK PPS Calibration

Source: [pps_calibrator.py](../ka9q/pps_calibrator.py). Measures the
end-to-end RF→ADC→DSP→RTP chain delay on a radiod instance by
detecting PPS phase flips on a BPSK IQ channel injected by a local
GPS-disciplined transmitter. The result is a single nanosecond
correction that applies to every channel on that radiod.

### `BpskPpsCalibrator`

```python
BpskPpsCalibrator(
    sample_rate: int,
    consecutive_required: int = 10,
    edge_tolerance_samples: int = 10,
    min_pulse_fraction: float = 0.99,
    enable_notch_500hz: bool = False,
)
```

- `process_samples(iq_samples: np.ndarray, rtp_timestamp: int) -> Optional[PpsCalibrationResult]`
  — feed one batch of complex64 IQ; returns a result once locked.
- `locked -> bool` — property, `True` once `pps_consecutive >= consecutive_required`.
- `reset()` — wipe state if the stream restarted.

### `PpsCalibrationResult`

Dataclass: `chain_delay_ns`, `chain_delay_samples`, `pps_ok`,
`pps_noise`, `pps_consecutive`, `locked`.

### `NotchFilter500Hz`

`NotchFilter500Hz(sample_rate, pole_radius=0.99)`. Stateful biquad
IIR notch at 500 Hz; exposed separately for callers who want to
pre-filter IQ before passing to other detectors. Call
`.process(iq_samples) -> np.ndarray`.

```python
cal = BpskPpsCalibrator(sample_rate=24000)
def on_samples(samples, quality):
    r = cal.process_samples(samples, quality.last_rtp_timestamp)
    if r is not None:
        for ch in other_channels:
            ch.chain_delay_correction_ns = r.chain_delay_ns
```

---

## Utilities

### `generate_multicast_ip`

Source: [addressing.py](../ka9q/addressing.py).

```python
generate_multicast_ip(unique_id: str, prefix: str = "239",
                      *, radiod_host: Optional[str] = None) -> str
```

Deterministic multicast IPv4 from a SHA-256 hash of `unique_id`
(optionally combined with `radiod_host`). Collision probability is
≈1 in 16.7M. Pass `radiod_host` when one client talks to multiple
radiod instances.

### `ChannelMonitor`

Source: [monitor.py](../ka9q/monitor.py). Background watchdog that
keeps a set of "desired" channels alive through radiod restarts,
without the callback overhead of `ManagedStream`.

```python
monitor = ChannelMonitor(control, check_interval=2.0)
monitor.start()
ssrc = monitor.monitor_channel(frequency_hz=14.074e6,
                               preset="usb", sample_rate=12000)
# ...later...
monitor.unmonitor_channel(ssrc)
monitor.stop()
```

Every `check_interval` seconds the monitor runs `discover_channels()`
and calls `control.ensure_channel(**params)` for anything that's
missing.

### `allocate_ssrc`

See [RadiodControl](#allocate_ssrc-module-level) above.

### `KA9Q_RADIO_COMMIT`

From [compat.py](../ka9q/compat.py). String giving the ka9q-radio
git commit this release of ka9q-python was validated against. Used
by `ka9q-update` and other deployment tooling to detect drift.

```python
from ka9q import KA9Q_RADIO_COMMIT
print(f"Tested against ka9q-radio {KA9Q_RADIO_COMMIT}")
```

---

## Exceptions

Source: [exceptions.py](../ka9q/exceptions.py). All derive from
`Ka9qError`.

| Class | Raised when |
|---|---|
| `Ka9qError` | Base class; catch this to catch everything from the library. |
| `ConnectionError` | `RadiodControl` cannot reach radiod (DNS, socket, etc.). |
| `CommandError` | A TLV command was rejected or the socket errored. |
| `ValidationError` | A parameter failed input validation (frequency range, SSRC range, preset name, multicast address format, ...). |

Note: `ConnectionError` shadows the built-in; import as
`ka9q.ConnectionError` or alias to avoid confusion.

---

## CLI: `ka9q`

Installed as a console script (also `python -m ka9q.cli`). Source:
[cli.py](../ka9q/cli.py). Every subcommand takes a radiod host as its
first positional argument.

```
ka9q [--interface IFACE] <list | query | set | tui> ...
```

### `ka9q list HOST`

Discover channels via multicast.

```
ka9q list HOST [--timeout SEC] [--json]
```
Prints an `SSRC | Frequency | Preset | Dest` table, or a JSON array
with `--json`.

### `ka9q query HOST`

Poll or watch typed status. Every `ChannelStatus` field (including
nested sub-structures) is addressable with `--field DOTTED.PATH`.

```
ka9q query HOST --ssrc N
ka9q query HOST --ssrc N --field pll.lock
ka9q query HOST --ssrc N --field frontend.calibrate --json
ka9q query HOST --watch                    # stream all SSRCs
ka9q query HOST --ssrc N --watch           # stream one SSRC
```

Flags: `--ssrc N`, `--field PATH`, `--json`, `--watch`,
`--timeout SEC` (default 2.0).

Without `--field` the command prints a multi-section human-readable
render (tuning / frontend / signal / filter / demod-specific /
squelch / output / Opus / TP).

### `ka9q set HOST --ssrc N PARAM VALUE`

Change one parameter. `PARAM` is one of:

```
frequency preset mode sample-rate samprate low-edge high-edge
kaiser-beta shift gain output-level headroom
agc agc-hangtime agc-recovery agc-threshold
rf-gain rf-atten
squelch-open squelch-close snr-squelch
pll pll-bw pll-square isb envelope
channels encoding demod-type pl-tone threshold-extend
lock description first-lo status-interval max-delay
opus-bitrate opus-dtx opus-application opus-bandwidth opus-fec
window destination
```

Each verb maps to a `RadiodControl` setter. Booleans accept
`1/0/true/false/yes/no/on/off`. Encoding / demod-type / window
accept either their integer value or the symbolic name (e.g.
`encoding S16LE`, `demod-type FM`).

```
ka9q set radiod.local --ssrc 12345678 frequency 14074000
ka9q set radiod.local --ssrc 12345678 encoding S16LE
ka9q set radiod.local --ssrc 12345678 pll true
ka9q set radiod.local --ssrc 12345678 destination 239.1.2.3:5004
```

### `ka9q tui [HOST]`

Launch the Textual-based TUI (requires the optional `[tui]` extra).

```
ka9q tui HOST [--ssrc N]
```

If `HOST` is omitted, the TUI presents the mDNS-discovered radiod
services from `discover_radiod_services()`.
