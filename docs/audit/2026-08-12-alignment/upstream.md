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
