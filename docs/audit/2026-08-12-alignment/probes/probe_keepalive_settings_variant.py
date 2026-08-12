"""Variant of probe_keepalive_settings.py: tests Q4 (keepalive preservation)
properly, in two phases, after Round 2 diagnosis showed the brief's
original probe design couldn't distinguish "expired" from "encoding lost
while alive" (see idempotency.md "Empirical results — Round 2").

Round 2 finding #1 (units): `lifetime=20` in the brief's original probe
is 20 radiod *frames* (control.py:1313-1322: "~1000 frames ~= 20s" at the
default 20ms blocktime), i.e. ~0.4s of protected time -- not 20 seconds.
A single poll at t=0 then a flat 30s sleep gives the channel no chance to
survive regardless of any keepalive mechanism, so the brief's original
probe (kept as-is, unmodified, in probe_keepalive_settings.py per the
"re-run per original instructions" round-2 directive) mostly demonstrates
frame-vs-second unit confusion, not a real preservation/loss result. This
variant uses `lifetime=1000` (~20s, the unit the docstring itself uses
for its example) so a 30s window can actually distinguish "expired on
schedule" from "kept alive."

Round 2 finding #2 (does a bare poll count as a keepalive?):
control.py:1968-1970's `set_channel_lifetime` docstring claims "Polling
auto-extends a non-zero lifetime to at least ~20s, so a client ... should
call this method (or any other poll) periodically as a keep-alive"
(emphasis on "or any other poll"). Phase A below tests that literally: a
`lifetime=1000` channel, polled every 5s via plain `poll_channel()`
(which sends only OUTPUT_SSRC+COMMAND_TAG, no LIFETIME tag) with no
`set_channel_lifetime()` call at all. Phase B is the control: the same
setup, but with an explicit `set_channel_lifetime()` refresh every 5s (the
call path Q4's VERIFIED-BY-CODE verdict is actually about).

Empirically (see idempotency.md for the raw traces): Phase A's channel
died on schedule at ~20s despite four prior plain polls -- a bare
`poll_channel()` query does NOT extend LIFETIME on this radiod build,
contradicting the "(or any other poll)" clause of the docstring quoted
above. Phase B's channel survived the full 30s with encoding reading back
as F32LE at every single check -- confirming Q4's VERIFIED-BY-CODE verdict
for the `set_channel_lifetime` code path specifically.
"""
import sys
import time
from ka9q import RadiodControl, Encoding

HOST = "bee1-status.local"
LAN_IP = "192.168.1.176"
WORKING_DESTINATION = "239.139.172.41"
FREQ = 7_040_000.0
LIFETIME_FRAMES = 1000  # ~20s at default 20ms blocktime (control.py:1313-1322)
SSRC_A, SSRC_B = 3999900008, 3999900009


def run_phase(c, ssrc, refresh_with_set_lifetime):
    c.create_channel(FREQ, preset="usb", sample_rate=12000, ssrc=ssrc,
                      encoding=Encoding.F32LE, lifetime=LIFETIME_FRAMES,
                      destination=WORKING_DESTINATION)
    readings = []
    for i in range(7):  # t=0,5,...,30
        info = c.poll_channel(ssrc, expected_freq=FREQ, timeout=5.0)
        readings.append(info.encoding if info else None)
        print(f"  t+{i*5}s encoding:", readings[-1])
        if i < 6:
            if refresh_with_set_lifetime:
                c.set_channel_lifetime(ssrc, LIFETIME_FRAMES)
            time.sleep(5)
    return readings


c = RadiodControl(HOST, interface=LAN_IP)
try:
    print("Phase A: lifetime=1000 frames, plain poll_channel() every 5s, "
          "no set_channel_lifetime() call -- does a bare poll count as a "
          "keepalive per the docstring's '(or any other poll)' claim?")
    a = run_phase(c, SSRC_A, refresh_with_set_lifetime=False)

    print("Phase B: lifetime=1000 frames, explicit set_channel_lifetime() "
          "refresh every 5s -- the code path Q4's VERIFIED-BY-CODE verdict "
          "is actually about.")
    b = run_phase(c, SSRC_B, refresh_with_set_lifetime=True)

    a_survived_30s = a[-1] is not None
    b_survived_and_preserved = all(r == Encoding.F32LE for r in b)
    print("Phase A survived to t+30s (bare poll IS a keepalive):",
          a_survived_30s)
    print("Phase B survived to t+30s with encoding preserved throughout "
          "(set_channel_lifetime IS a working keepalive):",
          b_survived_and_preserved)
    # Exit 0 = matches the empirical result documented above (A expires,
    # B survives+preserves). This is a descriptive probe, not a
    # pass/fail gate on Task 6 -- it directly informs Q4's scope.
    sys.exit(0 if (not a_survived_30s and b_survived_and_preserved) else 1)
finally:
    for ssrc in (SSRC_A, SSRC_B):
        try:
            c.remove_channel(ssrc)
        except Exception as e:
            print(f"cleanup remove_channel({ssrc}) failed:", e)
    c.close()
