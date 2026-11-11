"""
Textual TUI for ka9q-radio, modelled on the ncurses ``control`` program.

Eight panels (Tuning, Frontend/GPSDO, Signal/Levels, Filter/FFT, Demod,
Input/Status, Output/RTP, Options) update live from radiod's status
multicast. Keybindings mirror ``control``'s one-letter commands and open
a modal prompt that calls the corresponding :class:`RadiodControl` setter.

Requires: ``pip install textual``.
"""
from __future__ import annotations

import threading
from queue import Queue, Empty
from typing import Callable, Optional

try:
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Grid, Horizontal, Vertical
    from textual.screen import ModalScreen, Screen
    from textual.widgets import (
        DataTable, Footer, Header, Input, Label, ListItem, ListView, Static,
    )
    from textual.reactive import reactive
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "Textual is required for the ka9q TUI. Install with: pip install textual"
    ) from exc

from .control import RadiodControl
from .discovery import (
    ChannelInfo, discover_channels, discover_radiod_services,
)
from .status import ChannelStatus
from .types import DemodType


# ---------------------------------------------------------------------------
# Status worker
# ---------------------------------------------------------------------------

class _StatusWorker(threading.Thread):
    """Background thread that runs :meth:`RadiodControl.listen_status`."""

    def __init__(self, control: RadiodControl, q: Queue, ssrcs: Optional[set]):
        super().__init__(daemon=True)
        self._control = control
        self._q = q
        self._ssrcs = ssrcs
        self._stop = threading.Event()

    def run(self) -> None:
        def cb(st: ChannelStatus) -> None:
            if self._stop.is_set():
                return
            self._q.put(st)

        while not self._stop.is_set():
            try:
                self._control.listen_status(cb, duration=1.0, ssrcs=self._ssrcs)
            except Exception:  # noqa: BLE001
                if self._stop.is_set():
                    return

    def stop(self) -> None:
        self._stop.set()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt_hz(v: Optional[float]) -> str:
    if v is None:
        return "—"
    if abs(v) >= 1e6:
        return f"{v/1e6:.6f} MHz"
    if abs(v) >= 1e3:
        return f"{v/1e3:.3f} kHz"
    return f"{v:+.2f} Hz"


def _yn(b: Optional[bool]) -> str:
    return "—" if b is None else ("yes" if b else "no")


# ---------------------------------------------------------------------------
# Panels
# ---------------------------------------------------------------------------

class Panel(Static):
    """Base panel. Subclasses override :meth:`render_status`."""

    DEFAULT_CSS = """
    Panel {
        border: round $accent;
        padding: 0 1;
        height: auto;
        min-height: 7;
    }
    Panel > .title { text-style: bold; color: $accent; }
    """

    title: str = ""

    def update_status(self, st: Optional[ChannelStatus]) -> None:
        if st is None:
            self.update(f"[b]{self.title}[/b]\n(waiting for status…)")
            return
        self.update(f"[b]{self.title}[/b]\n{self.render_status(st)}")

    def render_status(self, st: ChannelStatus) -> str:  # pragma: no cover
        return ""


class TuningPanel(Panel):
    title = "Tuning"

    def render_status(self, st: ChannelStatus) -> str:
        fe = st.frontend
        lines = [
            f"Carrier:   {_fmt_hz(st.frequency)}",
            f"First LO:  {_fmt_hz(st.first_lo)}  lock={_yn(fe.lock)}",
            f"Second LO: {_fmt_hz(st.second_lo)}",
            f"Shift:     {_fmt_hz(st.shift)}",
            f"Filter:    {_fmt_hz(st.low_edge)} .. {_fmt_hz(st.high_edge)}",
            f"FE filter: {_fmt_hz(fe.fe_low_edge)} .. {_fmt_hz(fe.fe_high_edge)}",
        ]
        return "\n".join(lines)


class FrontendPanel(Panel):
    title = "Frontend / GPSDO"

    def render_status(self, st: ChannelStatus) -> str:
        fe = st.frontend
        cal_ppm = f"{fe.calibrate_ppm:+.3f}" if fe.calibrate_ppm is not None else "—"
        ref_hz = f"{fe.gpsdo_reference_hz:.3f}" if fe.gpsdo_reference_hz is not None else "—"
        lines = [
            f"Desc:      {fe.description or '—'}",
            f"Rate:      {fe.input_samprate} Hz  bits={fe.ad_bits_per_sample}  "
            f"real={_yn(fe.isreal)}",
            f"Calibrate: {cal_ppm} ppm",
            f"Ref (10M): {ref_hz} Hz",
            f"AD over:   {fe.ad_over}  since: {fe.samples_since_over}",
            f"Gains:     LNA={fe.lna_gain} MIX={fe.mixer_gain} IF={fe.if_gain}",
            f"RF:        gain={fe.rf_gain} atten={fe.rf_atten} AGC={_yn(fe.rf_agc)}",
        ]
        return "\n".join(lines)


class SignalPanel(Panel):
    title = "Signal / Levels"

    def render_status(self, st: ChannelStatus) -> str:
        fe = st.frontend
        dbm = fe.input_power_dbm
        return "\n".join([
            f"IF power:     {fe.if_power} dBFS",
            f"RF level cal: {fe.rf_level_cal} dB",
            f"Input:        {dbm:.1f} dBm" if dbm is not None else "Input:        —",
            f"Baseband:     {st.baseband_power} dB",
            f"Noise dens:   {st.noise_density} dB/Hz",
            f"S/N0:         {st.snr_per_hz} dB-Hz",
            f"S/N:          {st.snr} dB  (BW={st.bandwidth} Hz)",
            f"Output lvl:   {st.output_level} dB",
        ])


class FilterPanel(Panel):
    title = "Filter / FFT"

    def render_status(self, st: ChannelStatus) -> str:
        lines = [
            f"β (Kaiser):  {st.kaiser_beta}",
            f"Blocksize:   {st.filter_blocksize}",
            f"FIR length:  {st.filter_fir_length}",
            f"Drops:       {st.filter_drops}",
            f"Noise BW:    {st.noise_bw}",
        ]
        f2 = st.filter2
        if f2.blocking:
            lines.append(
                f"Filter2:     blk={f2.blocking} size={f2.blocksize} "
                f"fir={f2.fir_length} β={f2.kaiser_beta}"
            )
        return "\n".join(lines)


class DemodPanel(Panel):
    title = "Demod"

    def render_status(self, st: ChannelStatus) -> str:
        head = f"Mode: {st.preset or '—'}  ({st.demod_name})"
        if st.demod_type == DemodType.FM_DEMOD:
            body = "\n".join([
                f"FM SNR:      {st.fm.fm_snr} dB",
                f"Peak dev:    {st.fm.peak_deviation} Hz",
                f"PL tone:     cfg={st.fm.pl_tone} meas={st.fm.pl_deviation}",
                f"De-emph:     tc={st.fm.deemph_tc}  gain={st.fm.deemph_gain}",
                f"Thr-extend:  {_yn(st.fm.threshold_extend)}",
            ])
        elif st.demod_type == DemodType.LINEAR_DEMOD:
            p = st.pll
            body = "\n".join([
                f"AGC: {_yn(st.agc_enable)}  gain={st.gain}  headroom={st.headroom}",
                f"  hang={st.agc_hangtime}s  recov={st.agc_recovery_rate} dB/s  "
                f"thresh={st.agc_threshold} dB",
                f"ISB: {_yn(st.independent_sideband)}   Env: {_yn(st.envelope)}",
                f"PLL: en={_yn(p.enable)} lock={_yn(p.lock)} sq={_yn(p.square)}",
                f"  BW={p.bw} Hz  Δf={p.freq_offset} Hz  SNR={p.snr} dB",
                f"  φ={p.phase} rad  wraps={p.wraps}",
            ])
        elif st.demod_type in (DemodType.SPECT_DEMOD, DemodType.SPECT2_DEMOD):
            sp = st.spectrum
            body = "\n".join([
                f"RBW: {sp.resolution_bw} Hz",
                f"Bins: {sp.bin_count}  crossover: {sp.crossover} Hz",
                f"FFT N: {sp.fft_n}  window: {sp.window_type}",
                f"Overlap: {sp.overlap}  avg: {sp.avg}  shape: {sp.shape}",
            ])
        else:
            body = "(no demod-specific fields)"
        return head + "\n" + body


class InputStatusPanel(Panel):
    title = "Input / Status"

    def render_status(self, st: ChannelStatus) -> str:
        fe = st.frontend
        return "\n".join([
            f"GPS time:    {st.gps_time}",
            f"Cmd count:   {st.cmd_cnt}",
            f"Rate:        {fe.input_samprate} Hz",
            f"Samples in:  {fe.input_samples}",
            f"Overranges:  {fe.ad_over}  since: {fe.samples_since_over}",
            f"Status dest: {st.status_dest_socket}",
            f"Status int:  {st.status_interval}",
        ])


class OutputPanel(Panel):
    title = "Output / RTP"

    def render_status(self, st: ChannelStatus) -> str:
        lines = [
            f"SSRC:    {st.ssrc}",
            f"Rate:    {st.output_samprate} Hz  channels={st.output_channels}",
            f"Enc:     {st.encoding_name}  TTL={st.output_ttl}",
            f"Dest:    {st.output_data_dest_socket}",
            f"Samples: {st.output_samples}",
            f"Pkts:    data={st.output_data_packets} meta={st.output_metadata_packets}",
            f"Errors:  {st.output_errors}  maxdelay={st.maxdelay}",
        ]
        if st.opus.bit_rate:
            lines.append(
                f"Opus:    {st.opus.bit_rate} bps dtx={_yn(st.opus.dtx)} "
                f"app={st.opus.application} bw={st.opus.bandwidth} fec={st.opus.fec}"
            )
        return "\n".join(lines)


class OptionsPanel(Panel):
    title = "Options / Squelch"

    def render_status(self, st: ChannelStatus) -> str:
        return "\n".join([
            f"Lock:        {_yn(st.lock)}",
            f"SNR sq:      {_yn(st.snr_squelch_enable)}",
            f"Sq open:     {st.squelch_open} dB",
            f"Sq close:    {st.squelch_close} dB",
            f"ISB:         {_yn(st.independent_sideband)}",
            f"Envelope:    {_yn(st.envelope)}",
            f"AGC:         {_yn(st.agc_enable)}",
            f"Thr-ext(FM): {_yn(st.fm.threshold_extend)}",
        ])


# ---------------------------------------------------------------------------
# Modal prompt
# ---------------------------------------------------------------------------

class PromptModal(ModalScreen[Optional[str]]):
    """Minimal single-line input modal."""

    DEFAULT_CSS = """
    PromptModal { align: center middle; }
    PromptModal > Vertical {
        background: $panel;
        border: thick $accent;
        padding: 1 2;
        width: 60;
        height: auto;
    }
    """

    def __init__(self, prompt: str, initial: str = "") -> None:
        super().__init__()
        self._prompt = prompt
        self._initial = initial

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(self._prompt)
            yield Input(value=self._initial, id="modal-input")

    def on_mount(self) -> None:
        self.query_one("#modal-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)

    def key_escape(self) -> None:
        self.dismiss(None)


# ---------------------------------------------------------------------------
# Picker screens
# ---------------------------------------------------------------------------

class RadiodPickerScreen(Screen[Optional[str]]):
    """Discovers radiod services via mDNS and lets the user pick one."""

    DEFAULT_CSS = """
    RadiodPickerScreen { align: center middle; }
    RadiodPickerScreen > Vertical {
        background: $panel; border: thick $accent;
        padding: 1 2; width: 80; height: 20;
    }
    RadiodPickerScreen ListView { height: 1fr; }
    """
    BINDINGS = [
        Binding("r", "rescan", "Rescan", priority=True),
        Binding("escape", "cancel", "Cancel", priority=True),
        Binding("q", "cancel", "Quit", priority=True),
        Binding("ctrl+c", "force_quit", "Quit", priority=True, show=False),
    ]

    def action_force_quit(self) -> None:
        self.app.exit()

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Select a radiod instance  (Enter=pick  r=rescan  q/Esc=quit)")
            yield Static("Discovering…", id="rp-status")
            yield ListView(id="rp-list")

    def on_mount(self) -> None:
        self.run_worker(self._scan, thread=True, exclusive=True)

    def _scan(self) -> None:
        try:
            services = discover_radiod_services(timeout=5.0)
        except Exception as exc:  # noqa: BLE001
            self.app.call_from_thread(self._show_error, str(exc))
            return
        self.app.call_from_thread(self._populate, services)

    def _show_error(self, msg: str) -> None:
        self.query_one("#rp-status", Static).update(f"Error: {msg}")

    def _populate(self, services: list) -> None:
        lv = self.query_one("#rp-list", ListView)
        lv.clear()
        if not services:
            self.query_one("#rp-status", Static).update(
                "No radiod services found via mDNS. Press r to rescan or Esc to quit."
            )
            return
        self.query_one("#rp-status", Static).update(
            f"Found {len(services)} service(s)."
        )
        self._services = services
        for svc in services:
            lv.append(ListItem(Label(f"{svc['name']:40s}  {svc['address']}")))
        if len(services) == 1:
            self.dismiss(services[0]["address"])
            return
        lv.index = 0
        self.call_after_refresh(lv.focus)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        idx = self.query_one("#rp-list", ListView).index
        if idx is None or not getattr(self, "_services", None):
            return
        self.dismiss(self._services[idx]["address"])

    def action_rescan(self) -> None:
        self.query_one("#rp-status", Static).update("Rescanning…")
        self.query_one("#rp-list", ListView).clear()
        self.run_worker(self._scan, thread=True, exclusive=True)

    def action_cancel(self) -> None:
        self.dismiss(None)


class SsrcPickerScreen(Screen[Optional[int]]):
    """Lists SSRCs on the chosen radiod and lets the user pick one (or all)."""

    DEFAULT_CSS = """
    SsrcPickerScreen { align: center middle; }
    SsrcPickerScreen > Vertical {
        background: $panel; border: thick $accent;
        padding: 1 2; width: 100; height: 28;
    }
    SsrcPickerScreen DataTable { height: 1fr; }
    """
    BINDINGS = [
        Binding("r", "rescan", "Rescan", priority=True),
        Binding("a", "all_ssrcs", "All", priority=True),
        Binding("escape", "cancel", "Back", priority=True),
        Binding("q", "force_quit", "Quit", priority=True),
        Binding("ctrl+c", "force_quit", "Quit", priority=True, show=False),
    ]

    def action_force_quit(self) -> None:
        self.app.exit()

    def __init__(self, host: str, interface: Optional[str] = None) -> None:
        super().__init__()
        self._host = host
        self._interface = interface
        self._ssrcs: list[int] = []

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(
                f"SSRCs on {self._host}  "
                "(Enter=pick  a=all  r=rescan  Esc=back)"
            )
            yield Static("Discovering channels…", id="sp-status")
            t = DataTable(id="sp-table", cursor_type="row")
            t.add_columns("SSRC", "Freq (MHz)", "Preset", "Rate", "SNR", "Dest")
            yield t

    def on_mount(self) -> None:
        self.run_worker(self._scan, thread=True, exclusive=True)

    def _scan(self) -> None:
        try:
            channels = discover_channels(
                self._host, listen_duration=2.0, interface=self._interface
            )
        except Exception as exc:  # noqa: BLE001
            self.app.call_from_thread(self._show_error, str(exc))
            return
        self.app.call_from_thread(self._populate, channels)

    def _show_error(self, msg: str) -> None:
        self.query_one("#sp-status", Static).update(f"Error: {msg}")

    def _populate(self, channels: dict) -> None:
        table = self.query_one("#sp-table", DataTable)
        table.clear()
        self._ssrcs = []
        if not channels:
            self.query_one("#sp-status", Static).update(
                "No SSRCs found. Press r to rescan, a for all, or Esc to go back."
            )
            return
        self.query_one("#sp-status", Static).update(
            f"Found {len(channels)} channel(s). Enter to pick, a for all."
        )
        for ssrc in sorted(channels):
            ch: ChannelInfo = channels[ssrc]
            snr = "—" if ch.snr == float("-inf") else f"{ch.snr:.1f}"
            table.add_row(
                str(ssrc),
                f"{ch.frequency / 1e6:.6f}",
                ch.preset,
                f"{ch.sample_rate}",
                snr,
                f"{ch.multicast_address}:{ch.port}",
            )
            self._ssrcs.append(ssrc)
        self.call_after_refresh(table.focus)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        idx = event.cursor_row
        if 0 <= idx < len(self._ssrcs):
            self.dismiss(self._ssrcs[idx])

    def action_all_ssrcs(self) -> None:
        self.dismiss(0)  # 0 sentinel = "all SSRCs" (radiod never uses 0)

    def action_rescan(self) -> None:
        self.query_one("#sp-status", Static).update("Rescanning…")
        self.query_one("#sp-table", DataTable).clear()
        self.run_worker(self._scan, thread=True, exclusive=True)

    def action_cancel(self) -> None:
        self.dismiss(None)


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

class Ka9qApp(App):
    """Main TUI app."""

    CSS = """
    Screen { background: $surface; }
    #grid {
        grid-size: 3 3;
        grid-gutter: 1 1;
        padding: 1 1;
    }
    #header-line {
        height: 1;
        background: $boost;
        color: $text;
        padding: 0 1;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("ctrl+c", "quit", "Quit", priority=True, show=False),
        Binding("question_mark,h", "help", "Help"),
        Binding("f", "prompt('frequency', 'Carrier frequency (Hz)')", "Freq"),
        Binding("p", "prompt('preset', 'Preset / mode')", "Preset"),
        Binding("S", "prompt('sample-rate', 'Sample rate (Hz)')", "SR"),
        Binding("s", "prompt('squelch-open', 'Squelch open (dB)')", "Squelch"),
        Binding("G", "prompt('rf-gain', 'RF gain (dB)')", "RFgain"),
        Binding("A", "prompt('rf-atten', 'RF atten (dB)')", "RFatten"),
        Binding("g", "prompt('gain', 'Linear gain (dB)')", "Gain"),
        Binding("H", "prompt('headroom', 'Headroom (dB)')", "Head"),
        Binding("L", "prompt('agc-threshold', 'AGC threshold (dB)')", "AGCthr"),
        Binding("R", "prompt('agc-recovery', 'AGC recovery (dB/s)')", "AGCrec"),
        Binding("T", "prompt('agc-hangtime', 'AGC hang time (s)')", "Hang"),
        Binding("P", "prompt('pll-bw', 'PLL bandwidth (Hz)')", "PLLbw"),
        Binding("K", "prompt('kaiser-beta', 'Kaiser β')", "β"),
        Binding("e", "prompt('encoding', 'Encoding (e.g. S16LE, F32LE, OPUS)')", "Enc"),
        Binding("b", "prompt('opus-bitrate', 'Opus bitrate (bps)')", "Opus"),
        Binding("l", "toggle_lock", "Lock"),
        Binding("i", "toggle_isb", "ISB"),
        Binding("v", "toggle_envelope", "Env"),
        Binding("x", "toggle_threshold_extend", "ThExt"),
        Binding("t", "prompt('pl-tone', 'PL tone (Hz, 0 off)')", "PLtone"),
        Binding("D", "prompt('demod-type', 'Demod type (linear/fm/wfm/spect)')", "Demod"),
        Binding("M", "repick", "Pick radiod/SSRC"),
    ]

    status: reactive[Optional[ChannelStatus]] = reactive(None)

    def __init__(self, host: Optional[str] = None, ssrc: Optional[int] = None,
                 interface: Optional[str] = None) -> None:
        super().__init__()
        self._host = host
        self._ssrc = ssrc
        self._interface = interface
        self._control: Optional[RadiodControl] = None
        self._queue: Queue = Queue()
        self._worker: Optional[_StatusWorker] = None

    # --- lifecycle -----------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static(f"radiod: {self._host or '(picking…)'}   "
                     f"SSRC: {self._ssrc or 'any'}",
                     id="header-line")
        with Grid(id="grid"):
            yield TuningPanel()
            yield FrontendPanel()
            yield SignalPanel()
            yield FilterPanel()
            yield DemodPanel()
            yield OptionsPanel()
            yield InputStatusPanel()
            yield OutputPanel()
            yield Static("", id="pad")  # 9th cell
        yield Footer()

    def on_mount(self) -> None:
        self.title = "ka9q TUI"
        self.sub_title = self._host or ""
        if self._host is None:
            self.push_screen(RadiodPickerScreen(), self._on_host_picked)
        else:
            self._start_session()

    def _on_host_picked(self, host: Optional[str]) -> None:
        if not host:
            self.exit()
            return
        self._host = host
        self.sub_title = host
        self.push_screen(
            SsrcPickerScreen(host, interface=self._interface),
            self._on_ssrc_picked,
        )

    def _on_ssrc_picked(self, ssrc: Optional[int]) -> None:
        if ssrc is None:
            # Back out to host picker
            self._host = None
            self.sub_title = ""
            self.push_screen(RadiodPickerScreen(), self._on_host_picked)
            return
        self._ssrc = ssrc if ssrc != 0 else None
        self._start_session()

    def _start_session(self) -> None:
        self.query_one("#header-line", Static).update(
            f"radiod: {self._host}   SSRC: {self._ssrc or 'any'}"
        )
        if self._control is None:
            self._control = RadiodControl(self._host, interface=self._interface)
        ssrcs = {self._ssrc} if self._ssrc else None
        if self._worker is not None:
            self._worker.stop()
        self._worker = _StatusWorker(self._control, self._queue, ssrcs)
        self._worker.start()
        if not getattr(self, "_intervals_started", False):
            self.set_interval(0.2, self._drain_queue)
            self.set_interval(1.0, self._tick_poll)
            self._intervals_started = True
        if self._ssrc:
            self.run_worker(self._initial_poll, thread=True, exclusive=False)

    def _initial_poll(self) -> None:
        if self._control is None:
            return
        try:
            st = self._control.poll_status(self._ssrc, timeout=2.0)
            self._queue.put(st)
        except Exception:
            pass

    def _tick_poll(self) -> None:
        if not self._ssrc or self._control is None:
            return
        self.run_worker(self._poll_once, thread=True, exclusive=False,
                        group="poll")

    def _poll_once(self) -> None:
        if self._control is None:
            return
        try:
            st = self._control.poll_status(self._ssrc, timeout=0.8)
            self._queue.put(st)
        except Exception:
            pass

    def action_repick(self) -> None:
        if self._worker:
            self._worker.stop()
            self._worker = None
        if self._control is not None:
            try:
                self._control.close()
            except Exception:
                pass
            self._control = None
        self._host = None
        self._ssrc = None
        self.status = None
        self.push_screen(RadiodPickerScreen(), self._on_host_picked)

    def on_unmount(self) -> None:
        if self._worker:
            self._worker.stop()
        if self._control is not None:
            try:
                self._control.close()
            except Exception:
                pass

    # --- status plumbing ----------------------------------------------

    def _drain_queue(self) -> None:
        latest: Optional[ChannelStatus] = None
        try:
            while True:
                latest = self._queue.get_nowait()
        except Empty:
            pass
        if latest is None:
            return
        if self._ssrc is None:
            self._ssrc = latest.ssrc
            self.query_one("#header-line", Static).update(
                f"radiod: {self._host}   SSRC: {self._ssrc}"
            )
        self.status = latest

    def watch_status(self, st: Optional[ChannelStatus]) -> None:
        for panel in self.query(Panel):
            panel.update_status(st)

    # --- actions -------------------------------------------------------

    def action_help(self) -> None:
        msg = (
            "Keys: f=freq  p=preset  S=samprate  s=squelch  G=RFgain  A=RFatten\n"
            "      g=gain  H=headroom  L=AGCthr  R=AGCrec  T=AGChang  P=PLLbw\n"
            "      K=β  e=encoding  b=Opus-br  l=lock  i=ISB  v=env  x=thExt\n"
            "      t=PLtone  D=demod  q=quit   ?=this help"
        )
        self.push_screen(PromptModal(msg, initial=""))

    def action_prompt(self, param: str, prompt: str) -> None:
        if not self._ssrc:
            self.bell()
            return

        def _after(value: Optional[str]) -> None:
            if value is None or value == "":
                return
            from .cli import SET_VERBS
            fn = SET_VERBS.get(param)
            if fn is None:
                return
            try:
                fn(self._control, self._ssrc, value)
            except Exception as exc:  # noqa: BLE001
                self.query_one("#header-line", Static).update(
                    f"radiod: {self._host}   SSRC: {self._ssrc}   ERR: {exc}"
                )

        self.push_screen(PromptModal(prompt), _after)

    def _toggle(self, param_name: str, current: Optional[bool]) -> None:
        if not self._ssrc:
            self.bell()
            return
        from .cli import SET_VERBS
        fn = SET_VERBS[param_name]
        new = "false" if current else "true"
        try:
            fn(self._control, self._ssrc, new)
        except Exception:
            pass

    def action_toggle_lock(self) -> None:
        self._toggle("lock", self.status.lock if self.status else None)

    def action_toggle_isb(self) -> None:
        self._toggle("isb", self.status.independent_sideband if self.status else None)

    def action_toggle_envelope(self) -> None:
        self._toggle("envelope", self.status.envelope if self.status else None)

    def action_toggle_threshold_extend(self) -> None:
        cur = self.status.fm.threshold_extend if self.status else None
        self._toggle("threshold-extend", cur)


def run_tui(host: str, ssrc: Optional[int] = None,
            interface: Optional[str] = None) -> int:
    Ka9qApp(host, ssrc=ssrc, interface=interface).run()
    return 0
