"""Variant of probe_create_twice.py: second create call OMITS a param that
was set on the first call. (b1 only)

Task 6's idempotency.md Q1 argues the concrete failure mode is: a first
create sets a non-default sample_rate; a second create on the same SSRC
that doesn't repeat sample_rate= (Python arg falsy/None -> OUTPUT_SAMPRATE
TLV omitted from the wire packet) leaves the *existing* radiod-side value
untouched rather than resetting to a template/preset default, because
radiod applies the second command as a delta-update (decode_radio_commands
only mutates fields whose TLV is present), not a full re-init
(create_chan()'s template reset only happens on a *first* create for an
unused SSRC).

Expected empirical outcome per Task 6: sample_rate stays at the
first call's value (48000) after the second call, even though the
second call's own default is 12000 -- i.e. the two calls' polled
snapshots for sample_rate DIVERGE from what a "second call's stated
params fully apply" model would predict, but sample_rate specifically
should read as unchanged (48000) both times, evidencing delta-update
over reset.

Round 2 adaptations: `interface=` and `destination=` required — see
probe_create_twice.py's docstring and idempotency.md "Empirical results
— Round 2" for the full explanation.
"""
import sys
from ka9q import RadiodControl

HOST, SSRC, FREQ = "bee1-status.local", 3999900004, 7_040_000.0
LAN_IP = "192.168.1.176"
WORKING_DESTINATION = "239.139.172.41"
FIELDS = ("frequency", "sample_rate", "preset", "encoding")


def snap(info):
    return {f: getattr(info, f, None) for f in FIELDS}


c = RadiodControl(HOST, interface=LAN_IP)
try:
    # First call: explicit non-default sample_rate.
    c.create_channel(FREQ, preset="usb", sample_rate=48000, ssrc=SSRC,
                      destination=WORKING_DESTINATION)
    first = snap(c.poll_channel(SSRC, expected_freq=FREQ, timeout=5.0))

    # Second call on the SAME ssrc: sample_rate omitted (None), so
    # create_channel's own default (per its signature, sample_rate:
    # Optional[int] = None) means OUTPUT_SAMPRATE is not sent this time.
    # destination= is repeated (same value) -- omitting it on the second
    # call would itself introduce the "destination missing" failure mode
    # from Round 1, which is not what this probe is testing.
    c.create_channel(FREQ, preset="usb", ssrc=SSRC, destination=WORKING_DESTINATION)
    second = snap(c.poll_channel(SSRC, expected_freq=FREQ, timeout=5.0))

    print("first: ", first)
    print("second:", second)
    preserved = second["sample_rate"] == 48000
    print("SAMPLE_RATE PRESERVED (delta-update, matches DEFECT verdict)"
          if preserved else
          "SAMPLE_RATE RESET (contradicts DEFECT verdict)")
    # Exit 0 means the empirical result matches Task 6's predicted
    # delta-update behavior (sample_rate survives the second call
    # despite not being re-sent).
    sys.exit(0 if preserved else 1)
finally:
    try:
        c.remove_channel(SSRC)
    finally:
        c.close()
