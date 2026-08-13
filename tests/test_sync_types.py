"""Regression tests for scripts/sync_types.py's C-enum parser (audit F10).

The member-value regex required (\\d+), so a negative literal like
upstream's ``INVALID_DEMOD = -1`` failed to match and was silently
skipped — by sync_types.py AND check_upstream_drift.py (which imports
parse_c_enum), blinding the drift safety net to negative sentinels."""

import sys
from pathlib import Path

# Make scripts/ importable (same pattern as tests/test_upstream_drift.py)
SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from sync_types import parse_c_enum  # noqa: E402

HEADER = """
enum demod_type {
  INVALID_DEMOD = -1, // used as sentinel
  LINEAR_DEMOD = 0, // Linear demodulation
  FM_DEMOD, // Frequency demodulation
  N_DEMOD // Dummy equal to number of valid entries
};
"""


def test_negative_literal_is_parsed():
    entries = parse_c_enum(HEADER, "demod_type")
    assert ("INVALID_DEMOD", -1, "used as sentinel") in entries


def test_members_after_negative_keep_correct_values():
    values = {name: value
              for name, value, _ in parse_c_enum(HEADER, "demod_type")}
    assert values == {
        "INVALID_DEMOD": -1,
        "LINEAR_DEMOD": 0,
        "FM_DEMOD": 1,
        "N_DEMOD": 2,
    }
