# Keeping channels alive on a shared control plane

**Status:** design, not built. 2026-09-21.
**Supersedes in practice:** the 30-second `set_channel_lifetime` keepalive.

## What Phil asks of a controller

Four rules, from ka9q's author on 2026-09-21:

1. The control/status protocol stays **multicast, idempotent and shared**, so
   several controllers can drive one radiod and all hold a coherent view.
2. A controller sends configuration parameters autonomously **only** when
   retransmitting a command whose tag has not yet been echoed. Otherwise it
   sends an empty poll.
3. Because every poll response dumps the whole channel state, a controller
   should **share other controllers' responses** rather than ask again, and
   should **randomize its poll timing**.
4. A controller resets its poll timer when a status arrives that another
   controller's poll triggered.

And one prohibition that shapes everything below: no controller should
automatically *correct* what it believes to be a wrong channel state. That
starts a tuning war. State changes follow the user.

## What we do today, and why it has to change

`set_channel_lifetime(ssrc, 6000)` every 30 seconds per channel, carrying a
re-asserted output encoding alongside. On AC0G-B4 that is 120 autonomous
configuration broadcasts an hour, which every other controller on the plane
must receive and parse.

Rule 2 rules it out. Two measurements say we can drop it outright:

- **An empty poll refreshes the self-destruct timer.** `decode_radio_commands`
  begins `chan->lifetime = chan->lifestart`, for any command of length ≥ 2,
  and `poll_channel` sends `COMMAND_TAG + OUTPUT_SSRC + EOL` and nothing
  else. Measured on B4 2026-09-21: an unpolled control channel died at
  37.7 s; a channel polled every 5 s outlived it and then died 43.7 s after
  the polls stopped — one fresh lifetime from its last poll.
- **The re-asserted encoding defends against nothing.** `loadpreset` sits
  inside `case PRESET:` at both our pin and `ka9q/main`, so a LIFETIME-only
  command never reaches it. Our own issue (HamSCI/ka9q-python#3) records that
  we never reproduced the downgrade it guards against.

## The design

### The fact that makes it simple

Every status packet carries the channel's **current remaining lifetime**
(`radio_status.c`: `encode_int32(&bp,LIFETIME,chan->lifetime)`), and
`status.py` already decodes it into `ChannelStatus.lifetime`.

So a controller never has to *infer* whether someone else's command refreshed
a timer. It reads the answer. When another controller polls a channel we own,
the next status we see carries a lifetime that has jumped back up, and our
own poll becomes unnecessary — rules 3 and 4 satisfied by observation rather
than by bookkeeping about tags.

That turns the keepalive from open loop (fire every 30 s regardless) into
closed loop (act when the channel actually needs it).

### Shape

A `ChannelKeepalive` holding, per owned SSRC:

- `lifetime_frames` — the last value seen in a status
- `observed_at` — the monotonic time of that reading
- `block_time_s` — measured, not assumed (see below)

On each tick it projects the remaining lifetime forward from the last
reading, and polls a channel only when that projection falls under a
threshold, with jitter applied so two controllers do not synchronise.

```
remaining_s ≈ (lifetime_frames × block_time_s) − (now − observed_at)
poll when    remaining_s < threshold_s × uniform(0.85, 1.15)
```

### Three things that must not go wrong

**⛔ Absence of status must never read as health.** If no status has arrived
for a channel within some bound, the controller knows nothing about its
lifetime and must poll unconditionally. A suppression rule whose quiet state
is "do nothing" turns a dropped multicast group into a fleet of channels
quietly self-destructing. The fail-safe direction is to poll.

**⛔ Suppression is per-SSRC, never global.** `chan->lifetime` is refreshed by
a command addressed to *that channel*. Another controller polling a different
SSRC refreshes nothing of ours. Seeing traffic on the plane is not evidence
our channel was touched; seeing a status *for our SSRC* is.

**⚠ Block time is a measurement, not a constant.** `lifetime` counts radiod
main-loop blocks, and the conversion to seconds depends on the configured
block time. B4 measured ~18.9 ms on 2026-09-21 (2000 frames ≈ 37.7 s) with no
`blocktime` in its radiod config, so that is a default rather than a fleet
constant. Derive it per station by watching the lifetime count down between
two statuses, and fall back to polling on a fixed short period when it cannot
be derived.

### Threshold

Margin has to cover the status cadence (`STATUS_INTERVAL`), the poll round
trip, and lost packets on a multicast group with no retransmission. A
threshold near a third of the configured lifetime leaves room for two missed
statuses before the channel is at risk. With our 6000 frames ≈ 113 s, that
puts a poll roughly every 35–40 s in the worst case — no worse than today —
while a plane with several active controllers should see us poll rarely.

### What it needs from the library

`ChannelInfo` does not carry `lifetime`, though `ChannelStatus` does. Same
plumbing gap that `demod_type` and `output_channels` had, and the same fix:
decode it in `decode_status_dict` and pass it through in `discovery.py`.

## What this design refuses to do

It does not reconcile channel state. If a status shows a frequency or a
filter we did not ask for, the keepalive records it and says nothing. Putting
it back is the tuning war Phil warns against, and a controller that "knows
better" than the operator is exactly the thing a shared plane cannot carry.

`ChannelManager.ensure_channels_exist(update_existing=...)` already defaults
to `False` and is reached only from operator-invoked CLI verbs. That default
is load-bearing on a shared plane and should be commented as such, rather
than left looking like an ordinary convenience.

## Testing it

The arrangement that settled the empty-poll question generalises: run two
channels, drive one and leave the other, and let the untouched arm reveal the
timescale. Then stop driving the first and confirm it dies — without that
third arm, survival proves nothing about cause.

For suppression specifically, the case worth building is two controllers on
one SSRC: confirm the second suppresses while the first polls, and — the one
that matters — confirm it resumes promptly when the first goes away.
