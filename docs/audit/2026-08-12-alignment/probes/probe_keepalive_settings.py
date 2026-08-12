"""Does a keepalive-refreshed channel keep its creation-time encoding? (b1 only)

Adaptation note (verified against ka9q/discovery.py before running):
ChannelInfo.encoding is a plain int field (0=none, 4=F32LE, ...), not an
Encoding enum instance -- but ka9q.types.Encoding.F32LE is itself just the
plain int constant 4, so `after.encoding == Encoding.F32LE` compares
correctly with no attribute-name change needed. No adaptation to the
brief's attribute name was required.

There is no library-level "send a keepalive" call in RadiodControl. Per
control.py:1962-1975 (`set_channel_lifetime` docstring), radiod itself
auto-extends a channel's non-zero lifetime to at least
`Channel_idle_timeout` (~20s) any time it receives a poll naming that
SSRC -- so the "before" `poll_channel()` call in this probe is itself the
activity that extends the channel's idle timer; there is no separate
Python-side re-send-settings keepalive loop running during the 30s sleep.
This probe is therefore a black-box check of two things at once: (a)
does the channel survive to the 30s mark at all (radiod-side idle-timeout
behavior, given only the single poll at t=0), and (b) if it survives,
does its encoding still read back as what was set at creation. A `None`
result from the second `poll_channel()` (channel destroyed) would print
as "LOST" via the `after is not None` check, distinguishing "expired"
from "encoding silently changed while still alive." See idempotency.md
Q4: the VERIFIED-BY-CODE verdict is specifically about
`set_channel_lifetime`'s explicit re-send of encoding on refresh; this
probe does not call `set_channel_lifetime` and is not a direct empirical
test of that code path -- it is the closest black-box analog buildable
from the brief's given probe shape.

Round 2 adaptations: `interface=` and `destination=` required — see
probe_create_twice.py's docstring and idempotency.md "Empirical results
— Round 2" for the full explanation.
"""
import sys
import time
from ka9q import RadiodControl, Encoding

HOST, SSRC, FREQ = "bee1-status.local", 3999900002, 7_040_000.0
LAN_IP = "192.168.1.176"
WORKING_DESTINATION = "239.139.172.41"

c = RadiodControl(HOST, interface=LAN_IP)
try:
    c.create_channel(FREQ, preset="usb", sample_rate=12000, ssrc=SSRC,
                      encoding=Encoding.F32LE, lifetime=20,
                      destination=WORKING_DESTINATION)
    before = c.poll_channel(SSRC, expected_freq=FREQ, timeout=5.0)
    time.sleep(30)  # > one keepalive interval for lifetime=20
    after = c.poll_channel(SSRC, expected_freq=FREQ, timeout=5.0)
    print("encoding before/after:", before.encoding if before else None,
          after.encoding if after else None)
    ok = after is not None and after.encoding == Encoding.F32LE
    print("PRESERVED" if ok else "LOST")
    sys.exit(0 if ok else 1)
finally:
    try:
        c.remove_channel(SSRC)
    finally:
        c.close()
