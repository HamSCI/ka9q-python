"""
Protocol compatibility test — catches drift between ka9q/types.py
and the ka9q-radio C headers (status.h, rtp.h, radio.h, window.h).

Skipped automatically when ka9q-radio source is not available on disk.
"""

import contextlib
import shutil
import subprocess
import sys
import tempfile
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


def _read_pin() -> Optional[str]:
    """The ka9q-radio commit types.py was generated from."""
    compat_file = PROJECT_ROOT / "ka9q_radio_compat"
    if not compat_file.exists():
        return None
    for line in compat_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return line
    return None


@contextlib.contextmanager
def _tree_at_pin(repo: Path, pin: str):
    """A read-only worktree of `repo` at `pin`, removed afterwards.

    ⛔ The headers must be read AT THE PIN, never at whatever branch the
    checkout happens to sit on.  On 2026-09-23 the local ka9q-radio `main`
    was a 2-month-old fork snapshot (2026-07-15, 490 commits behind
    upstream) while the pin was current, so comparing against HEAD reported
    four bogus DemodType/WindowType drifts against a types.py that was
    perfectly in sync with its pin.  A drift check that reads the wrong
    revision manufactures its own alarm.
    """
    tmp = Path(tempfile.mkdtemp(prefix="ka9q-radio-at-pin-"))
    wt = tmp / "tree"
    add = subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-q", "--detach", str(wt), pin],
        capture_output=True, text=True,
    )
    try:
        yield (wt if add.returncode == 0 else None)
    finally:
        subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", str(wt)],
                       capture_output=True, text=True)
        shutil.rmtree(tmp, ignore_errors=True)


@pytest.mark.skipif(
    ka9q_radio_path is None,
    reason="ka9q-radio source tree not found at ../ka9q-radio",
)
def test_types_match_status_h():
    """types.py must match the C headers AT THE PINNED COMMIT.

    Not at the checkout's HEAD: the pin is what types.py was generated
    from and what clients were validated against, while HEAD is whatever
    branch someone last left the reference checkout on.
    """
    pin = _read_pin()
    if not pin:
        pytest.skip("no ka9q_radio_compat pin to validate against")
    with _tree_at_pin(ka9q_radio_path, pin) as tree:
        if tree is None:
            pytest.skip(f"pinned commit {pin[:12]} not present in the local checkout")
        result = subprocess.run(
            [sys.executable, str(SYNC_SCRIPT), "--check",
             "--ka9q-radio", str(tree)],
            capture_output=True,
            text=True,
            cwd=str(PROJECT_ROOT),
        )
        assert result.returncode == 0, (
            f"types.py is out of sync with ka9q-radio headers at pin {pin[:12]}:\n"
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

    # "Reachable HERE" means reachable from ANY ref in this repository --
    # not from HEAD.  Asserting against HEAD couples the check to whichever
    # branch someone last checked out: on 2026-09-23 the pin sat on
    # ka9q/main while local main was a stale July fork snapshot, and the
    # test failed on a types.py that was demonstrably in sync with its pin.
    exists = subprocess.run(
        ["git", "-C", str(ka9q_radio_path), "cat-file", "-e", f"{pinned}^{{commit}}"],
        capture_output=True, text=True,
    )
    assert exists.returncode == 0, (
        f"pinned commit {pinned[:12]} is not present in the local "
        f"ka9q-radio checkout — types.py cannot have been validated against it"
    )
    refs = subprocess.run(
        ["git", "-C", str(ka9q_radio_path), "for-each-ref", "--contains", pinned,
         "--format=%(refname)"],
        capture_output=True, text=True,
    )
    assert refs.stdout.strip(), (
        f"pinned commit {pinned[:12]} is present but unreachable from any ref "
        f"in the local ka9q-radio checkout — a future gc would drop it"
    )
