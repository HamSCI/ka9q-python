"""After ManagedStream recovery, is channel state identical? Simulates loss
by removing the channel out from under the stream. (b1 only)

Adaptations from the brief (verified against ka9q/managed_stream.py before
running):

1. `ManagedStream.__init__` takes no `ssrc=` parameter. It always derives
   the channel's SSRC deterministically inside `RadiodControl.ensure_channel`
   -> `allocate_ssrc(frequency_hz, preset, sample_rate, agc, gain,
   destination, encoding, radiod_host)` (control.py:68-130). There is no
   supported way to force that hash into the 3999900000-3999900999 test
   range from the public ManagedStream API. To stay within the spirit of
   the SSRC-range rule (traceability / avoiding collision with real
   channels) while keeping the probe using the real production API
   surface, this probe:
     - uses an out-of-band, clearly-synthetic test frequency
       (`FREQ = 7_040_000.0 + 900_003`, i.e. 7.040903 MHz, chosen only to
       be unlikely to collide with any channel already running on b1 --
       confirmed absent from the live `discover_channels()` listing taken
       immediately before this probe ran, see task-7-report.md) so the
       resulting channel is identifiable in logs even though its SSRC is
       not in the reserved numeric block;
     - reads the actual SSRC from the `ChannelInfo` returned by
       `stream.start()` (`.ssrc` attribute, confirmed present on
       `ChannelInfo` in ka9q/discovery.py) rather than a `stream.ssrc`
       attribute (no such attribute exists on `ManagedStream`);
     - explicitly removes that actual SSRC via a plain `RadiodControl` in
       `finally`, regardless of whether it fell in the reserved range,
       since `ManagedStream.stop()` (managed_stream.py) only tears down
       the local RTP receive path and intentionally does NOT call
       `remove_channel` on radiod (channels are meant to be shareable
       across clients per `ensure_channel`'s design) -- so stop() alone
       would leak the channel.
   This deviation is called out explicitly in task-7-report.md per the
   brief's instruction to "verify actual attribute names... and note any
   adaptation."

2. `ManagedStream.stop()` returns `ManagedStreamStats`, not `None` --
   used only for its side effect here, matching the brief.
"""
import sys
import time
from ka9q import RadiodControl
from ka9q.managed_stream import ManagedStream

HOST = "bee1-status.local"
FREQ = 7_040_000.0 + 900_003  # 7.040903 MHz -- synthetic test frequency
FIELDS = ("frequency", "sample_rate", "preset", "encoding")


def snap(info):
    return {f: getattr(info, f, None) for f in FIELDS}


c = RadiodControl(HOST)
saboteur = RadiodControl(HOST)
stream = None
actual_ssrc = None
try:
    stream = ManagedStream(c, FREQ, preset="usb", sample_rate=12000,
                            on_samples=lambda *a, **k: None)
    started = stream.start()
    actual_ssrc = started.ssrc
    print("actual_ssrc (hash-derived, not test-range):", actual_ssrc)
    time.sleep(3)
    before = snap(c.poll_channel(actual_ssrc, expected_freq=FREQ, timeout=5.0))
    saboteur.remove_channel(actual_ssrc)   # simulate radiod losing the channel
    time.sleep(15)                          # allow monitor to detect + recreate
    after = snap(c.poll_channel(actual_ssrc, expected_freq=FREQ, timeout=5.0))
    print("before:", before)
    print("after: ", after)
    print("EQUIVALENT" if before == after else "DIVERGED")
    sys.exit(0 if before == after else 1)
finally:
    if stream is not None:
        stream.stop()
    if actual_ssrc is not None:
        try:
            c.remove_channel(actual_ssrc)
        except Exception as e:
            print("cleanup remove_channel failed:", e)
    for ctl in (saboteur, c):
        try:
            ctl.close()
        except Exception:
            pass
