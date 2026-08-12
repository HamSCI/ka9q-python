# Control-Surface Completeness Matrix — pin..AUDIT_HEAD

**AUDIT_HEAD:** `cedec349f7b4212078de3e007d142b4c64d36546` (2026.08.12-1-trixie1)
**Clone:** `~/audit/ka9q-radio`, checked out to `AUDIT_HEAD` for extraction, restored
to `main` afterward (detached-HEAD hygiene, per brief Step 3).

This is the definitive map between every TLV `radiod` honors (as a command
or as a status field) at `AUDIT_HEAD`, and whether `ka9q/control.py` /
`ka9q/status.py` / `ka9q/discovery.py` actually reach it. Because
ka9q-python is the mandatory control path for `radiod` (no-bypass policy),
an unmapped writable TLV is a finding regardless of whether any current
sigmond-suite client happens to need it today.

---

## Step 1/2: Locating the writable and readable surfaces

```bash
cd ~/audit/ka9q-radio
git checkout cedec349f7b4212078de3e007d142b4c64d36546
grep -rn "decode_radio_commands" --include='*.c' .
```

All seven hits (`wfm.c`, `spectrum.c`, `radio.c`, `fm.c`, `linear.c` call
it; `radio_status.c` defines it) point to **`src/radio_status.c:136`** —
not `radio.c` as the brief's "historically" note suggested; the function
was moved out of `radio.c` into its own translation unit at some point
in this repo's history. `send_radio_status` (line 113) and the static
`encode_radio_status` (line 703) live in the same file, found via:

```bash
grep -n "encode_radio_status\|send_radio_status" --include='*.c' -r .
```

- **Writable surface** = every `case <ENUM>:` label inside
  `decode_radio_commands` (`src/radio_status.c:136`–`699`), across both
  its two passes (a first pass that special-cases `PRESET` so mode
  presets apply before other per-field overrides in the same packet, and
  a second pass for everything else). **51 unique TLVs.**
- **Readable surface** = every `encode_*(&bp, <ENUM>, ...)` call inside
  `encode_radio_status` (`src/radio_status.c:703`–`912`), including the
  mode-conditional blocks (`LINEAR_DEMOD`/`FM_DEMOD`/`WFM_DEMOD`/
  `SPECT_DEMOD`/`SPECT2_DEMOD`) and the two `if(chan->demod_type !=
  SPECT_DEMOD/SPECT2_DEMOD)` guarded sections. **106 unique TLVs.**
- Union of both sets: **107** of the **118** TLVs defined in
  `enum status_type` (`src/status.h`). The remaining 11
  (`DC_I_OFFSET`, `DC_Q_OFFSET`, `IQ_IMBALANCE`, `IQ_PHASE`,
  `DIRECT_CONVERSION`, `LOCK`, `UNUSED`, `UNUSED2`, `UNUSED3`,
  `UNUSED4`, `EOL`) are neither read nor written by `radiod` at
  `AUDIT_HEAD` and are out of scope for this matrix — see the LOCK
  callout under Gaps, though, since it is a special case.
- `git diff 14d780af..cedec349 -- src/status.h` is **empty** (confirmed
  independently here and already established in Task 3's
  `contract.md`): the TLV enum itself has not drifted since the
  `types.py` pin, so there are **zero "not-in-types.py" (upstream-new)
  TLVs** in this matrix — every name below already exists in
  `ka9q/types.py::StatusType`.

## Step 3: Mapping to ka9q-python

Writable TLVs were matched with:
```bash
grep -n "StatusType\.<NAME>" ka9q/control.py
```
counting a hit as "mapped" only when it is an `encode_*(cmdbuffer, StatusType.<NAME>, ...)`
call inside a `set_*`/`create_channel`/`tune` method body — not a
docstring example (`>>> encode_double(buf, StatusType.RADIO_FREQUENCY, ...)`
appears near the top of `control.py` as inline API documentation for the
`encode_*` helpers themselves) and not a decode branch
(`elif type_val == StatusType.<NAME>:` inside
`_decode_status_response`).

Readable TLVs were matched with:
```bash
grep -rn "StatusType\.<NAME>" ka9q/status.py ka9q/discovery.py
```
`ka9q/status.py::decode_status_packet` is the single decoder for the
full per-channel status packet (populates the `ChannelStatus`
dataclass); `ka9q/discovery.py::discover_channels_native` independently
decodes a lightweight subset for channel discovery. (The brief's Step 3
literally includes `ka9q/control.py` in this grep too; that file was
excluded here for the *readable* check because every hit it produces is
the TLV's own *write* call, not a decode — e.g. `SETOPTS` matches
`control.py` via `set_options()`'s `encode_int64` call, which would have
masked the real read-side gap documented below.)

### Full matrix

| # | TLV | dir | RadiodControl method / decoder | status |
|---|-----|-----|-------------------------------|--------|
| 1 | `COMMAND_TAG` | R/W | write:`create_channel`,`poll_channel`,`poll_status`,`remove_channel`,`set_agc`,`set_agc_hangtime`,`set_agc_recovery_rate`,`set_agc_threshold`,`set_channel_lifetime`,`set_demod_type`,`set_description`,`set_destination`,`set_doppler`,`set_envelope_detection`,`set_filter`,`set_filter2`,`set_first_lo`,`set_fm_threshold_extension`,`set_frequency`,`set_gain`,`set_headroom`,`set_independent_sideband`,`set_lock`,`set_max_delay`,`set_options`,`set_opus_application`,`set_opus_bandwidth`,`set_opus_bitrate`,`set_opus_dtx`,`set_opus_fec`,`set_output_channels`,`set_output_encoding`,`set_output_level`,`set_pl_tone`,`set_pll`,`set_preset`,`set_rf_attenuation`,`set_rf_gain`,`set_sample_rate`,`set_shift_frequency`,`set_spectrum`,`set_squelch`,`set_status_interval`,`tune` &#124; read:`status.py` | exposed |
| 2 | `CMD_CNT` | R-only | read:`status.py` | exposed |
| 3 | `GPS_TIME` | R-only | read:`status.py` | exposed |
| 4 | `DESCRIPTION` | R-only | read:`status.py` | exposed |
| 5 | `STATUS_DEST_SOCKET` | R-only | read:`status.py` | exposed |
| 6 | `SETOPTS` | R/W | write:`set_options` &#124; read: **none** | **gap** |
| 7 | `CLEAROPTS` | W-only | write:`set_options` | exposed |
| 8 | `RTP_TIMESNAP` | R-only | read:`status.py` | exposed |
| 9 | `BIN_BYTE_DATA` | R-only | read:`status.py` | exposed |
| 10 | `INPUT_SAMPRATE` | R-only | read:`status.py` | exposed |
| 11 | `SPECTRUM_BASE` | R/W | write: **none** &#124; read:`status.py` | **gap** |
| 12 | `SPECTRUM_AVG` | R/W | write: **none** &#124; read:`status.py` | **gap** |
| 13 | `INPUT_SAMPLES` | R-only | read:`status.py` | exposed |
| 14 | `WINDOW_TYPE` | R/W | write: **none** &#124; read:`status.py` | **gap** |
| 15 | `NOISE_BW` | R-only | read:`status.py` | exposed |
| 16 | `OUTPUT_DATA_SOURCE_SOCKET` | R-only | read:`status.py` | exposed |
| 17 | `OUTPUT_DATA_DEST_SOCKET` | R/W | write:`create_channel`,`set_destination`,`tune` &#124; read:`status.py` | exposed |
| 18 | `OUTPUT_SSRC` | R-only | read:`status.py` | exposed |
| 19 | `OUTPUT_TTL` | R-only | read:`status.py` | exposed |
| 20 | `OUTPUT_SAMPRATE` | R/W | write:`create_channel`,`set_sample_rate`,`tune` &#124; read:`status.py` | exposed |
| 21 | `OUTPUT_METADATA_PACKETS` | R-only | read:`status.py` | exposed |
| 22 | `OUTPUT_DATA_PACKETS` | R-only | read:`status.py` | exposed |
| 23 | `OUTPUT_ERRORS` | R-only | read:`status.py` | exposed |
| 24 | `CALIBRATE` | R-only | read:`status.py` | exposed |
| 25 | `LNA_GAIN` | R-only | read:`status.py` | exposed |
| 26 | `MIXER_GAIN` | R-only | read:`status.py` | exposed |
| 27 | `IF_GAIN` | R-only | read:`status.py` | exposed |
| 33 | `RADIO_FREQUENCY` | R/W | write:`create_channel`,`remove_channel`,`set_frequency`,`tune` &#124; read:`status.py` | exposed |
| 34 | `FIRST_LO_FREQUENCY` | R/W | write:`set_first_lo` &#124; read:`status.py` | exposed |
| 35 | `SECOND_LO_FREQUENCY` | R-only | read:`status.py` | exposed |
| 36 | `SHIFT_FREQUENCY` | R/W | write:`set_shift_frequency` &#124; read:`status.py` | exposed |
| 37 | `DOPPLER_FREQUENCY` | R/W | write:`set_doppler` &#124; read:`status.py` | exposed |
| 38 | `DOPPLER_FREQUENCY_RATE` | R/W | write:`set_doppler` &#124; read:`status.py` | exposed |
| 39 | `LOW_EDGE` | R/W | write:`create_channel`,`set_filter`,`tune` &#124; read:`status.py` | exposed |
| 40 | `HIGH_EDGE` | R/W | write:`create_channel`,`set_filter`,`tune` &#124; read:`status.py` | exposed |
| 41 | `KAISER_BETA` | R/W | write:`create_channel`,`set_filter` &#124; read:`status.py` | exposed |
| 42 | `FILTER_BLOCKSIZE` | R-only | read:`status.py` | exposed |
| 43 | `FILTER_FIR_LENGTH` | R-only | read:`status.py` | exposed |
| 44 | `FILTER2` | R/W | write:`set_filter2` &#124; read:`status.py` | exposed |
| 45 | `IF_POWER` | R-only | read:`status.py` | exposed |
| 46 | `BASEBAND_POWER` | R-only | read:`status.py` | exposed |
| 47 | `NOISE_DENSITY` | R-only | read:`status.py` | exposed |
| 48 | `DEMOD_TYPE` | R/W | write:`create_channel`,`set_demod_type` &#124; read:`status.py` | exposed — **stale range, see note below** |
| 49 | `OUTPUT_CHANNELS` | R/W | write:`set_output_channels` &#124; read:`status.py` | exposed |
| 50 | `INDEPENDENT_SIDEBAND` | R/W | write:`set_independent_sideband` &#124; read:`status.py` | exposed |
| 51 | `PLL_ENABLE` | R/W | write:`set_pll` &#124; read:`status.py` | exposed |
| 52 | `PLL_LOCK` | R-only | read:`status.py` | exposed |
| 53 | `PLL_SQUARE` | R/W | write:`set_pll` &#124; read:`status.py` | exposed |
| 54 | `PLL_PHASE` | R-only | read:`status.py` | exposed |
| 55 | `PLL_BW` | R/W | write:`set_pll` &#124; read:`status.py` | exposed |
| 56 | `ENVELOPE` | R/W | write:`set_envelope_detection` &#124; read:`status.py` | exposed |
| 57 | `SNR_SQUELCH` | R/W | write:`set_squelch` &#124; read:`status.py` | exposed |
| 58 | `PLL_SNR` | R-only | read:`status.py` | exposed |
| 59 | `FREQ_OFFSET` | R-only | read:`status.py` | exposed |
| 60 | `PEAK_DEVIATION` | R-only | read:`status.py` | exposed |
| 61 | `PL_TONE` | R-only | read:`status.py` | exposed |
| 62 | `AGC_ENABLE` | R/W | write:`create_channel`,`set_agc`,`tune` &#124; read:`status.py` | exposed |
| 63 | `HEADROOM` | R/W | write:`set_agc`,`set_headroom` &#124; read:`status.py` | exposed |
| 64 | `AGC_HANGTIME` | R/W | write:`set_agc`,`set_agc_hangtime` &#124; read:`status.py` | exposed |
| 65 | `AGC_RECOVERY_RATE` | R/W | write:`set_agc`,`set_agc_recovery_rate` &#124; read:`status.py` | exposed |
| 66 | `FM_SNR` | R-only | read:`status.py` | exposed |
| 67 | `AGC_THRESHOLD` | R/W | write:`set_agc`,`set_agc_threshold` &#124; read:`status.py` | exposed |
| 68 | `GAIN` | R/W | write:`create_channel`,`set_gain`,`tune` &#124; read:`status.py` | exposed |
| 69 | `OUTPUT_LEVEL` | R-only | read:`status.py` | exposed |
| 70 | `OUTPUT_SAMPLES` | R-only | read:`status.py` | exposed |
| 71 | `OPUS_BIT_RATE` | R/W | write:`set_opus_bitrate` &#124; read:`status.py` | exposed |
| 72 | `MAXDELAY` | R/W | write:`set_max_delay` &#124; read:`status.py` | exposed |
| 73 | `FILTER2_BLOCKSIZE` | R-only | read:`status.py` | exposed |
| 74 | `FILTER2_FIR_LENGTH` | R-only | read:`status.py` | exposed |
| 75 | `FILTER2_KAISER_BETA` | R/W | write:`set_filter2` &#124; read:`status.py` | exposed |
| 76 | `SPECTRUM_FFT_N` | R-only | read:`status.py` | exposed |
| 77 | `FILTER_DROPS` | R-only | read:`status.py` | exposed |
| 79 | `TP1` | R-only | read:`status.py` | exposed |
| 80 | `TP2` | R-only | read:`status.py` | exposed |
| 82 | `AD_BITS_PER_SAMPLE` | R-only | read:`status.py` | exposed |
| 83 | `SQUELCH_OPEN` | R/W | write:`set_squelch` &#124; read:`status.py` | exposed |
| 84 | `SQUELCH_CLOSE` | R/W | write:`set_squelch` &#124; read:`status.py` | exposed |
| 85 | `PRESET` | R/W | write:`create_channel`,`set_preset`,`tune` &#124; read:`status.py` | exposed |
| 86 | `DEEMPH_TC` | R-only | read:`status.py` | exposed |
| 87 | `DEEMPH_GAIN` | R-only | read:`status.py` | exposed |
| 89 | `PL_DEVIATION` | R-only | read:`status.py` | exposed |
| 90 | `THRESH_EXTEND` | R/W | write:`set_fm_threshold_extension` &#124; read:`status.py` | exposed |
| 91 | `SPECTRUM_SHAPE` | R/W | write:`set_spectrum` &#124; read:`status.py` | exposed |
| 93 | `RESOLUTION_BW` | R/W | write:`set_spectrum` &#124; read:`status.py` | exposed |
| 94 | `BIN_COUNT` | R/W | write:`set_spectrum` &#124; read:`status.py` | exposed |
| 95 | `CROSSOVER` | R/W | write:`set_spectrum` &#124; read:`status.py` | exposed |
| 96 | `BIN_DATA` | R-only | read:`status.py` | exposed |
| 97 | `RF_ATTEN` | R/W | write:`set_rf_attenuation`,`tune` &#124; read:`status.py` | exposed |
| 98 | `RF_GAIN` | R/W | write:`set_rf_gain`,`tune` &#124; read:`status.py` | exposed |
| 99 | `RF_AGC` | R-only | read:`status.py` | exposed |
| 100 | `FE_LOW_EDGE` | R-only | read:`status.py` | exposed |
| 101 | `FE_HIGH_EDGE` | R-only | read:`status.py` | exposed |
| 102 | `FE_ISREAL` | R-only | read:`status.py` | exposed |
| 104 | `AD_OVER` | R-only | read:`status.py` | exposed |
| 105 | `RTP_PT` | R-only | read:`status.py` | exposed |
| 106 | `STATUS_INTERVAL` | R/W | write:`set_status_interval` &#124; read:`status.py` | exposed |
| 107 | `OUTPUT_ENCODING` | R/W | write:`create_channel`,`set_channel_lifetime`,`set_output_encoding`,`tune` &#124; read:`status.py` | exposed |
| 108 | `SAMPLES_SINCE_OVER` | R-only | read:`status.py` | exposed |
| 109 | `PLL_WRAPS` | R-only | read:`status.py` | exposed |
| 110 | `RF_LEVEL_CAL` | R-only | read:`status.py` | exposed |
| 111 | `OPUS_DTX` | R/W | write:`set_opus_dtx` &#124; read:`status.py` | exposed |
| 112 | `OPUS_APPLICATION` | R/W | write:`set_opus_application` &#124; read:`status.py` | exposed |
| 113 | `OPUS_BANDWIDTH` | R-only | read:`status.py` | exposed |
| 114 | `OPUS_FEC` | R-only | read:`status.py` | exposed |
| 115 | `SPECTRUM_STEP` | R/W | write: **none** &#124; read:`status.py` | **gap** |
| 116 | `SPECTRUM_OVERLAP` | R/W | write: **none** &#124; read:`status.py` | **gap** |
| 117 | `LIFETIME` | R/W | write:`create_channel`,`set_channel_lifetime`,`tune` &#124; read:`status.py` | exposed |

`#` is the TLV's numeric value from `ka9q/types.py::StatusType` (gaps in
the sequence, e.g. 28–32, 78, 81, 88, 92, 103, are the 11 out-of-scope
TLVs listed above).

**Counts:**
- Writable TLVs (`decode_radio_commands` case labels): **51**
- Readable TLVs (`encode_radio_status` encode calls): **106**
- Union (this matrix): **107**
- Not-in-`types.py` (upstream-new): **0** — `src/status.h` is
  byte-identical between the `types.py` pin and `AUDIT_HEAD`
- Exposed (reachable end-to-end for every direction the TLV has): **101**
- Gap: **6** (5 write-side, 1 read-side; see below)

---

## Gaps

Each gap below is a TLV `radiod` honors (or emits) that `RadiodControl`
cannot reach through its public API. Per the no-bypass policy, these are
findings regardless of current sigmond-suite usage.

### Write-side (radiod accepts the command; `RadiodControl` has no way to send it)

- **`WINDOW_TYPE`** (spectrum analyzer FFT window selection). No
  `set_*` method encodes it. **Partial reachability exists outside
  `RadiodControl`'s own surface:** `ka9q/spectrum_stream.py`'s
  `SpectrumStream._send_spectrum_command()` encodes `WINDOW_TYPE`
  directly into a raw command buffer and sends it via
  `RadiodControl.send_command()` (the raw escape hatch), bypassing
  `set_spectrum()` entirely. A client using `RadiodControl` directly
  (not `SpectrumStream`) has no ergonomic way to choose a window
  function; it's stuck with whatever `radiod` defaults to.
- **`SPECTRUM_AVG`** (number of FFTs averaged into each spectrum
  response). Same situation as `WINDOW_TYPE`: reachable only via
  `SpectrumStream`'s hand-rolled buffer, not via `set_spectrum()` or any
  other `RadiodControl` method. A client cannot tune spectrum-averaging
  depth (noise floor vs. update rate trade-off) through the documented
  API.
- **`SPECTRUM_OVERLAP`** (FFT window overlap fraction, 0–1, when
  averaging). Same situation — `SpectrumStream`-only, not
  `RadiodControl`-native. No way to tune overlap for a smoother
  spectrum waterfall without reimplementing `SpectrumStream`'s raw
  packet construction.
- **`SPECTRUM_BASE`** (base dB level for 1-byte SPECT2 spectrum
  quantization). **Zero reachability anywhere in ka9q-python** — not
  even `SpectrumStream` writes this one. A SPECT2-mode client cannot
  set the quantization floor radiod uses to pack spectrum bins into
  bytes; it can only read back whatever `radiod` last chose
  (`ChannelStatus` decodes it) and has no way to request a different
  value.
- **`SPECTRUM_STEP`** (dB per quantization step for 1-byte SPECT2
  spectrum data). Same as `SPECTRUM_BASE` — zero reachability anywhere,
  including `SpectrumStream`. A SPECT2 client has no control over
  quantization resolution.

  (`set_spectrum()` at `ka9q/control.py:2772` covers `RESOLUTION_BW`,
  `BIN_COUNT`, `CROSSOVER`, `SPECTRUM_SHAPE` only. Fixing all five
  spectrum-mode gaps cleanly means adding `window_type`, `avg`,
  `overlap`, `base`, and `step` parameters to `set_spectrum()` — and,
  ideally, having `SpectrumStream` call the new `set_spectrum()` instead
  of hand-encoding its own buffer, so there is exactly one write path.)

### Read-side (radiod emits the field in every status packet; ka9q-python drops it)

- **`SETOPTS`** — `radiod` unconditionally encodes `chan->options`
  under the `SETOPTS` tag at the end of every status packet
  (`encode_int64(&bp,SETOPTS,chan->options);`, `src/radio_status.c:907`).
  `ka9q/status.py::decode_status_packet` has no branch for it and
  `ChannelStatus` has no `options` field, so the bitmask radiod is
  actually running with (as opposed to the bits a client last
  requested via `set_options()`) is silently dropped on every packet.
  A client that calls `set_options(set_bits=...)` has no way to
  read back confirmation that the option actually took, or to discover
  options set by another client / by preset / by radiod default.
  This is technically outside the brief's literal "gap = unmapped
  writable TLV" framing (it's the mirror case — a *readable* TLV
  ka9q-python fails to expose) but is included here because it's the
  same class of contract violation and was found by the same
  methodology.

### Bonus finding: a write that radiod silently ignores (not a gap, the inverse)

- **`LOCK`** — `ka9q/control.py::set_lock()` (line 3046) encodes and
  sends `StatusType.LOCK`, and `status.h` documents the field as
  settable ("Tuner is locked, will ignore retune commands (boolean)"),
  and `struct channel` in `src/radio.h` has a backing `bool lock` field
  — but `decode_radio_commands` at `AUDIT_HEAD` has **no `case LOCK:`**
  in its switch statement. The TLV falls through to `default: break;`
  and is silently discarded by radiod. (The only place `LOCK` is
  actually decoded in the whole ka9q-radio tree is `src/dump.c:328`, a
  diagnostic dump tool — not the command path.) So `set_lock()` gives
  the illusion of controlling the tuner lock, but at `AUDIT_HEAD` it is
  a no-op against real radiod. This isn't a `RadiodControl` gap — the
  method exists and sends the right bytes — it's an upstream omission
  ka9q-python cannot fix locally; the API should probably document (or
  the caller should be warned) that `set_lock()` currently has no
  effect until `radiod` grows a `case LOCK:` handler.

### `DEMOD_TYPE` note (context carried over from Task 2/3)

`DEMOD_TYPE` itself is fully exposed (`create_channel`, `set_demod_type`;
decoded in `status.py`), but `set_demod_type()`'s validation is stale:

```python
# ka9q/control.py:2852-2854
_validate_ssrc(ssrc)
if not (0 <= demod_type <= 4):
    raise ValidationError(f"Invalid demod_type: {demod_type} (must be 0-4)")
```

At `AUDIT_HEAD`, `enum demod_type` (`src/radio.h`) has grown a sixth
member, `IDLE_DEMOD = 5` ("placeholder that just processes commands"),
pushing `N_DEMOD` from `5` to `6` (introduced in commit `654fda5e`,
already documented in detail in Task 3's `contract.md`, which flags
`control.py:2854` explicitly as "Task 4/9 remediation scope"). This
hardcoded `<= 4` bound means `set_demod_type(ssrc, 5)` raises
`ValidationError` even against a radiod that fully supports
`IDLE_DEMOD` — a client cannot select the new demod type through
`RadiodControl` at all. This is not a TLV-mapping gap (`DEMOD_TYPE` the
tag is fully wired) but a **value-range gap**: the enum member exists on
the wire, `types.py`'s own `DemodType.N_DEMOD` is still `5` (stale, same
root cause — the `types.py` pin predates `IDLE_DEMOD`), and
`control.py`'s independent hardcoded bound compounds it. Both need to
move together: regenerate `types.py` (`scripts/sync_types.py --apply`)
to add `IDLE_DEMOD = 5` / bump `N_DEMOD` to `6`, then widen
`control.py:2854`'s bound to `<= 5` (or derive it from
`DemodType.N_DEMOD - 1` so it can't drift again).

---

## Step 5: Commit

```bash
git add docs/audit/2026-08-12-alignment/control-surface.md
git commit -m "audit: radiod control-surface completeness matrix"
```
