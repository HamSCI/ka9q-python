"""PacketResequencer accounting: counters, gap offsets, flush, long gaps.

Diagnosis of 2026-11-22 (ka9q-python 3.27.0, resequencer.py at 95098cf):

  A. A lost-packet recovery counted its output twice in cumulative_samples
     and stats.samples_output (once in _handle_lost_packet, again in
     _try_output).
  B. GapEvent.position_samples carried cumulative_samples at detection, so it
     inherited A's excess and ignored samples emitted earlier in the same call.
  flush() sorted by raw sequence number (wrong across 65535 -> 0), never
     counted its fills in samples_filled, and never set last_chunk_rtp_start.
  MAX_GAP_SAMPLES truncated a long gap's fill, so a client labelling samples
     first_rtp_timestamp + count slipped by the truncated amount.
  RadiodStream kept gap events only from calls that produced output, and
     counted an empty-payload packet's span twice in total_gaps_filled.

Every scenario below builds samples whose value is the sample's true
(unwrapped) RTP index + 1, so a zero marks a fill and any delivered sample
can be checked against the RTP the stream claims for it.
"""
import logging
import struct
from types import SimpleNamespace

import numpy as np
import pytest

from ka9q.discovery import ChannelInfo
from ka9q.multi_stream import MultiStream, _ChannelSlot
from ka9q.resequencer import PacketResequencer, RTPPacket
from ka9q.stream import RadiodStream
from ka9q.stream_quality import GapEvent, GapSource, StreamQuality

M32 = 1 << 32
N = 100
T0 = 1000


@pytest.fixture(autouse=True)
def _quiet():
    logging.disable(logging.CRITICAL)
    yield
    logging.disable(logging.NOTSET)


def mk(seq, ts_unwrapped, n=N):
    s = np.arange(ts_unwrapped, ts_unwrapped + n, dtype=np.float64) + 1.0
    return RTPPacket(sequence=seq & 0xFFFF, timestamp=ts_unwrapped % M32,
                     ssrc=1, samples=s)


def zero(seq, ts_unwrapped, n=N):
    return RTPPacket(sequence=seq & 0xFFFF, timestamp=ts_unwrapped % M32,
                     ssrc=1, samples=np.zeros(n, dtype=np.float64))


def seqs(lst, base=T0):
    return [mk(s, base + s * N) for s in lst]


class Run:
    """Drive a resequencer and keep everything it emits."""

    def __init__(self, packets, buffer_size=8, spp=N, rate=16000, flush=True,
                 **kw):
        self.r = PacketResequencer(buffer_size=buffer_size,
                                   samples_per_packet=spp, sample_rate=rate,
                                   **kw)
        self.chunks = []          # (offset, label, samples)
        self.gaps = []
        self.delivered = 0
        for p in packets:
            s, g = self.r.process_packet(p)
            self._take(s, g)
        self.pre = (self.delivered, self.r.cumulative_samples,
                    self.r.stats.samples_output)
        self.pre_filled = self.r.stats.samples_filled
        self.pre_stream = self.stream()
        if flush:
            s, g = self.r.flush()
            self._take(s if len(s) else None, g)

    def _take(self, s, g):
        if s is not None:
            self.chunks.append((self.delivered, self.r.last_chunk_rtp_start, s))
            self.delivered += len(s)
        self.gaps.extend(g)

    def stream(self):
        return (np.concatenate([c[2] for c in self.chunks])
                if self.chunks else np.array([]))


def zero_runs(stream):
    z = (stream == 0).astype(np.int8)
    e = np.diff(np.concatenate(([0], z, [0])))
    a, b = np.flatnonzero(e == 1), np.flatnonzero(e == -1)
    return [(int(x), int(y - x)) for x, y in zip(a, b)]


def merged(spans):
    out = []
    for pos, dur in sorted(spans):
        if out and out[-1][0] + out[-1][1] == pos:
            out[-1] = (out[-1][0], out[-1][1] + dur)
        else:
            out.append((pos, dur))
    return out


def check(run, contiguous=True):
    r = run.r
    # Counters equal the delivered count, before and after flush.
    assert run.pre[1] == run.pre[0], "cumulative_samples before flush"
    assert run.pre[2] == run.pre[0], "samples_output before flush"
    assert r.cumulative_samples == run.delivered
    assert r.stats.samples_output == run.delivered
    stream = run.stream()
    # Every gap sits on zeros at its reported offset ...
    filled = [(g.position_samples, g.duration_samples) for g in run.gaps
              if g.duration_samples > 0]
    for pos, dur in filled:
        assert 0 <= pos and pos + dur <= len(stream), (pos, dur)
        assert not stream[pos:pos + dur].any(), (pos, dur)
    # ... and the zero runs match the gaps one to one.
    assert zero_runs(stream) == merged(filled)
    # samples_filled counts exactly the zeros delivered.
    assert r.stats.samples_filled == int((stream == 0).sum())
    assert run.pre_filled == int((run.pre_stream == 0).sum())
    # Each chunk's label + index is the true RTP of every real sample.
    for off, lab, s in run.chunks:
        assert lab is not None
        nz = np.flatnonzero(s != 0)
        true = (s[nz].astype(np.int64) - 1) % M32
        assert np.all((lab + nz) % M32 == true), (off, lab)
    # Fills keep their true length: index + first RTP == true RTP.
    if contiguous and len(stream):
        nz = np.flatnonzero(stream != 0)
        first = int(stream[nz[0]]) - 1 - int(nz[0])
        assert np.all(stream[nz].astype(np.int64) - 1 == first + nz)


SCENARIOS = {
    "i_one_loss": dict(packets=seqs([0, 1, 2, 4, 5, 6, 7, 8, 9, 10, 11])),
    "ii_two_losses": dict(packets=seqs([0, 1, 2, 4, 5, 6, 7, 8, 10, 11, 12,
                                        13, 14, 15, 16])),
    "iii_ts_jump_same_call": dict(packets=[
        mk(0, T0), mk(1, T0 + N), mk(3, T0 + 3 * N + 250), mk(2, T0 + 2 * N),
        mk(4, T0 + 4 * N + 250)]),
    "iii_b_loss_then_ts_jump": dict(packets=seqs([0, 1, 2, 4, 5, 6, 7]) + [
        mk(9, T0 + 9 * N + 250), mk(8, T0 + 8 * N), mk(10, T0 + 10 * N + 250)]),
    "iii_c_recovery_and_jump_same_call": dict(packets=[
        mk(s, T0 + s * N + (250 if s >= 5 else 0))
        for s in [0, 1, 2, 4, 5, 6, 7, 8, 9, 10]]),
    "iii_d_then_loss_11": dict(packets=[
        mk(s, T0 + s * N + (250 if s >= 5 else 0))
        for s in [0, 1, 2, 4, 5, 6, 7, 8, 9, 10, 12, 13, 14, 15, 16]]),
    "iv_reorder_no_loss": dict(packets=seqs([0, 2, 1, 3, 5, 4, 6, 7, 8])),
    "v_rtp_wrap_with_loss": dict(packets=[
        mk(s, M32 - 350 + s * N) for s in [0, 1, 2, 4, 5, 6, 7, 8, 9]]),
    "vii_b_zero_block_buffered": dict(packets=[
        mk(0, T0), zero(2, T0 + 2 * N), mk(3, T0 + 3 * N), mk(1, T0 + N)]),
    "vii_c_zero_block_after_recovery": dict(
        packets=seqs([0, 1, 2, 4, 5, 6, 7, 8, 9, 10, 11])
        + [zero(12, T0 + 12 * N)] + seqs([13])),
    "viii_burst_loss": dict(packets=seqs([0, 1, 2, 5, 6, 7, 8, 9, 10])),
    "ix_flush_two_holes": dict(packets=seqs([0, 1, 2, 4, 6]), buffer_size=16),
    "ix_b_flush_across_seq_wrap": dict(packets=[
        mk(s, T0 + (s - 65533) * N) for s in [65533, 65534, 65536, 65537]],
        buffer_size=16),
    "ix_c_flush_wrap_hole_before_wrap": dict(packets=[
        mk(s, T0 + (s - 65533) * N) for s in [65533, 65535, 65536, 65537]],
        buffer_size=16),
}


def _frag():
    lens = [60, 40] * 8
    ts = [T0]
    for ln in lens[:-1]:
        ts.append(ts[-1] + ln)
    return [mk(i, ts[i], lens[i]) for i in range(len(lens))]


SCENARIOS["vi_fragments_one_lost"] = dict(
    packets=[p for i, p in enumerate(_frag()) if i != 3])
SCENARIOS["vi_b_fragments_two_lost"] = dict(
    packets=[p for i, p in enumerate(_frag()) if i not in (3, 10)])


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_scenario_invariants(name):
    check(Run(**SCENARIOS[name]))


def test_flush_wrap_delivers_the_true_span():
    """flushwrap.py: 600 samples for a 500-sample span, seq 65535 after 0, 1."""
    run = Run([mk(65533, T0), mk(65535, T0 + 2 * N), mk(0, T0 + 3 * N),
               mk(1, T0 + 4 * N)], buffer_size=16)
    s = run.stream()
    assert len(s) == 5 * N
    assert [int(s[k]) - 1 - T0 if s[k] else None
            for k in range(0, len(s), N)] == [0, None, 200, 300, 400]
    assert run.r.stats.samples_filled == N
    check(run)


def test_flush_sets_last_chunk_rtp_start():
    r = PacketResequencer(buffer_size=16, samples_per_packet=N)
    r.process_packet(mk(0, T0))
    r.process_packet(mk(2, T0 + 2 * N))       # emits seq 0; seq 2 waits
    out, gaps = r.flush()
    assert len(out) == 2 * N                  # fill for seq 1, then seq 2
    assert r.last_chunk_rtp_start == T0 + N
    assert [(g.position_samples, g.duration_samples) for g in gaps] == [(N, N)]


def test_reported_positions_are_exact_in_scenario_ii():
    run = Run(**SCENARIOS["ii_two_losses"])
    nl = [(g.position_samples, g.duration_samples) for g in run.gaps]
    assert nl == [(300, 100), (900, 100)]


# ── item 4: gaps longer than MAX_GAP_SAMPLES ──

RATE = 16000


def test_long_outage_is_filled_at_its_true_length_detect_path():
    """A contiguous-sequence ts jump past the old 960,000 cap."""
    G = 1_000_000                              # 62.5 s at 16 kHz
    pk = [mk(0, T0), mk(1, T0 + N), mk(2, T0 + 2 * N + G), mk(3, T0 + 3 * N + G)]
    run = Run(pk, rate=RATE)
    assert [g.duration_samples for g in run.gaps] == [G]
    check(run)


def test_long_outage_is_filled_at_its_true_length_loss_path():
    G = 1_200_000
    pk = [mk(s, T0 + s * N + (G if s >= 3 else 0))
          for s in [0, 1, 3, 4, 5, 6, 7, 8, 9]]       # seq 2 lost too
    run = Run(pk, rate=RATE)
    assert sum(g.duration_samples for g in run.gaps) == G + N
    check(run)


def test_gap_past_the_fill_bound_breaks_continuity_loudly():
    """A gap no fill should cover (a re-based RTP clock, an outage longer
    than max_fill_seconds): nothing is filled, a STREAM_INTERRUPTION event
    says continuity broke, and the post-gap packet starts a fresh chunk
    whose label is its own true RTP."""
    G = 10 * 60 * RATE                         # 10 minutes
    pre = [mk(0, T0), mk(1, T0 + N), mk(2, T0 + 2 * N)]
    post = [mk(s, T0 + s * N + G) for s in range(3, 8)]
    r = PacketResequencer(buffer_size=8, samples_per_packet=N, sample_rate=RATE)
    calls = [r.process_packet(p) + (r.last_chunk_rtp_start,) for p in pre + post]
    emitted = [(s, g, lab) for s, g, lab in calls if s is not None]
    events = [e for _, g, _ in emitted for e in g]
    assert [e.source for e in events] == [GapSource.STREAM_INTERRUPTION]
    assert events[0].duration_samples == 0
    # The chunk carrying the interruption begins at the post-gap packet.
    k = next(i for i, (_, g, _) in enumerate(emitted) if g)
    s, g, lab = emitted[k]
    assert lab == (T0 + 3 * N + G) % M32
    assert int(s[0]) - 1 == T0 + 3 * N + G
    assert g[0].position_samples == sum(len(x) for x, _, _ in emitted[:k])
    assert not any((x == 0).any() for x, _, _ in emitted)
    assert r.cumulative_samples == sum(len(x) for x, _, _ in emitted)
    assert r.stats.samples_filled == 0


def test_interruption_never_shares_a_chunk_with_pre_gap_samples():
    """Seq 3 arrives late, so seq 2, 3 and the post-gap seq 4 would leave in
    one call; the call must end before the break."""
    G = 10 * 60 * RATE
    r = PacketResequencer(buffer_size=16, samples_per_packet=N, sample_rate=RATE)
    out = []
    for p in [mk(0, T0), mk(1, T0 + N), mk(2, T0 + 2 * N),
              mk(4, T0 + 4 * N + G), mk(3, T0 + 3 * N), mk(5, T0 + 5 * N + G)]:
        s, g = r.process_packet(p)
        if s is not None:
            out.append((r.last_chunk_rtp_start, s, g))
    for lab, s, g in out:
        assert np.all((lab + np.arange(len(s))) % M32
                      == (s.astype(np.int64) - 1) % M32)
    assert sum(len(s) for _, s, _ in out) == 6 * N


# ── RadiodStream / MultiStream ──

def _channel():
    return ChannelInfo(ssrc=7, preset="usb", sample_rate=RATE, frequency=1e7,
                       snr=0.0, multicast_address="239.1.2.3", port=5004,
                       encoding=4)


def _rtp(seq, ts, samples, ssrc=7):
    hdr = struct.pack("!BBHII", 0x80, 0, seq & 0xFFFF, ts % M32, ssrc)
    return hdr + np.asarray(samples, dtype="<f4").tobytes()


def _radiod_stream(batches, **kw):
    kw.setdefault("samples_per_packet", N)
    kw.setdefault("resequence_buffer_size", 8)
    kw.setdefault("deliver_interval_packets", 3)
    rs = RadiodStream(_channel(),
                      on_samples=lambda s, q: batches.append((s.copy(), q)),
                      **kw)
    rs._first_rtp_timestamp = None             # start() sets it; no socket here
    rs._running = True                         # lets stop() flush
    return rs


def _vals(seq, ts, n=N):
    return np.arange(ts, ts + n, dtype=np.float64) + 1.0


def test_radiodstream_keeps_gaps_from_a_call_with_no_output():
    batches = []
    rs = _radiod_stream(batches, deliver_interval_packets=1)
    gap = GapEvent(source=GapSource.NETWORK_LOSS, position_samples=0,
                   duration_samples=0, timestamp_utc="", packets_affected=0)
    real = rs.resequencer.process_packet
    answers = iter([(None, [gap])])
    rs.resequencer.process_packet = lambda p: next(answers, None) or real(p)
    rs._process_packet(_rtp(0, T0, _vals(0, T0)))       # no output, one gap
    rs.resequencer.process_packet = real
    rs._process_packet(_rtp(0, T0, _vals(0, T0)))
    rs._process_packet(_rtp(1, T0 + N, _vals(1, T0 + N)))
    assert any(g is gap for _, q in batches for g in q.batch_gaps)


def test_radiodstream_zero_block_gap_lands_with_its_zeros():
    batches = []
    rs = _radiod_stream(batches, deliver_interval_packets=1)
    for seq, z in [(0, False), (2, True), (3, False), (1, False), (4, False)]:
        ts = T0 + seq * N
        rs._process_packet(_rtp(seq, ts, np.zeros(N) if z else _vals(seq, ts)))
    rs.stop()
    stream = np.concatenate([s for s, _ in batches])
    gaps = [g for _, q in batches for g in q.batch_gaps]
    assert [(g.source, g.position_samples, g.duration_samples) for g in gaps] \
        == [(GapSource.RADIOD_BLOCK_DROP, 2 * N, N)]
    assert zero_runs(stream) == [(2 * N, N)]
    # Each batch's gaps fall inside that batch.
    for s, q in batches:
        for g in q.batch_gaps:
            assert 0 <= g.position_samples - q.batch_start_sample < len(s)


def test_radiodstream_empty_payload_span_counted_once():
    batches = []
    rs = _radiod_stream(batches)
    for seq in range(12):
        ts = T0 + seq * N
        if seq == 3:
            rs._process_packet(_rtp(seq, ts, []))        # empty payload
        else:
            rs._process_packet(_rtp(seq, ts, _vals(seq, ts)))
    rs.stop()
    stream = np.concatenate([s for s, _ in batches])
    zeros = int((stream == 0).sum())
    assert zeros == N
    q = rs.quality
    assert q.total_gaps_filled == zeros
    assert sum(g.duration_samples for _, b in batches for g in b.batch_gaps) \
        == zeros


def _labels_hold(batches):
    for s, q in batches:
        assert q.delivered_rtp_start is not None
        first = (q.first_rtp_timestamp + q.total_samples_delivered - len(s)) % M32
        assert first == q.delivered_rtp_start
        nz = np.flatnonzero(s != 0)
        assert np.all((first + nz) % M32 == (s[nz].astype(np.int64) - 1) % M32)


def test_radiodstream_labels_hold_across_a_long_filled_outage():
    batches = []
    rs = _radiod_stream(batches)
    G = 1_000_000
    for seq in range(10):
        ts = T0 + seq * N + (G if seq >= 4 else 0)
        rs._process_packet(_rtp(seq, ts, _vals(seq, ts)))
    rs.stop()
    _labels_hold(batches)
    assert rs.quality.first_rtp_timestamp == T0


def test_radiodstream_rebases_its_anchor_at_an_interruption():
    """first_rtp_timestamp + total_samples_delivered must keep naming the
    next sample's RTP, the way hf-timestd's ring buffer and wspr-recorder
    label batches, even when the stream could not fill the gap."""
    batches = []
    rs = _radiod_stream(batches)
    G = 10 * 60 * RATE
    for seq in range(16):
        ts = T0 + seq * N + (G if seq >= 5 else 0)
        rs._process_packet(_rtp(seq, ts, _vals(seq, ts)))
    rs.stop()
    _labels_hold(batches)
    assert sum(len(s) for s, _ in batches) == 16 * N
    assert rs.quality.total_gaps_filled == 0
    events = [g for _, q in batches for g in q.batch_gaps]
    assert [g.source for g in events] == [GapSource.STREAM_INTERRUPTION]


def _slot(cb):
    return _ChannelSlot(
        channel_info=None,
        resequencer=PacketResequencer(buffer_size=8, samples_per_packet=N,
                                      sample_rate=RATE),
        quality=StreamQuality(), on_samples=cb, sample_rate=RATE,
        deliver_interval=3, frequency_hz=0.0, preset="usb", encoding=4,
        is_iq=False, on_stream_dropped=None, on_stream_restored=None,
    )


def test_multistream_rebases_its_anchor_at_an_interruption():
    batches = []

    def cb(s, q):
        batches.append((s.copy(), q.copy()))

    m = MultiStream(control=None)
    slot = _slot(cb)
    G = 10 * 60 * RATE
    for seq in range(16):
        ts = T0 + seq * N + (G if seq >= 5 else 0)
        if slot.first_rtp_timestamp is None:
            slot.first_rtp_timestamp = ts
            slot.quality.first_rtp_timestamp = ts
        out, gaps = slot.resequencer.process_packet(mk(seq, ts))
        m._route_output(slot, out, gaps)
    _labels_hold(batches)
    events = [g for _, q in batches for g in q.batch_gaps]
    assert [g.source for g in events] == [GapSource.STREAM_INTERRUPTION]


# ── stop(): the final batch is delivered and labelled ──

def test_radiodstream_stop_delivers_the_pending_batch_labelled():
    batches = []
    rs = _radiod_stream(batches, deliver_interval_packets=10)
    for seq in range(4):
        rs._process_packet(_rtp(seq, T0 + seq * N, _vals(seq, T0 + seq * N)))
    rs.stop()
    assert sum(len(s) for s, _ in batches) == 4 * N
    _labels_hold(batches)


def test_radiodstream_flush_only_batch_carries_its_label():
    batches = []
    rs = _radiod_stream(batches, deliver_interval_packets=1,
                        resequence_buffer_size=16)
    for seq in (0, 1, 3):                     # seq 3 waits behind the hole
        rs._process_packet(_rtp(seq, T0 + seq * N, _vals(seq, T0 + seq * N)))
    rs.stop()
    assert sum(len(s) for s, _ in batches) == 4 * N
    _labels_hold(batches)


def test_multistream_stop_flush_batch_carries_its_label():
    batches = []

    def cb(s, q):
        batches.append((s.copy(), q.copy()))

    m = MultiStream(control=None)
    slot = _slot(cb)
    slot.deliver_interval = 1
    slot.resequencer = PacketResequencer(buffer_size=16, samples_per_packet=N,
                                         sample_rate=RATE)
    slot.first_rtp_timestamp = slot.quality.first_rtp_timestamp = T0
    for seq in (0, 1, 3):
        out, gaps = slot.resequencer.process_packet(mk(seq, T0 + seq * N))
        m._route_output(slot, out, gaps)
    m._slots = {7: slot}
    m._running = True
    m.stop()
    assert sum(len(s) for s, _ in batches) == 4 * N
    _labels_hold(batches)
