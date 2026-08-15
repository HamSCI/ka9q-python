"""The compat pin must be reachable from the repo sigmond will clone.

`sigmond bin/smd:_install_radiod_native()` reads `ka9q_radio_compat`,
clones UPSTREAM ka9q/ka9q-radio, and checks that commit out.  So a pin
that exists only on a fork is not merely wrong — it is unbuildable.

That happened on 2026-08-15: `sync_types.py --apply` was run while the
local ka9q-radio checkout sat on a merge branch pushed only to a personal
fork, so the pin became a fork-only commit and the golden template build
died four hours later with

    ka9q-radio clone failed: fatal: unable to read tree (7fca458a...)
    radiod: radiod binary not found (native build missing)

The hazard had been written down that morning and shipped anyway.  A
paragraph did not hold; a check does.
"""
import subprocess

import pytest

from scripts.sync_types import is_reachable_from_remote


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@pytest.fixture
def upstream_and_fork(tmp_path):
    """An 'upstream' repo and a clone carrying one extra local commit."""
    up = tmp_path / "upstream"
    up.mkdir()
    _git("init", "-q", "-b", "main", cwd=up)
    _git("config", "user.email", "t@t", cwd=up)
    _git("config", "user.name", "t", cwd=up)
    (up / "f").write_text("one\n")
    _git("add", "f", cwd=up)
    _git("commit", "-qm", "one", cwd=up)
    upstream_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=up,
                                  capture_output=True, text=True).stdout.strip()

    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(up), str(work)], check=True)
    _git("config", "user.email", "t@t", cwd=work)
    _git("config", "user.name", "t", cwd=work)
    (work / "f").write_text("two\n")
    _git("commit", "-qam", "fork-only", cwd=work)
    fork_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=work,
                              capture_output=True, text=True).stdout.strip()
    return work, upstream_sha, fork_sha


def test_an_upstream_commit_is_reachable(upstream_and_fork):
    work, upstream_sha, _ = upstream_and_fork

    assert is_reachable_from_remote(work, upstream_sha, remote="origin") is True


def test_a_fork_only_commit_is_not_reachable(upstream_and_fork):
    """The 2026-08-15 case: HEAD is a commit upstream has never seen."""
    work, _, fork_sha = upstream_and_fork

    assert is_reachable_from_remote(work, fork_sha, remote="origin") is False


def test_a_missing_remote_is_not_silently_treated_as_reachable(tmp_path):
    """Refusing to answer must not read as 'yes'."""
    r = tmp_path / "r"
    r.mkdir()
    _git("init", "-q", "-b", "main", cwd=r)

    assert is_reachable_from_remote(r, "0" * 40, remote="nosuchremote") is False


def test_the_importable_constant_and_the_text_file_agree():
    """`_ka9q_radio_pin()` prefers `ka9q.compat.KA9Q_RADIO_COMMIT` and only
    falls back to the text file.  Fixing one and not the other looks
    fixed and changes nothing — the golden build kept pinning the
    fork-only commit through a second failed run on 2026-08-15 for
    exactly this reason.
    """
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    from ka9q.compat import KA9Q_RADIO_COMMIT

    text = [ln.strip() for ln in
            (root / "ka9q_radio_compat").read_text().splitlines()
            if ln.strip() and not ln.startswith("#")][0]

    assert KA9Q_RADIO_COMMIT == text, (
        "ka9q/compat.py and ka9q_radio_compat disagree — sync_types.py "
        "--apply writes both; hand-edits must too")
