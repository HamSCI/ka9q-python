# Upstream Reference Freeze — ka9q-radio Alignment Audit

**Audit date:** 2026-08-12

**Pin commit (basis of audit):** `14d780af624e821941708bd0d64fd895a0c80a2a`

**AUDIT_HEAD (audited upstream sha):** `cedec349f7b4212078de3e007d142b4c64d36546`

This entire audit is frozen to `cedec349f7b4212078de3e007d142b4c64d36546` (2026.08.12-1-trixie1). All Tasks 2–4 and 9 must use this exact sha, not a moving branch.

**Commit count:** 149 commits from pin to AUDIT_HEAD

---

## Drift Watcher Verdict

**Severity:** FAIL (stream-critical change)

**Summary:** 1 stream-critical change — RTP delivery at risk

**Per-field detail (JSON):**

```json
{
  "severity": "fail",
  "pin": "14d780af624e821941708bd0d64fd895a0c80a2a",
  "upstream_ref": "origin/main",
  "upstream_sha": "cedec349f7b4212078de3e007d142b4c64d36546",
  "header_deltas": [
    {
      "header": "src/radio.h",
      "enum": "DemodType",
      "severity": "fail",
      "changes": [
        {
          "kind": "added",
          "name": "IDLE_DEMOD",
          "pin": null,
          "head": 5,
          "severity": "warn",
          "reason": "new field upstream — review for client exposure"
        },
        {
          "kind": "value_changed",
          "name": "N_DEMOD",
          "pin": 5,
          "head": 6,
          "severity": "fail",
          "reason": "DemodType value shift — clients decode wrong format"
        }
      ]
    }
  ],
  "summary": "1 stream-critical change — RTP delivery at risk",
  "error": null
}
```

### Key Changes

**src/radio.h (DemodType):**
- `+IDLE_DEMOD = 5` — new field upstream (advisory, review for client exposure)
- `~N_DEMOD: 5 → 6` — **CRITICAL** value shift; clients decode wrong format if not updated

### Anomalies

The N_DEMOD enum value shift (5 → 6) is a breaking change. Any client (e.g., ka9q-python, sigmond-suite receivers) that hard-codes or compares against the old enum value will receive misinterpreted demod type fields from packets encoded with the new value.

**Task 2–4 coordination required:** Before advancing the pin past this commit, ka9q-python's `StatusType.N_DEMOD` constant and all downstream consumers (hf-timestd, wspr-recorder, psk-recorder, hfdl-recorder, codar-sounder, any code matching `from ka9q.types import StatusType, Encoding`) must be updated in tandem.

---

## Commit classification

Every commit in `14d780af..cedec349` (149 commits, Step 1 of Task 2), classified per the scheme in the task brief. `git show --stat` was checked for all 149; `git show` (full diff) was read for every commit touching `*.h`, `radio.c`, `rtp.c`, `multicast.c`, `status.c`, `radio_status.c`, a demod source (`fm.c`, `linear.c`, `wfm.c`, `spectrum.c`), or a client-side TLV encoder (`control.c`, `tune.c`) — 46 commits in total got a full-diff read; the rest were classified from their `--stat` file list and subject line (debian/config/rules/service/aux packaging, or mechanical driver/build cleanups).

| sha | subject | class | note |
|---|---|---|---|
| `4709d0a8` | remove spurious abs on powers -c arg | internal | powers.c CLI arg fix (drop spurious abs()) + spectrum.c copyright-comment update; no wire/behavior change |
| `641647ac` | Merge branch 'main' of github.com:ka9q/ka9q-radio | packaging | debian/changelog version bump, no source change |
| `fb4863db` | 2026.08.01-1-trixie1 | packaging | debian/changelog version bump, no source change |
| `c2353a4c` | remove unused <uuid/uuid.h> from radio.c | internal | radio.c: remove unused <uuid/uuid.h> include, no uuid_* calls anywhere; build hygiene only |
| `503d2b9f` | drop the uuid-dev build dependency | packaging | packaging/udev/config/service files only: debian/control,docs/INSTALL.md |
| `5ed11fa4` | include <string.h> where string functions are used | internal | add missing <string.h>/<strings.h> includes across 12 driver files to fix implicit-declaration UB; no behavior change |
| `c834f1da` | add $(LDFLAGS) to the driver plugin link rules | internal | src/Makefile: add $(LDFLAGS) to driver plugin link rules; build tweak |
| `1f53abef` | remove unused Avahi headers from avahi.h and fix its include guard | internal | avahi.h: drop unused Avahi dev headers (avahi.c/avahi_browse.c shell out to avahi-* tools) and fix broken include guard |
| `c01f0b9e` | drop the libavahi-client-dev build dependency | packaging | packaging/udev/config/service files only: debian/control,docs/INSTALL.md |
| `ca620bcf` | tighten set_filter, add guards to window routines | internal | filter.c set_filter()/window.c: add M<2 and window_gain==0/non-finite guards, move the impulse-response buffer from a VLA to lmalloc(); defensive hardening and refactor, no change to filter output on valid input |
| `23a7f1f9` | 2026.08.02-1-trixie1 | packaging | debian/changelog version bump, no source change |
| `f0959172` | Merge pull request #236 from techn0mad/fix-avahi-includes | internal | merge of 1f53abef (avahi header cleanup), no unique diff |
| `b72ae73b` | Merge pull request #235 from techn0mad/fix-so-ldflags | internal | merge of c834f1da (LDFLAGS build fix), no unique diff |
| `d2d73560` | Merge branch 'main' into fix-uuid-include | packaging | merge conflict resolution in debian/control removing uuid-dev build-dep, duplicate of 503d2b9f |
| `aa311900` | Merge pull request #234 from techn0mad/fix-uuid-include | internal | merge of c2353a4c (drop uuid.h include), no unique diff |
| `1a974c18` | Merge pull request #233 from techn0mad/fix-string-includes | internal | merge of 5ed11fa4 (string.h includes), no unique diff |
| `adf75d09` | 2026.08.02-2-trixie1 | packaging | debian/changelog version bump, no source change |
| `f6b2ed46` | remove superfluous signal(SIGKILL) calls | internal | ctcss/opusd/opussend/pcmsend/rdsd.c: remove signal(SIGKILL, closedown) calls (SIGKILL cannot be caught, so the call was always a no-op) |
| `9e7c6410` | helper script for udev-driven radiod start | packaging | packaging/udev/config/service files only: aux/Makefile,aux/start-ka9q-radio.in |
| `054755fb` | simple default config files for udev-driven rx888 radiod | packaging | packaging/udev/config/service files only: config/defaults/04b4-00f1.conf,config/defaults/radiod@rx888.conf |
| `60c89f45` | makefile for default radiod config files | packaging | packaging/udev/config/service files only: config/Makefile |
| `bf83a393` | new service files for udev-driven radiod launch | packaging | packaging/udev/config/service files only: aux/start-ka9q-radio,service/ka9q-radio@.service.in,service/radiod@.service.in |
| `da7a2f20` | add service file for udev-driven radiod launch | packaging | packaging/udev/config/service files only: service/Makefile |
| `8c8e1982` | add rule for udev-driven launch of radiod - DISABLED BY DEFAULT | packaging | packaging/udev/config/service files only: rules/71-rx888-autoload.rules-disabled,rules/71-rx888.rules,rules/72-rx888-ka9q.rules,rules/Makefile |
| `605cfa12` | debian directory changes for udev-driven radiod launch | packaging | packaging/udev/config/service files only: debian/ka9q-radio-rx888.install,debian/ka9q-radio.install,debian/ka9q-radio.tmpfiles |
| `bc8c3f26` | add some default config files for rx888 hydra airspy fobos | packaging | packaging/udev/config/service files only: config/defaults/04b4-00f1.conf,config/defaults/16d0-132e.conf,config/defaults/1d50-60a1.conf,config/defaults/airspy.conf,config/defaults/fobos.conf,config/defaults/hydra-note.txt,config/defaults/hydrasdr.conf,debian/ka9q-radio-airspy.install,debian/ka9q-radio-fobos.install,debian/ka9q-radio-hydrasdr.install,debian/ka9q-radio-rx888.install |
| `33ffa04b` | fix file name | packaging | config/defaults: rename hydra-note.txt -> hydrasdr.note (file rename only) |
| `0ba45e32` | add config files for airspyhf | packaging | packaging/udev/config/service files only: config/defaults/03eb-800c.conf,config/defaults/airspyhf.conf,debian/ka9q-radio-airspyhf.install |
| `9e82f004` | udev rule for activating airspyhf - disabled by default | packaging | packaging/udev/config/service files only: rules/52-airspyhf.rules |
| `76990ed3` | fix generation of start-ka9q-radio script | packaging | packaging/udev/config/service files only: aux/Makefile,aux/start-ka9q-radio |
| `83371753` | add airspy radiod udev launch rule - disabled by default | packaging | packaging/udev/config/service files only: rules/51-airspy.rules |
| `7c14e132` | add udev rule for launching radiod for hydrasdr - disabled by default | packaging | packaging/udev/config/service files only: rules/51-hydrasdr.rules |
| `5fbc6e9a` | add ka9q-radio radiod launch rules - disabled by default | packaging | packaging/udev/config/service files only: rules/54-bladerf.rules |
| `78cdcb61` | add hackrf and fobos radiod rules - disabled by default | packaging | packaging/udev/config/service files only: rules/53-airspy-ka9q.rules,rules/66-hackrf.rules,rules/73-fobos-sdr.rules,rules/74-fobos-sdr-ka9q.rules |
| `d925d2a0` | update rules makefile | packaging | packaging/udev/config/service files only: rules/Makefile |
| `63983da8` | update debian file list | packaging | packaging/udev/config/service files only: debian/ka9q-radio-airspy.install |
| `6e17ee72` | pass serial number to radiod from start-ka9q-radio | packaging | packaging/udev/config/service files only: aux/start-ka9q-radio.in |
| `d31475a2` | hydrasdr driver can get serial number from -s option on radio command line | internal | hydrasdr.c: accept serial number via -s radiod command-line option; hardware-selection feature, not protocol-visible |
| `cbfa8664` | fix hydrasdr serial parsing | internal | hydrasdr.c: fix serial-number parsing bug from d31475a2 |
| `b81334a9` | airspy serial number parse in udev | packaging | packaging/udev/config/service files only: rules/51-airspy.rules |
| `f1acc322` | echo radiod start command | packaging | packaging/udev/config/service files only: aux/start-ka9q-radio.in |
| `6374ce48` | change syntax of radiod start command | packaging | packaging/udev/config/service files only: aux/start-ka9q-radio.in |
| `6fbe5375` | use safe version of hydrasdr serial | packaging | packaging/udev/config/service files only: rules/51-airspy.rules |
| `c51b3974` | use safe serial number in hydrasdr rule | packaging | packaging/udev/config/service files only: rules/51-hydrasdr.rules |
| `06ad85cd` | accept hydra serial numbers with space or _ | internal | hydrasdr.c: accept hydra serial numbers containing space or underscore |
| `35b52013` | simpler parse of hydra sn | internal | hydrasdr.c: simplify serial-number parsing |
| `e4657044` | move ka9q-radio radiod autolaunch rules to common file | packaging | packaging/udev/config/service files only: debian/ka9q-radio.install,rules/51-airspy.rules,rules/51-hydrasdr.rules,rules/52-airspyhf.rules,rules/54-bladerf.rules,rules/66-hackrf.rules,rules/68-funcube-dongle-proplus.rules,rules/68-funcube-dongle.rules,rules/70-rx888-boot.rules,rules/71-rx888.rules,rules/73-fobos-sdr.rules,rules/90-ka9q-radio-autostart.rules |
| `26db2b8e` | remove empty hackrf rule, install common ka9q-radio launch rule | packaging | packaging/udev/config/service files only: debian/ka9q-radio-hackrf.install,rules/Makefile |
| `4c5fdbb7` | configure udevdir in debian packaging | packaging | packaging/udev/config/service files only: Makefile |
| `796810e6` | move all rules to usr/lib/udev/rules.d in packaged version | packaging | packaging/udev/config/service files only: debian/ka9q-radio.install |
| `687c059d` | 2026.08.03-1-trixie1 | packaging | debian/changelog version bump, no source change |
| `2dd4ab3b` | fix debian packaging for udev launch of radiod | packaging | packaging/udev/config/service files only: aux/Makefile,debian/ka9q-radio-rx888.install,debian/ka9q-radio.install,debian/ka9q-radio.tmpfiles |
| `dfc27ac1` | Merge branch 'main' of github.com:ka9q/ka9q-radio | packaging | debian/changelog version bump, no source change |
| `33cb5a4a` | 2026.08.03-1-trixie2 | packaging | debian/changelog version bump, no source change |
| `f3209417` | modify airspyhf handler to look for serial number on radiod command line | internal | airspyhf.c: look up serial number from the radiod command line, matching hydrasdr/airspy handling |
| `3880417e` | 2026.08.03-2-trixie1 | packaging | debian/changelog version bump, no source change |
| `3fe2b5a9` | fix typo in changelog | packaging | debian/changelog version bump, no source change |
| `9effb7e1` | shuffle directory creation for ka9q-radio configs | packaging | packaging/udev/config/service files only: debian/ka9q-radio.dirs,debian/ka9q-radio.tmpfiles |
| `f2f250bc` | 2026.08.03-2-trixie2 | packaging | debian/changelog version bump, no source change |
| `33643394` | force arg to start-ka9q-radio to lower case | packaging | packaging/udev/config/service files only: aux/start-ka9q-radio.in |
| `b09bbec9` | 2026.08.03-3-trixie1 | packaging | debian/changelog version bump, no source change |
| `cf051d5f` | relax airspy serial number parsing to allow lower case | internal | airspy.c: relax serial-number parsing to allow lower case |
| `2e379e0d` | 2026.08.03-4-trixie1 | packaging | debian/changelog version bump, no source change |
| `89b9592f` | make temp network failures less fatal | behavior | audio.c send_output(): replace abort() on a persistent-looking audio send failure with a suppressed, logged warning that lets radiod keep running; service/ka9q-radio@.service.in also raises StartLimitIntervalSec/RestartSec. Previously a transient network failure could abort() and drop radiod (and its RTP streams) entirely; now radiod stays up through it |
| `e7c1aef8` | 2026.08.03-5-trixie1 | packaging | debian/changelog version bump, no source change |
| `df50a75f` | add more udev autostart examples | packaging | config/defaults: add more udev autostart example config files |
| `868f7328` | add gitignore to aux | packaging | packaging/udev/config/service files only: aux/.gitignore |
| `cecf7bc2` | remove superfluous debian file | packaging | packaging/udev/config/service files only: aux/ka9q-radio.tmpfiles |
| `9e7de808` | debian cleanup | packaging | packaging/udev/config/service files only: debian/ka9q-radio-hydrasdr.install |
| `4041eb82` | fix orphaned debian files | packaging | packaging/udev/config/service files only: debian/ka9q-radio-airspy.install,debian/ka9q-radio-bladerf.install,debian/ka9q-radio-fobos.install,debian/ka9q-radio-hackrf.install,debian/ka9q-radio-hydrasdr.install,debian/ka9q-radio-rx888.install,debian/ka9q-radio.install |
| `1a6d3fe4` | more of the same | packaging | packaging/udev/config/service files only: debian/ka9q-radio-airspyhf.install |
| `561ed2f8` | 2026.08.03-5-trixie2 | packaging | debian/changelog version bump, no source change |
| `6a1ec88a` | add rtlsdr to udev launch, basic hackrf.conf | packaging | packaging/udev/config/service files only: config/defaults/hackrf.conf,rules/90-ka9q-radio-autostart.rules |
| `4335df3d` | udev radiod launch rules for amsat uk funcube dongle - untested | packaging | packaging/udev/config/service files only: config/defaults/04d8-fb31.conf,config/defaults/funcube.conf,rules/90-ka9q-radio-autostart.rules |
| `2d88d408` | 2026.08.04-1-trixie1 | packaging | debian/changelog version bump, no source change |
| `99a19110` | add a few missing files to debian installs | packaging | packaging/udev/config/service files only: debian/ka9q-radio-funcube.install,debian/ka9q-radio-hackrf.install |
| `41c1acd8` | add bare config files for rtlsdr | packaging | packaging/udev/config/service files only: config/defaults/0bda-2832.conf,config/defaults/rtlsdr.conf,debian/ka9q-radio-rtlsdr.install |
| `da9e2943` | 2026.08.05-1-trixie1 | packaging | debian/changelog version bump, no source change |
| `cb999064` | remove double call of libusb_free_device_list on rx888 startup failure | internal | rx888.c: remove a duplicate libusb_free_device_list() call on a startup-failure path; internal cleanup |
| `3c545a87` | Merge branch 'main' of github.com:ka9q/ka9q-radio | packaging | debian/changelog version bump, no source change |
| `f574ad18` | 2026.08.05-2-trixie1 | packaging | debian/changelog version bump, no source change |
| `a1e25b23` | destroy channel status mutex on channel termination | internal | fm/linear/spectrum/wfm.c: destroy chan->status.lock mutex on demod exit to fix a resource leak; internal cleanup |
| `7b116abf` | Merge branch 'main' of github.com:ka9q/ka9q-radio | packaging | debian/changelog version bump, no source change |
| `73707fa8` | restructure channel status init/destroy | internal | radio.c/fm.c/linear.c/spectrum.c/wfm.c: move status-mutex init/destroy out of per-demod code into start_demod/demod_thread; internal restructuring, no observable change |
| `49774ead` | fix start-ka9q-radio to accept manual args without serial numbers | packaging | aux/start-ka9q-radio.in: accept device identities without an embedded serial; radio.c change is comment-only |
| `bde125a7` | 2026.08.05-2-trixie1 | packaging | debian/changelog version bump, no source change |
| `e1d675e6` | earlier lock on slave filter response | internal | filter.c execute_filter_output(): take slave->response_mutex before reading slave->fdomain/response/bins instead of after, closing a race with set_filter()'s hot-swap; internal concurrency fix |
| `b4342e95` | 2026.08.06-1-trixie1 | packaging | debian/changelog version bump, no source change |
| `8bc56ee4` | minor changes to build dependencies | packaging | packaging/udev/config/service files only: debian/control,docs/INSTALL.md |
| `bef0d413` | Merge branch 'main' of github.com:ka9q/ka9q-radio | packaging | debian/changelog version bump, no source change |
| `cbe7cef4` | test for invalid beta values in kaiser windows | internal | window.c make_kaiser()/make_kaiserf(): reject NaN/Inf beta values; input-validation hardening only |
| `a913eb01` | clean and harden fft job queuing | internal | filter.c: harden/simplify FFT job queue (drop free-list, calloc per job, UINT_MAX sentinel); internal robustness, no observable output change |
| `4a17d691` | remove redundant isnan tests | internal | control.c: isnan()+isfinite() -> isfinite() cleanup, same family as 56387a71; no functional change |
| `453712be` | remove redundant isnan tests | internal | airspy.c/airspyhf.c: isnan()+isfinite() -> isfinite() cleanup |
| `561d8ed3` | remove redundant isnan test | internal | bladerf.c: isnan()+isfinite() -> isfinite() cleanup |
| `5eac07a7` | remove redundant isnan test | internal | fobos.c: isnan()+isfinite() -> isfinite() cleanup |
| `56387a71` | remove redundant isnan tests | internal | fm/funcube/hydrasdr/linear/monitor-*/powers/rtlsdr/rx888/sdrplay/setfilt/spectrum/wfm/window.c: isnan()+isfinite() -> isfinite() cleanup (isfinite() already excludes NaN); no functional change |
| `13beb9b0` | const correctness | internal | bladerf/fobos/funcube/hydrasdr/rtlsdr/sig_gen.c: const-correctness cleanup, no functional change |
| `e2b21244` | rename Preset_table for compatibility | internal | control.c: rename Pdict -> Preset_table (module-global) for naming consistency with radio.c; no functional change |
| `9bf17f5a` | arg guard on make_maddr | internal | multicast.c: add NULL/empty-string guard to make_maddr(); defensive hardening only |
| `b957df53` | arg type change | internal | avahi.c/.h: change avahi_start() address arg from int to uint32_t; internal signature cleanup |
| `f825316f` | limit ssrc to 32 bits | status-tlv | control.c send_poll(): change ssrc param from int to uint32_t and switch encode_int()->encode_int32() for OUTPUT_SSRC/COMMAND_TAG; encode_int() sign-extends a plain int to 64 bits before encoding, so an SSRC with bit31 set was previously encoded as an 8-byte value instead of 4; fixes a real TLV wire-encoding bug for high-valued SSRCs |
| `0ee6efc7` | fix minor buffer overrun in set_defaults | internal | modes.c set_defaults(): replace snprintf(chan->name,...,"new chan") with strlcpy() to fix a (harmless, since the literal always fits) buffer-overrun code pattern |
| `e96e6681` | Merge branch 'main' of github.com:ka9q/ka9q-radio | internal | merge of 13beb9b0/e2b21244/9bf17f5a/b957df53/f825316f branch, no unique diff |
| `fc7afa01` | fix accidental extension of ssrc width in channel list request | status-tlv | control.c: suffix the all-SSRCs poll constant as 0xffffffffU so it is unsigned before send_poll(uint32_t) is called; closes the same sign-extension hole as f825316f for the channel-list-request broadcast poll |
| `21213da9` | avoid dB underflow in rx888 agc | behavior | rx888.c agc_rx888(): skip the AGC update this cycle when frontend->if_power==0 instead of computing power2dB(0)=-Inf; changes rx888 AGC control-loop behavior at startup/silence |
| `356e1890` | Merge branch 'main' of github.com:ka9q/ka9q-radio | internal | merge of e96e6681 branch into 21213da9, no unique diff |
| `9c2e3ce9` | major rewrite of initialization and session creation | behavior | radio.c/modes.c/radio_status.c: major rewrite of loadconfig()/set_defaults()/dynamic channel creation - new per-channel 'advertise' and 'data' config keys, reordered avahi advertise timing, new CHANNEL_STARTING/RUNNING state handling in radio_status.c; changes radiod startup/dynamic-channel-creation behavior observable by clients (mDNS advertising timing, first-status-response flow) though it does not alter the TLV wire format itself |
| `972f538f` | remove accidental adds | internal | remove accidentally-committed si5351-64.c/.h and binary (dead experimental code), never built |
| `db9da8ac` | remove garbage file | internal | remove accidentally-committed binary src/st5OeTWP |
| `5d43aade` | remove more junk files | internal | remove accidentally-committed src/sitest and src/sitest.c |
| `e8397a35` | more garbage files | internal | remove accidentally-committed src/gauss-aes.c |
| `3be1d4b7` | try to resolve deadlock between close_chan and lookup_or_create_chan() | internal | radio.c/radio.h: make close_chan() static, add chan_t typedef, first attempt at fixing close_chan/lookup_or_create_chan deadlock; internal concurrency fix |
| `16e186d2` | try to resolve deadlock between close_chan and lookup_or_create_chan() | internal | radio.c: simplify close_chan() mutex unlock/destroy (drop redundant err captures); internal, continuation of 3be1d4b7 |
| `392a9962` | better race avoidance | internal | radio.c/radio_status.c: rework channel-state locking around Channel_list_mutex vs chan->status.lock to close a race in lookup_or_create_chan()/close_chan(); internal concurrency fix, no wire-format change |
| `f4ea1b1c` | better race avoidance | internal | radio.c: fix CHANNEL_ACTIVE->CHANNEL_RUNNING typo left over from 392a9962; internal |
| `546ce45a` | its getting late... | internal | radio.c: fix (void)err; -> (void)err; typo from 392a9962; internal |
| `1ecfa87c` | assert on fft buffer pointer | internal | fallocate.c/filter.c: add assert on FFT buffer pointer; defensive hardening only |
| `e5df6204` | comment | internal | radio.c: comment-only change |
| `c774a6ac` | avoid divide by zero in fm time constant presets | internal | modes.c loadpreset(): guard fm.rate time-constant calc against tc==0 divide-by-zero; edge-case hardening |
| `e12613ba` | restore some isnan checks to avoid fp exceptions on uninitialized floats when debugging | internal | radio_status.c decode_radio_commands()/encode_radio_status(): restore redundant isnan() checks alongside isfinite() for debug-build clarity; no behavioral difference (isfinite() already excludes NaN) |
| `26467b91` | belt and suspenders test for uninitialized remainder in downconvert | internal | radio.c downconvert(): add redundant isnan(remainder) startup-detection test; belt-and-suspenders, no behavior change |
| `e0fe29b2` | allow description to be set in hardware section | behavior | radio.c loadconfig(): allow 'description' to be set in [hardware] section (parsed after it, overwriting [global]) and move avahi advertise call to after hardware setup so it picks up the hardware-set description; changes mDNS-advertised service description, which ka9q-python's discovery.py resolves |
| `c91c7875` | No preset default in receiver sections so the global preset can become the default | behavior | radio.c process_section(): drop hardcoded 'am' preset default per receiver section so the [global] preset becomes the effective default; changes which demod/preset a dynamically-created channel gets when a section omits 'preset' |
| `79ef7584` | move lmalloc to filter.c, add some halfhearted checks and messages for alloc failures. Hard to get excited about them with 64-bit virtual memory | internal | filter.c/misc.c/misc.h: move lmalloc() from misc.c to filter.c (made static) and add malloc-failure error paths; internal hardening, behavior only differs under OOM |
| `bbc14c10` | send response during demod exit/restart if pending | behavior | fm/linear/spectrum/wfm.c: call response() after the demod loop exits so a pending command response is sent even when the demod is about to restart/exit; previously a response could be silently dropped on restart, which matters to ka9q-python's control.py request/response flow |
| `cefab072` | dont flush command queue when restarting a demod | behavior | fm/linear/spectrum/radio.c/wfm.c: stop flushing chan->commands[] queue in each demod's per-instance cleanup so queued commands survive a demod restart and are only freed in close_chan(); previously a command arriving right as a demod restarted could be lost |
| `82ffb925` | use fma for compute_tuning remainder | internal | radio.c compute_tuning(): use fma() for the remainder calculation and add isnan/isfinite asserts; numerically equivalent to the old subtraction on this hardware, no observable format change |
| `b9109fc5` | enable fma use: fp-contract=fast | internal | src/Makefile: add -ffp-contract=fast to enable fma; build/codegen tweak (already relied on by 82ffb925) |
| `f6da6040` | make .h files self contained with includes | internal | add missing standard-library includes (stdint.h/stdbool.h/stddef.h) to avahi.h, ax25.h, import.h, multicast.h, rtp.h, rx888.h, window.h so each header is self-contained; build hygiene only, no declarations changed |
| `4a6273d6` | more includes | internal | monitor.h: add missing includes (stddef/stdbool/stdint/sys-socket/pthread/complex/math); build hygiene only |
| `3a43f173` | some more includes | internal | monitor.h: add more missing includes (opus/portaudio/assert/radio.h); build hygiene only |
| `fd5709e6` | move _GNU_SOURCE from sources to c flags | internal | src/Makefile + ~70 .c files: move _GNU_SOURCE from a per-file #define to a compiler flag; mechanical build cleanup, no behavior change |
| `ed896ae8` | introduce chan_t typedef | internal | mechanical `struct channel` -> `chan_t` typedef rename across audio.c/control.c/decode_status.c/fm.c/linear.c/modes.c/monitor-data.c/monitor.h/pcmrecord.c/radio.c/radio.h/radio_status.c/spectrum.c/wd-record.c/wfm.c; no functional change |
| `ccab466c` | introduce sess_t typedef in monitor | internal | monitor-data/monitor-display/monitor.c/monitor.h: mechanical `struct session` -> `sess_t` typedef rename; no functional change |
| `414c4b33` | gratuiously correct modulo inline | internal | filter.c: cosmetic rewrite of an inline modulo helper, author labels it gratuitous; no functional change |
| `34a887b2` | replace union-based type punning with memcpy | internal | status.c encode_float/encode_double/encode_vector/decode_float/decode_double: replace union-based type punning with memcpy() to avoid UB; produces byte-for-byte identical TLV output, no wire-format change |
| `e25f02cb` | comment | internal | monitor.c: comment-only change |
| `51a177fc` | mostly gratuitous zeroing of pointers in channel structure when setting defaults | internal | modes.c set_defaults(): explicitly zero a few pointer fields the author labels mostly-gratuitous (calloc'd storage is already zero); no functional change |
| `2c8abd5f` | some not-really-necessary null pointer checks suggested by a histrionic AI | internal | hid-libusb.c/misc.c/multicast.c/radio.c: add a few redundant NULL checks after calloc()/assert(); defensive hardening only |
| `db86b3f7` | emit lmalloc fail message before aborting | internal | filter.c/Makefile: print an error message before aborting on lmalloc() failure; diagnostics only |
| `6107680d` | abort pcmrecord when out of disk space | internal | pcmrecord.c: abort the (client-side) pcmrecord utility when disk space runs out; not part of radiod or the wire protocol |
| `ed4098d5` | abort pcmrecord if /dev/null cant be opened | internal | pcmrecord.c: abort pcmrecord if /dev/null can't be opened; same as 6107680d |
| `1c0a4231` | 2026.08.10-1-trixie1 | packaging | debian/changelog version bump, no source change |
| `ce4fcdd9` | ensure 32 bit command fields arent sign extended to 64 | status-tlv | tune.c: switch encode_int()->encode_int32() for COMMAND_TAG/OUTPUT_SSRC/LIFETIME in the tune utility's command encoder; same sign-extension fix as f825316f/fc7afa01 applied to a second client |
| `9d3bd08e` | have demods return -1 when exiting, 0 when restarting | behavior | fm/linear/spectrum/wfm.c: demod entry points now return -1 (terminate) vs 0 (restart) based on chan->demod_type == INVALID_DEMOD instead of always returning 0; fixes whether an idle-timed-out dynamic channel actually gets torn down by demod_thread's restart loop, changing observable channel-lifecycle behavior |
| `5e325c0c` | add idle demod to packet dissector | packaging | share/ka9q_ctl.lua (Wireshark dissector): lower-case demod names and add the new 'idle' entry to match modes.c/654fda5e; dev-tooling update, not radiod/runtime code |
| `654fda5e` | More startup restructuring/simplification add idle demod when there's no default mode, allow demods to read first commands | status-tlv | radio.h enum demod_type: inserts IDLE_DEMOD before N_DEMOD (LINEAR=0,FM=1,WFM=2,SPECT=3,SPECT2=4,IDLE=5,N_DEMOD=6, was N_DEMOD=5) and adds INVALID_DEMOD=-1; radio_status.c encode_radio_status() sends chan->demod_type as DEMOD_TYPE on every status packet, so this shifts the wire value of N_DEMOD and adds a new demod-type value clients must recognize. Also restructures modes.c set_defaults()/loadpreset() and radio.c channel startup so a dynamic channel starts in IDLE_DEMOD and reads its first command before running the real demod (default demod changed from LINEAR_DEMOD to IDLE_DEMOD). This is the commit the drift watcher flagged FAIL on. |
| `cedec349` | 2026.08.12-1-trixie1 | packaging | debian/changelog version bump, no source change |

### Totals

| class | count |
|---|---|
| payload-rtp | 0 |
| status-tlv | 4 |
| capability | 0 |
| behavior | 8 |
| internal | 66 |
| packaging | 71 |
| **total** | **149** |

Sum of per-class counts = 149, matching the Step 1 commit count of 149.

### Expansions — payload-rtp / status-tlv / capability commits

No commit in this range touches the RTP payload itself (packet framing, sample encoding, timestamps) — 0 commits classed `payload-rtp`. No commit adds a new capability ka9q-python could newly expose without also being better classed as a protocol change — 0 commits classed `capability`. Four commits change the status/command TLV contract:

#### `654fda5e` — More startup restructuring/simplification (add idle demod...)

`src/radio.h`'s `enum demod_type` gains `INVALID_DEMOD = -1` as a sentinel and inserts
`IDLE_DEMOD` immediately before the terminal `N_DEMOD` marker, so the wire values become
`LINEAR_DEMOD=0, FM_DEMOD=1, WFM_DEMOD=2, SPECT_DEMOD=3, SPECT2_DEMOD=4, IDLE_DEMOD=5,
N_DEMOD=6` (previously `N_DEMOD=5`, with no value 5 in use). `radio_status.c`'s
`encode_radio_status()` sends `chan->demod_type` as the `DEMOD_TYPE` status field on every
status packet (`src/radio_status.c:763`), so `N_DEMOD`'s value shift and the new `IDLE_DEMOD`
value are genuinely on the wire, not just internal bookkeeping — this is the exact change the
Task-1 drift watcher flagged FAIL on. The same commit also restructures `modes.c`'s
`set_defaults()`/`loadpreset()` and `radio.c`'s dynamic-channel-creation path so a newly created
channel starts in `IDLE_DEMOD` (the new `DEFAULT_DEMOD`, replacing `LINEAR_DEMOD`) and processes
its first command before the real demodulator starts, which is a `behavior` change riding along
with the `status-tlv` change; the class is set to `status-tlv` because the enum-value shift is
the change that breaks unpatched clients.

#### `f825316f` — limit ssrc to 32 bits

`control.c`'s `send_poll()` took `ssrc` as a plain `int` and encoded it with `encode_int()`,
which sign-extends its argument to 64 bits before calling `encode_int64()`. Any SSRC with bit 31
set (i.e., the upper half of the 32-bit SSRC space, which `allocate_ssrc()`-style hashes can
produce) was therefore encoded as an 8-byte big-endian value instead of the correct 4 bytes. The
fix changes the parameter to `uint32_t` and switches to `encode_int32()` for both `OUTPUT_SSRC`
and `COMMAND_TAG`, so the field is always encoded at its correct width.

#### `fc7afa01` — fix accidental extension of ssrc width in channel list request

Companion fix to `f825316f`: the broadcast "list all channels" poll used the bare literal
`0xffffffff` (an `int`, and on this constant, negative) as the `ssrc` argument to `send_poll()`.
Suffixing it `0xffffffffU` makes it unsigned before `send_poll(uint32_t)` sees it, closing the
same sign-extension hole for the one call site that intentionally uses the reserved
all-ones SSRC.

#### `ce4fcdd9` — ensure 32 bit command fields arent sign extended to 64

Same bug, third call site: `tune.c`'s command encoder used `encode_int()` for `COMMAND_TAG`,
`OUTPUT_SSRC`, and `LIFETIME`. Switching to `encode_int32()` fixes those three fields for the
`tune` utility. Taken together, `f825316f`/`fc7afa01`/`ce4fcdd9` reveal a real
`encode_int()`-sign-extension footgun in ka9q-radio's own client tools; it is worth an explicit
check that ka9q-python's own TLV encoder (`control.py`) does not have the same pattern for SSRC
or tag fields, since it implements this encoding independently rather than by linking `status.c`.

### Notable `behavior`-class commits

Not required by the classification scheme to be expanded, but flagged here because they touch things ka9q-python callers rely on:

- `bbc14c10` / `cefab072` — a command sent right as a demod is restarting (e.g. a preset change)
  used to be able to silently lose its response and/or be dropped from the command queue;
  `control.py`'s request/response and retry logic assumes commands get a response, so this
  removes a source of spurious retries/timeouts against current radiod.
- `9c2e3ce9` / `654fda5e` — dynamic-channel creation is restructured (`CHANNEL_STARTING` /
  `CHANNEL_RUNNING` states, idle-then-first-command startup); the *timing* of the first status
  response to a newly created SSRC changes, though the TLV fields themselves don't.
- `e0fe29b2` — the mDNS-advertised service description can now come from `[hardware]`, which
  `discovery.py`'s mDNS resolution depends on for `-status.local` name formation.
- `c91c7875` — a receiver `[...]` section with no explicit `preset =` now inherits the `[global]`
  preset instead of defaulting to `am`; only affects statically-configured (radiod.conf) sections,
  not SSRCs ka9q-python creates dynamically via TLV, which always specify a preset.

### Commits that were hard to classify

- **`654fda5e`** — bundles a `status-tlv` enum shift with a `behavior` change to channel-startup
  sequencing in one commit. Resolved by classing it `status-tlv` (the breaking change dominates)
  and noting the behavior change in its expansion above, per the brief's instruction to make sure
  this commit's class reflects the N_DEMOD shift.
- **`9c2e3ce9`** ("major rewrite of initialization and session creation") — the largest commit in
  the range, touching config parsing, avahi advertising, and dynamic-channel creation together.
  It adds no new TLV fields and doesn't change `DEMOD_TYPE`/`N_DEMOD` encoding, so it isn't
  `status-tlv`; it doesn't add anything ka9q-python could expose via the control protocol (its new
  config keys are radiod-config-file-only), so it isn't `capability`. Resolved as `behavior`
  because it changes observable startup/avahi/dynamic-creation timing.
- **`89b9592f`** ("make temp network failures less fatal") — bundles a `service/*.service.in`
  restart-timing tweak (packaging) with an `audio.c` change that replaces an `abort()` on a
  persistent send failure with a suppressed warning (a real availability/behavior change: radiod
  no longer crashes-and-restarts on a transient network hiccup). Resolved as `behavior` since
  that's the change with actual client impact; the service-file tweak is incidental.
- **Trivial merge commits** (e.g. `641647ac`, `f0959172`, `e96e6681`, `356e1890`) — these appear
  in `git log --reverse 14d780af..cedec349` as ordinary commits, but `git show <sha>` on each
  shows no diff of their own (their content is already captured by the commits they merge, which
  are separately present in the range). Resolved by classing them `internal` (or `packaging` for
  the one, `d2d73560`, whose merge *did* carry a real conflict-resolution diff in
  `debian/control`) rather than double-counting the underlying changes.