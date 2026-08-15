"""radiod emits a block of zeros when it drops a filter output block.

ka9q-radio 55d9048d (release 2026.08.15-1-trixie1) redid the output
filter stall logic: a lapped block is counted and a block of zeros is
emitted, rather than the timeline being silently re-dated.  Sample-count
integrity is preserved, which is the important part.

The consequence for consumers is that those zeros arrive with CONTIGUOUS
RTP timestamps.  The resequencer sees no timestamp gap, so nothing in
the existing gap plumbing lights up: batch_gaps stays empty, gap_samples
stays zero, completeness still reads 100%.  A stall becomes invisible
downstream at exactly the moment it matters -- hf-timestd's T6 matched
filter integrates over +/-0.5 s, so zeros inside that window bias the
correlation rather than merely blanking it.

Phil's own argument is what makes the fix possible: a solid block of
exact IEEE-754 zeros across every sample does not occur in real RF.  A
32-bit float carries ~144 dB of instantaneous dynamic range, so noise
always occupies the low bits of a genuine signal.  Exact zeros are
therefore a reliable sentinel, and we can raise them as a gap ourselves.

Detected here rather than in hf-timestd so every consumer of the stream
inherits it through the plumbing that already exists.
"""
import numpy as np
import pytest

from ka9q.resequencer import PacketResequencer, RTPPacket
from ka9q.stream_quality import GapSource

SR = 96_000
N = 180


def _pkt(seq, ts, samples=None, n=N):
    if samples is None:
        samples = np.full(n, 0.5 + 0.5j, dtype=np.complex64)
    return RTPPacket(sequence=seq & 0xFFFF, timestamp=ts & 0xFFFFFFFF,
                     ssrc=1, samples=samples, wallclock=None)


def _zeros(n=N):
    return np.zeros(n, dtype=np.complex64)


def _reseq():
    r = PacketResequencer(buffer_size=64, samples_per_packet=N, sample_rate=SR)
    r.process_packet(_pkt(0, 1000))          # initialise
    return r


def test_an_all_zero_packet_is_reported_as_a_gap():
    r = _reseq()

    _out, gaps = r.process_packet(_pkt(1, 1000 + N, _zeros()))

    assert len(gaps) == 1
    assert gaps[0].source is GapSource.RADIOD_BLOCK_DROP
    assert gaps[0].duration_samples == N


def test_a_normal_packet_reports_no_gap():
    r = _reseq()

    _out, gaps = r.process_packet(_pkt(1, 1000 + N))

    assert gaps == []


def test_a_single_nonzero_sample_is_not_a_drop():
    """One non-zero sample means radiod delivered real data.  The
    sentinel is a SOLID block of zeros, nothing weaker."""
    s = _zeros()
    s[N // 2] = np.complex64(1e-9 + 0j)
    r = _reseq()

    _out, gaps = r.process_packet(_pkt(1, 1000 + N, s))

    assert gaps == []


def test_the_zeros_are_still_delivered():
    """Sample-count integrity is the whole point of Phil's design; we
    flag the zeros, we do not drop them.

    The resequencer's first emission also carries the initialising
    packet (see test_delivered_rtp_start), so drive one normal packet
    through first and check the zeros arrive intact after it.
    """
    r = _reseq()
    r.process_packet(_pkt(1, 1000 + N))          # flushes the init packet

    out, _gaps = r.process_packet(_pkt(2, 1000 + 2 * N, _zeros()))

    assert out is not None
    assert len(out) == N          # sample count preserved, nothing dropped
    assert not out.any()          # and they are the zeros radiod sent


def test_each_zero_packet_of_a_dropped_block_is_counted():
    """One 20 ms block at 96 kHz is 1920 samples = 11 packets on the
    wire, so a single dropped block arrives as a run of zero packets."""
    r = _reseq()
    total = 0

    for k in range(1, 11):
        _out, gaps = r.process_packet(_pkt(k, 1000 + k * N, _zeros()))
        total += sum(g.duration_samples for g in gaps)

    assert total == 10 * N


def test_gap_position_tracks_the_stream():
    r = _reseq()
    r.process_packet(_pkt(1, 1000 + N))

    _out, gaps = r.process_packet(_pkt(2, 1000 + 2 * N, _zeros()))

    assert gaps[0].position_samples > 0


def test_the_gap_survives_a_packet_that_produces_no_output():
    """MultiStream only harvested gaps when the resequencer also
    produced output, so a zero packet arriving out of order would have
    its drop marker discarded -- the stall going silent again, one
    layer further up."""
    r = _reseq()
    r.process_packet(_pkt(1, 1000 + N))              # flush the init packet

    # seq 3 arrives before seq 2: buffered, nothing emitted yet.
    out, gaps = r.process_packet(_pkt(3, 1000 + 3 * N, _zeros()))

    assert out is None                               # nothing delivered...
    assert len(gaps) == 1                            # ...but the drop is still reported
    assert gaps[0].source is GapSource.RADIOD_BLOCK_DROP
