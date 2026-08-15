"""
Protocol compatibility test — catches drift between ka9q/types.py
and the ka9q-radio C headers (status.h, rtp.h, radio.h, window.h).

Skipped automatically when ka9q-radio source is not available on disk.
"""

import subprocess
import sys
from pathlib import Path
from typing import Optional

import pytest

# Resolve paths relative to this file
PROJECT_ROOT = Path(__file__).resolve().parent.parent
SYNC_SCRIPT = PROJECT_ROOT / "scripts" / "sync_types.py"
KA9Q_RADIO_DEFAULT = PROJECT_ROOT.parent / "ka9q-radio"


def _find_ka9q_radio() -> Optional[Path]:
    """Return the ka9q-radio source path, or None if unavailable."""
    # Check default sibling location
    if (KA9Q_RADIO_DEFAULT / "src" / "status.h").exists():
        return KA9Q_RADIO_DEFAULT
    return None


ka9q_radio_path = _find_ka9q_radio()


@pytest.mark.skipif(
    ka9q_radio_path is None,
    reason="ka9q-radio source tree not found at ../ka9q-radio",
)
def test_types_match_status_h():
    """types.py must match the ka9q-radio C headers exactly."""
    result = subprocess.run(
        [sys.executable, str(SYNC_SCRIPT), "--check",
         "--ka9q-radio", str(ka9q_radio_path)],
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
    )
    assert result.returncode == 0, (
        f"types.py is out of sync with ka9q-radio headers:\n"
        f"{result.stdout}\n{result.stderr}"
    )


@pytest.mark.skipif(
    ka9q_radio_path is None,
    reason="ka9q-radio source tree not found at ../ka9q-radio",
)
def test_compat_pin_is_contained_in_the_local_ka9q_radio_checkout():
    """The pin must be CONTAINED in the local checkout, not equal to HEAD.

    The old assertion was pin == HEAD.  2026-08-15 showed that to be the
    wrong invariant twice over: a working checkout may legitimately sit on
    a fork superset (our merge = the pinned upstream release + two fork
    commits), and demanding equality pushed the pin to a fork-only commit
    that sigmond could not clone — killing the golden image build with
    "unable to read tree" and no radiod.

    What actually matters is that the commit the clients were validated
    against is reachable here, AND (checked in
    test_compat_pin_reachability) reachable from upstream.
    """
    compat_file = PROJECT_ROOT / "ka9q_radio_compat"
    assert compat_file.exists(), "ka9q_radio_compat pin file is missing"

    pinned = None
    for line in compat_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            pinned = line
            break
    assert pinned, "ka9q_radio_compat contains no commit hash"

    if not ka9q_radio_path.exists():
        pytest.skip("ka9q-radio checkout not present")

    contains = subprocess.run(
        ["git", "-C", str(ka9q_radio_path), "merge-base",
         "--is-ancestor", pinned, "HEAD"],
        capture_output=True, text=True,
    )
    assert contains.returncode == 0, (
        f"pinned commit {pinned[:12]} is not contained in the local "
        f"ka9q-radio checkout — types.py cannot have been validated against it"
    )
