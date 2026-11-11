"""
``ka9q`` command-line tool.

Subcommands mirror the things the Textual TUI does, so the CLI and TUI
share one vocabulary:

    ka9q list    HOST                              List channels (via discovery)
    ka9q query   HOST --ssrc N [--field path] [--json] [--watch]
    ka9q set     HOST --ssrc N PARAM VALUE         Change a parameter
    ka9q tui     HOST                              Launch the Textual TUI

Every ``set`` verb maps to a :class:`RadiodControl` setter, and every
``query --field`` path is one of the dotted paths produced by
:meth:`ChannelStatus.field_names`.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import is_dataclass, asdict
from typing import Any, Optional

from .control import RadiodControl
from .discovery import discover_channels
from .status import ChannelStatus
from .types import DemodType, Encoding, WindowType


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------

def _fmt_hz(v: Optional[float]) -> str:
    if v is None:
        return "—"
    if abs(v) >= 1e6:
        return f"{v/1e6:.6f} MHz"
    if abs(v) >= 1e3:
        return f"{v/1e3:.3f} kHz"
    return f"{v:+.1f} Hz"


def _render_status_text(st: ChannelStatus) -> str:
    fe = st.frontend
    L = []
    L.append(f"SSRC:         {st.ssrc}")
    L.append(f"Description:  {st.description or '—'}")
    L.append(f"Preset / Mode: {st.preset or '—'} ({st.demod_name})")
    L.append("")
    L.append("[Tuning]")
    L.append(f"  Carrier:     {_fmt_hz(st.frequency)}")
    L.append(f"  First LO:    {_fmt_hz(st.first_lo)}   lock={fe.lock}")
    L.append(f"  Second LO:   {_fmt_hz(st.second_lo)}")
    L.append(f"  Shift:       {_fmt_hz(st.shift)}")
    L.append(f"  Filter:      {_fmt_hz(st.low_edge)} .. {_fmt_hz(st.high_edge)}  (β={st.kaiser_beta})")
    L.append(f"  FE filter:   {_fmt_hz(fe.fe_low_edge)} .. {_fmt_hz(fe.fe_high_edge)}")
    L.append("")
    L.append("[Frontend / GPSDO]")
    L.append(f"  Input rate:  {fe.input_samprate} Hz  ({fe.ad_bits_per_sample} bits, real={fe.isreal})")
    if fe.calibrate is not None:
        L.append(f"  Calibrate:   {fe.calibrate:+.3e}  ({fe.calibrate_ppm:+.3f} ppm)")
        L.append(f"  Implied 10 MHz ref: {fe.gpsdo_reference_hz:.3f} Hz")
    L.append(f"  Overranges:  {fe.ad_over}  samples since: {fe.samples_since_over}")
    L.append(f"  Gains:       LNA={fe.lna_gain} MIX={fe.mixer_gain} IF={fe.if_gain} "
             f"RFgain={fe.rf_gain} RFatten={fe.rf_atten} RFAGC={fe.rf_agc}")
    L.append(f"  IF power:    {fe.if_power} dBFS  cal={fe.rf_level_cal} dB  "
             f"→ input {fe.input_power_dbm} dBm" if fe.if_power is not None else
             "  IF power:    —")
    L.append("")
    L.append("[Signal / Levels]")
    L.append(f"  Baseband:    {st.baseband_power} dB   Noise density: {st.noise_density} dB/Hz")
    L.append(f"  Output lvl:  {st.output_level} dB   S/N0: {st.snr_per_hz} dB-Hz   "
             f"S/N: {st.snr} dB  BW={st.bandwidth} Hz")
    L.append("")
    L.append("[Filter / FFT]")
    L.append(f"  Blocksize:   {st.filter_blocksize}  FIR len: {st.filter_fir_length}  "
             f"drops: {st.filter_drops}  noise_bw: {st.noise_bw}")
    if st.filter2.blocking:
        L.append(f"  Filter2:     blk={st.filter2.blocking} size={st.filter2.blocksize} "
                 f"fir={st.filter2.fir_length} β={st.filter2.kaiser_beta}")
    L.append("")
    if st.demod_type == DemodType.FM_DEMOD:
        L.append("[FM]")
        L.append(f"  SNR: {st.fm.fm_snr}  peak dev: {st.fm.peak_deviation} Hz  "
                 f"PL tone: {st.fm.pl_tone}/{st.fm.pl_deviation}")
        L.append(f"  De-emph TC: {st.fm.deemph_tc}  gain: {st.fm.deemph_gain}  "
                 f"thr-ext: {st.fm.threshold_extend}")
    elif st.demod_type == DemodType.LINEAR_DEMOD:
        L.append("[Linear]")
        L.append(f"  AGC: {st.agc_enable}  gain: {st.gain}  headroom: {st.headroom}  "
                 f"hang: {st.agc_hangtime}s  recov: {st.agc_recovery_rate} dB/s  "
                 f"thresh: {st.agc_threshold} dB")
        L.append(f"  ISB: {st.independent_sideband}   Envelope: {st.envelope}")
        p = st.pll
        L.append(f"  PLL: enable={p.enable} lock={p.lock} sq={p.square}  "
                 f"BW={p.bw} Hz  Δf={p.freq_offset} Hz  φ={p.phase}  "
                 f"SNR={p.snr} dB  wraps={p.wraps}")
    elif st.demod_type in (DemodType.SPECT_DEMOD, DemodType.SPECT2_DEMOD):
        sp = st.spectrum
        L.append("[Spectrum]")
        L.append(f"  RBW: {sp.resolution_bw} Hz  bins: {sp.bin_count}  crossover: {sp.crossover} Hz")
        L.append(f"  FFT N: {sp.fft_n}  window: {sp.window_type}  overlap: {sp.overlap}  "
                 f"avg: {sp.avg}  shape: {sp.shape}")
    L.append("")
    L.append("[Squelch / Options]")
    L.append(f"  SNR-sq: {st.snr_squelch_enable}   open: {st.squelch_open}   close: {st.squelch_close}")
    L.append(f"  Lock: {st.lock}")
    L.append("")
    L.append("[Output]")
    L.append(f"  rate={st.output_samprate} ch={st.output_channels} enc={st.encoding_name} "
             f"ttl={st.output_ttl}")
    L.append(f"  dest={st.output_data_dest_socket}")
    L.append(f"  samples={st.output_samples} pkts={st.output_data_packets} "
             f"meta={st.output_metadata_packets} errs={st.output_errors}")
    if st.opus.bit_rate:
        L.append(f"  Opus: {st.opus.bit_rate} bps  dtx={st.opus.dtx}  app={st.opus.application} "
                 f"bw={st.opus.bandwidth} fec={st.opus.fec}")
    if st.tp1 is not None or st.tp2 is not None:
        L.append(f"  TP1={st.tp1}  TP2={st.tp2}")
    return "\n".join(L)


def _json_default(o: Any) -> Any:
    if is_dataclass(o):
        return asdict(o)
    raise TypeError(f"not json-serializable: {type(o).__name__}")


# ---------------------------------------------------------------------------
# ``set`` verb dispatch
# ---------------------------------------------------------------------------

def _coerce_bool(v: str) -> bool:
    return v.lower() in ("1", "true", "yes", "on", "y", "t")


def _coerce_encoding(v: str) -> int:
    try:
        return int(v)
    except ValueError:
        return getattr(Encoding, v.upper())


def _coerce_demod(v: str) -> int:
    try:
        return int(v)
    except ValueError:
        return getattr(DemodType, v.upper() if v.upper().endswith("_DEMOD") else v.upper() + "_DEMOD")


def _coerce_window(v: str) -> int:
    try:
        return int(v)
    except ValueError:
        return getattr(WindowType, v.upper() if v.upper().endswith("_WINDOW") else v.upper() + "_WINDOW")


# (param-name → callable(control, ssrc, raw_value))
SET_VERBS = {
    "frequency":       lambda c, s, v: c.set_frequency(s, float(v)),
    "preset":          lambda c, s, v: c.set_preset(s, v),
    "mode":            lambda c, s, v: c.set_preset(s, v),
    "sample-rate":     lambda c, s, v: c.set_sample_rate(s, int(v)),
    "samprate":        lambda c, s, v: c.set_sample_rate(s, int(v)),
    "low-edge":        lambda c, s, v: c.set_filter(s, low_edge=float(v)),
    "high-edge":       lambda c, s, v: c.set_filter(s, high_edge=float(v)),
    "kaiser-beta":     lambda c, s, v: c.set_kaiser_beta(s, float(v)),
    "shift":           lambda c, s, v: c.set_shift_frequency(s, float(v)),
    "gain":            lambda c, s, v: c.set_gain(s, float(v)),
    "output-level":    lambda c, s, v: c.set_output_level(s, float(v)),
    "headroom":        lambda c, s, v: c.set_headroom(s, float(v)),
    "agc":             lambda c, s, v: c.set_agc(s, _coerce_bool(v)),
    "agc-hangtime":    lambda c, s, v: c.set_agc_hangtime(s, float(v)),
    "agc-recovery":    lambda c, s, v: c.set_agc_recovery_rate(s, float(v)),
    "agc-threshold":   lambda c, s, v: c.set_agc_threshold(s, float(v)),
    "rf-gain":         lambda c, s, v: c.set_rf_gain(s, float(v)),
    "rf-atten":        lambda c, s, v: c.set_rf_attenuation(s, float(v)),
    "squelch-open":    lambda c, s, v: c.set_squelch(s, enable=True, open_snr_db=float(v)),
    "squelch-close":   lambda c, s, v: c.set_squelch(s, enable=True, close_snr_db=float(v)),
    "snr-squelch":     lambda c, s, v: c.set_squelch(s, enable=_coerce_bool(v)),
    "pll":             lambda c, s, v: c.set_pll(s, enable=_coerce_bool(v)),
    "pll-bw":          lambda c, s, v: c.set_pll(s, enable=True, bandwidth_hz=float(v)),
    "pll-square":      lambda c, s, v: c.set_pll(s, enable=True, square=_coerce_bool(v)),
    "isb":             lambda c, s, v: c.set_independent_sideband(s, _coerce_bool(v)),
    "envelope":        lambda c, s, v: c.set_envelope_detection(s, _coerce_bool(v)),
    "channels":        lambda c, s, v: c.set_output_channels(s, int(v)),
    "encoding":        lambda c, s, v: c.set_output_encoding(s, _coerce_encoding(v)),
    "demod-type":      lambda c, s, v: c.set_demod_type(s, _coerce_demod(v)),
    "pl-tone":         lambda c, s, v: c.set_pl_tone(s, float(v)),
    "threshold-extend":lambda c, s, v: c.set_fm_threshold_extension(s, _coerce_bool(v)),
    "lock":            lambda c, s, v: c.set_lock(s, _coerce_bool(v)),
    "description":     lambda c, s, v: c.set_description(s, v),
    "first-lo":        lambda c, s, v: c.set_first_lo(s, float(v)),
    "status-interval": lambda c, s, v: c.set_status_interval(s, int(v)),
    "max-delay":       lambda c, s, v: c.set_max_delay(s, int(v)),
    "opus-bitrate":    lambda c, s, v: c.set_opus_bitrate(s, int(v)),
    "opus-dtx":        lambda c, s, v: c.set_opus_dtx(s, _coerce_bool(v)),
    "opus-application":lambda c, s, v: c.set_opus_application(s, int(v)),
    "opus-bandwidth":  lambda c, s, v: c.set_opus_bandwidth(s, int(v)),
    "opus-fec":        lambda c, s, v: c.set_opus_fec(s, int(v)),
    "window":          lambda c, s, v: c.set_spectrum(s, window_type=_coerce_window(v)),
    "destination":     lambda c, s, v: c.set_destination(s, *_parse_addr(v)),
}


def _parse_addr(v: str):
    if ":" in v:
        a, p = v.rsplit(":", 1)
        return a, int(p)
    return v, 5004


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

def cmd_list(args: argparse.Namespace) -> int:
    channels_map = discover_channels(args.host, listen_duration=args.timeout)
    channels = list(channels_map.values())
    if args.json:
        print(json.dumps([asdict(c) if is_dataclass(c) else c.__dict__ for c in channels],
                         default=_json_default, indent=2))
        return 0
    if not channels:
        print("No channels discovered.", file=sys.stderr)
        return 1
    print(f"{'SSRC':>10}  {'Frequency':>14}  {'Preset':<8}  Dest")
    for c in channels:
        freq = getattr(c, "frequency", None) or getattr(c, "frequency_hz", None)
        ssrc = getattr(c, "ssrc", "?")
        preset = getattr(c, "preset", "") or ""
        dest = getattr(c, "data_dest", "") or getattr(c, "destination", "") or ""
        print(f"{ssrc:>10}  {(freq or 0)/1e6:>13.6f}M  {preset:<8}  {dest}")
    return 0


def cmd_query(args: argparse.Namespace) -> int:
    with RadiodControl(args.host) as control:
        def show(st: ChannelStatus):
            if args.field:
                v = st.get_field(args.field)
                if args.json:
                    print(json.dumps({args.field: v}, default=_json_default))
                else:
                    print(v if v is not None else "")
            elif args.json:
                print(json.dumps(st.to_dict(), default=_json_default, indent=2))
            else:
                print(_render_status_text(st))
                print()

        if args.watch:
            # Passive listener — radiod multicasts periodically.
            ssrcs = {args.ssrc} if args.ssrc else None
            try:
                control.listen_status(show, ssrcs=ssrcs)
            except KeyboardInterrupt:
                return 0
        else:
            if args.ssrc is None:
                print("--ssrc required (or use --watch without --ssrc to see all)", file=sys.stderr)
                return 2
            st = control.poll_status(args.ssrc, timeout=args.timeout)
            show(st)
    return 0


def cmd_set(args: argparse.Namespace) -> int:
    verb = args.param.lower()
    if verb not in SET_VERBS:
        print(f"Unknown parameter '{verb}'. Known: {', '.join(sorted(SET_VERBS))}", file=sys.stderr)
        return 2
    with RadiodControl(args.host) as control:
        SET_VERBS[verb](control, args.ssrc, args.value)
    return 0


def cmd_tui(args: argparse.Namespace) -> int:
    try:
        from .tui import run_tui
    except ImportError as exc:
        print(f"TUI unavailable: {exc}\nInstall with: pip install ka9q-python[tui]", file=sys.stderr)
        return 2
    return run_tui(args.host, ssrc=args.ssrc, interface=args.interface)


# ---------------------------------------------------------------------------
# Top-level parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ka9q",
        description="Control and monitor ka9q-radio channels.",
    )
    p.add_argument("--interface", help="Network interface for multicast (optional)")
    sub = p.add_subparsers(dest="cmd", required=True)

    pl = sub.add_parser("list", help="Discover channels on HOST")
    pl.add_argument("host")
    pl.add_argument("--timeout", type=float, default=3.0)
    pl.add_argument("--json", action="store_true")
    pl.set_defaults(func=cmd_list)

    pq = sub.add_parser("query", help="Query a channel's full status")
    pq.add_argument("host")
    pq.add_argument("--ssrc", type=int)
    pq.add_argument("--field", help="Dotted path, e.g. 'frontend.calibrate' or 'pll.lock'")
    pq.add_argument("--json", action="store_true")
    pq.add_argument("--watch", action="store_true", help="Stream passive status updates")
    pq.add_argument("--timeout", type=float, default=2.0)
    pq.set_defaults(func=cmd_query)

    ps = sub.add_parser("set", help="Set a channel parameter")
    ps.add_argument("host")
    ps.add_argument("--ssrc", type=int, required=True)
    ps.add_argument("param", help=f"One of: {', '.join(sorted(SET_VERBS))}")
    ps.add_argument("value")
    ps.set_defaults(func=cmd_set)

    pt = sub.add_parser("tui", help="Launch the Textual TUI")
    pt.add_argument("host", nargs="?",
                    help="radiod host (omit to pick from mDNS discovery)")
    pt.add_argument("--ssrc", type=int, help="Focus a specific SSRC at startup")
    pt.set_defaults(func=cmd_tui)

    return p


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
