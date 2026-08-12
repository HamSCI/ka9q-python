"""Does create_channel converge when the channel already exists? (b1 only)

Task 6 verdict under test (idempotency.md Q1): DEFECT — an existing-SSRC
create is a radiod-side delta-update (only TLVs present in the *new*
packet are applied), not an atomic template reset. For two calls with
*identical* params (this probe), every relevant TLV is re-sent both
times, so the expected empirical outcome is CONVERGES — the delta-update
mechanism is invisible when nothing differs between calls. See
probe_create_twice_variant.py for the case that is expected to surface
the DEFECT (differing params on the second call).
"""
import sys
from ka9q import RadiodControl

HOST, SSRC, FREQ = "bee1-status.local", 3999900001, 7_040_000.0
FIELDS = ("frequency", "sample_rate", "preset", "encoding")


def snap(info):
    return {f: getattr(info, f, None) for f in FIELDS}


c = RadiodControl(HOST)
try:
    c.create_channel(FREQ, preset="usb", sample_rate=12000, ssrc=SSRC)
    first = snap(c.poll_channel(SSRC, expected_freq=FREQ, timeout=5.0))
    c.create_channel(FREQ, preset="usb", sample_rate=12000, ssrc=SSRC)
    second = snap(c.poll_channel(SSRC, expected_freq=FREQ, timeout=5.0))
    print("first: ", first)
    print("second:", second)
    print("CONVERGES" if first == second else "DIVERGES")
    sys.exit(0 if first == second else 1)
finally:
    try:
        c.remove_channel(SSRC)
    finally:
        c.close()
