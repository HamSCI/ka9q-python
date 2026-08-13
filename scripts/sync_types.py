#!/usr/bin/env python3
"""
Synchronize ka9q/types.py with ka9q-radio C header files.

Parses enum status_type from status.h, enum encoding from rtp.h,
enum demod_type from radio.h, and enum window_type from window.h,
then either checks for drift or regenerates types.py.

Usage:
    python scripts/sync_types.py --check   # exit non-zero if drift detected
    python scripts/sync_types.py --apply   # regenerate types.py and update pin
    python scripts/sync_types.py --diff    # show what would change (dry run)

Options:
    --ka9q-radio PATH   Path to ka9q-radio source tree
                        (default: ../ka9q-radio)
"""

import argparse
import re
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

# ---------------------------------------------------------------------------
# Paths (relative to this script's location)
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
TYPES_PY = PROJECT_ROOT / "ka9q" / "types.py"
COMPAT_PY = PROJECT_ROOT / "ka9q" / "compat.py"
COMPAT_FILE = PROJECT_ROOT / "ka9q_radio_compat"


# ---------------------------------------------------------------------------
# C enum parser
# ---------------------------------------------------------------------------
def parse_c_enum(header_text: str, enum_name: str) -> List[Tuple[str, int, str]]:
    """
    Parse a C enum from header text.

    Returns list of (name, value, comment) tuples in declaration order.
    """
    # Match the enum block — handles both "enum foo {" and "enum foo\n{"
    pattern = rf"enum\s+{enum_name}\s*\{{(.*?)\}}"
    match = re.search(pattern, header_text, re.DOTALL)
    if not match:
        raise ValueError(f"Could not find 'enum {enum_name}' in header text")

    body = match.group(1)
    entries: List[Tuple[str, int, str]] = []
    value = 0

    for line in body.split("\n"):
        line = line.strip()
        if not line or line.startswith("//") or line.startswith("/*"):
            continue

        # Match:  NAME,  NAME = 3,  NAME = 3, // comment   NAME, // comment
        m = re.match(
            r"([A-Z][A-Z0-9_]*)\s*(?:=\s*(-?\d+))?\s*,?\s*(?://\s*(.*))?\s*$",
            line,
        )
        if not m:
            continue

        name = m.group(1)
        if m.group(2) is not None:
            value = int(m.group(2))
        comment = (m.group(3) or "").strip()

        entries.append((name, value, comment))
        value += 1

    return entries


def get_git_commit(repo_path: Path) -> str:
    """Return the full SHA-1 of HEAD in the given repo."""
    result = subprocess.run(
        ["git", "-C", str(repo_path), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


# ---------------------------------------------------------------------------
# types.py parser — reads the CURRENT file to learn what Python already has
# ---------------------------------------------------------------------------
def parse_types_py() -> Tuple[Dict[str, int], Dict[str, int], Dict[str, int], Dict[str, int]]:
    """
    Parse the existing types.py and return:
        (status_entries, encoding_entries, demod_entries, window_entries)
    Each is {name: value}.  Aliases (F32 = F32LE) are excluded.
    """
    # We import the module directly so we get the truth including aliases
    import importlib.util

    spec = importlib.util.spec_from_file_location("_types", str(TYPES_PY))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    def _class_entries(cls) -> Dict[str, int]:
        seen_values: Set[int] = set()
        entries: Dict[str, int] = {}
        # First pass: collect non-alias attributes (declared first wins)
        for attr in sorted(dir(cls)):
            if attr.startswith("_"):
                continue
            val = getattr(cls, attr)
            if not isinstance(val, int):
                continue
            entries[attr] = val
        return entries

    return (
        _class_entries(mod.StatusType),
        _class_entries(mod.Encoding),
        _class_entries(mod.DemodType),
        _class_entries(mod.WindowType),
    )


# ---------------------------------------------------------------------------
# Code generator
# ---------------------------------------------------------------------------

# Comments extracted from the C headers, keyed by enum value ranges.
# The generator preserves the section-comment structure of the original
# types.py while replacing the constant definitions.

STATUS_SECTIONS = [
    (None, None, None),           # section header emitted inline
]


def generate_types_py(
    status_entries: List[Tuple[str, int, str]],
    encoding_entries: List[Tuple[str, int, str]],
    demod_entries: List[Tuple[str, int, str]],
    window_entries: List[Tuple[str, int, str]],
    commit_hash: str,
) -> str:
    """Generate the full contents of ka9q/types.py from parsed enums."""

    lines: List[str] = []

    lines.append('"""')
    lines.append("ka9q-radio protocol types and constants")
    lines.append("")
    lines.append("Auto-generated by scripts/sync_types.py from ka9q-radio C headers.")
    lines.append(f"Validated against ka9q-radio commit: {commit_hash[:12]}")
    lines.append("")
    lines.append("DO NOT EDIT MANUALLY — run: python scripts/sync_types.py --apply")
    lines.append('"""')
    lines.append("")
    lines.append("")
    lines.append("class StatusType:")
    lines.append('    """TLV type identifiers for radiod status/control protocol"""')
    lines.append("")

    for name, value, comment in status_entries:
        suffix = f"  # {comment}" if comment else ""
        lines.append(f"    {name} = {value}{suffix}")

    lines.append("")
    lines.append("")
    lines.append("# Command packet type")
    lines.append("CMD = 1")
    lines.append("")
    lines.append("")
    lines.append("# Encoding types — auto-generated from ka9q-radio/src/rtp.h")
    lines.append("class Encoding:")
    lines.append(
        '    """Output encoding types — values must match '
        'ka9q-radio/src/rtp.h enum encoding"""'
    )
    lines.append("")

    for name, value, comment in encoding_entries:
        suffix = f"  # {comment}" if comment else ""
        lines.append(f"    {name} = {value}{suffix}")

    # Backward-compat aliases
    lines.append("")
    lines.append("    # Backward compatibility aliases")
    enc_names = {n for n, _, _ in encoding_entries}
    if "F32LE" in enc_names:
        lines.append("    F32 = F32LE")
    if "F16LE" in enc_names:
        lines.append("    F16 = F16LE")

    # Demodulator types
    lines.append("")
    lines.append("")
    lines.append("# Demodulator types — auto-generated from ka9q-radio/src/radio.h")
    lines.append("class DemodType:")
    lines.append(
        '    """Demodulator types — values must match '
        'ka9q-radio/src/radio.h enum demod_type"""'
    )
    lines.append("")

    for name, value, comment in demod_entries:
        suffix = f"  # {comment}" if comment else ""
        lines.append(f"    {name} = {value}{suffix}")

    # Window types
    lines.append("")
    lines.append("")
    lines.append("# Window types — auto-generated from ka9q-radio/src/window.h")
    lines.append("class WindowType:")
    lines.append(
        '    """FFT window types — values must match '
        'ka9q-radio/src/window.h enum window_type"""'
    )
    lines.append("")

    for name, value, comment in window_entries:
        suffix = f"  # {comment}" if comment else ""
        lines.append(f"    {name} = {value}{suffix}")

    lines.append("")  # final newline
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Diff / check / apply
# ---------------------------------------------------------------------------
def _check_enum(
    label: str,
    c_entries: List[Tuple[str, int, str]],
    py_entries: Dict[str, int],
    alias_names: Optional[Set[str]] = None,
) -> List[str]:
    """Compare a single C enum against its Python counterpart."""
    if alias_names is None:
        alias_names = set()
    issues: List[str] = []
    c_map = {name: val for name, val, _ in c_entries}

    for name, val, comment in c_entries:
        if name not in py_entries:
            issues.append(f"{label}: MISSING  {name} = {val}  // {comment}")
        elif py_entries[name] != val:
            issues.append(
                f"{label}: VALUE MISMATCH  {name}: "
                f"C={val}, Python={py_entries[name]}"
            )

    for name, val in sorted(py_entries.items(), key=lambda x: x[1]):
        if name in alias_names:
            continue
        if name not in c_map:
            issues.append(f"{label}: EXTRA in Python  {name} = {val}")

    return issues


def compute_drift(
    c_status: List[Tuple[str, int, str]],
    c_encoding: List[Tuple[str, int, str]],
    c_demod: List[Tuple[str, int, str]],
    c_window: List[Tuple[str, int, str]],
) -> List[str]:
    """
    Compare C headers against current types.py.
    Returns a list of human-readable drift descriptions (empty = in sync).
    """
    py_status, py_encoding, py_demod, py_window = parse_types_py()
    issues: List[str] = []

    issues.extend(_check_enum("StatusType", c_status, py_status))
    issues.extend(_check_enum("Encoding", c_encoding, py_encoding, {"F32", "F16"}))
    issues.extend(_check_enum("DemodType", c_demod, py_demod))
    issues.extend(_check_enum("WindowType", c_window, py_window))

    return issues


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Synchronize ka9q/types.py with ka9q-radio C headers"
    )
    parser.add_argument(
        "--ka9q-radio",
        type=Path,
        default=None,
        help="Path to ka9q-radio source tree (default: ../ka9q-radio relative to project root)",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero if types.py is out of sync",
    )
    group.add_argument(
        "--apply",
        action="store_true",
        help="Regenerate types.py and update ka9q_radio_compat",
    )
    group.add_argument(
        "--diff",
        action="store_true",
        help="Show drift without modifying anything",
    )

    args = parser.parse_args()

    # Resolve ka9q-radio path
    if args.ka9q_radio:
        radio_path = args.ka9q_radio.resolve()
    else:
        # Try ../ka9q-radio relative to project root
        radio_path = (PROJECT_ROOT / ".." / "ka9q-radio").resolve()

    status_h = radio_path / "src" / "status.h"
    rtp_h = radio_path / "src" / "rtp.h"
    radio_h = radio_path / "src" / "radio.h"
    window_h = radio_path / "src" / "window.h"

    if not status_h.exists():
        print(f"ERROR: {status_h} not found", file=sys.stderr)
        print(
            f"  Provide --ka9q-radio PATH or ensure ka9q-radio is at {radio_path}",
            file=sys.stderr,
        )
        return 2

    for h in [rtp_h, radio_h, window_h]:
        if not h.exists():
            print(f"ERROR: {h} not found", file=sys.stderr)
            return 2

    # Parse C headers
    status_text = status_h.read_text()
    rtp_text = rtp_h.read_text()
    radio_text = radio_h.read_text()
    window_text = window_h.read_text()

    c_status = parse_c_enum(status_text, "status_type")
    c_encoding = parse_c_enum(rtp_text, "encoding")
    c_demod = parse_c_enum(radio_text, "demod_type")
    c_window = parse_c_enum(window_text, "window_type")

    commit = get_git_commit(radio_path)

    if args.check or args.diff:
        issues = compute_drift(c_status, c_encoding, c_demod, c_window)
        if issues:
            print(f"Protocol drift detected vs ka9q-radio {commit[:12]}:")
            print()
            for issue in issues:
                print(f"  {issue}")
            print()
            print(f"  {len(issues)} issue(s) found")
            print()
            print("Run 'python scripts/sync_types.py --apply' to synchronize.")
            return 1
        else:
            print(f"types.py is in sync with ka9q-radio {commit[:12]}")
            return 0

    # --apply
    new_content = generate_types_py(c_status, c_encoding, c_demod, c_window, commit)

    # Read current for comparison
    old_content = TYPES_PY.read_text() if TYPES_PY.exists() else ""

    if new_content == old_content:
        print(f"types.py is already in sync with ka9q-radio {commit[:12]}")
    else:
        TYPES_PY.write_text(new_content)
        print(f"Updated {TYPES_PY}")

    # Update compat pin (plain text file for scripts/humans)
    pin_content = (
        "# ka9q-radio commit that ka9q/types.py was last validated against\n"
        "# Updated by: scripts/sync_types.py --apply\n"
        f"{commit}\n"
    )
    COMPAT_FILE.write_text(pin_content)
    print(f"Updated {COMPAT_FILE} → {commit[:12]}")

    # Update ka9q/compat.py (importable constant for ka9q-update)
    compat_py_content = (
        '"""\n'
        "ka9q-radio compatibility pin.\n"
        "\n"
        "Exposes the ka9q-radio commit hash that this version of ka9q-python\n"
        "was validated against.  Intended for consumption by ka9q-update and\n"
        "other deployment tooling.\n"
        "\n"
        "Auto-updated by: scripts/sync_types.py --apply\n"
        "\n"
        "Usage:\n"
        "    from ka9q.compat import KA9Q_RADIO_COMMIT\n"
        '    print(f"Compatible with ka9q-radio at {KA9Q_RADIO_COMMIT}")\n'
        '"""\n'
        "\n"
        f'KA9Q_RADIO_COMMIT: str = "{commit}"\n'
    )
    COMPAT_PY.write_text(compat_py_content)
    print(f"Updated {COMPAT_PY} → {commit[:12]}")

    # Report what changed
    if old_content:
        # Re-parse to show the delta
        py_status_old, py_enc_old, py_demod_old, py_window_old = parse_types_py()

        for label, c_entries, py_old, aliases in [
            ("StatusType", c_status, py_status_old, set()),
            ("Encoding", c_encoding, py_enc_old, {"F32", "F16"}),
            ("DemodType", c_demod, py_demod_old, set()),
            ("WindowType", c_window, py_window_old, set()),
        ]:
            c_map = {name: val for name, val, _ in c_entries}
            added = [n for n, v, _ in c_entries if n not in py_old]
            removed = [n for n in py_old if n not in c_map and n not in aliases]
            if added:
                print(f"  {label} added:   {', '.join(added)}")
            if removed:
                print(f"  {label} removed: {', '.join(removed)}")

    print()
    print("Next steps:")
    print("  1. Review the diff:  git diff ka9q/types.py")
    print("  2. Update control.py if any names were renamed/removed")
    print("  3. Run tests:        python -m pytest tests/")
    print("  4. Commit and bump version")

    return 0


if __name__ == "__main__":
    sys.exit(main())
