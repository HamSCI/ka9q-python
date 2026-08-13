#!/usr/bin/env python3
"""Generate tests/client_usage_manifest.json from sigmond-suite client repos.

Scans every repo under a root directory for `import ka9q` / `from ka9q...
import ...` statements and records, per client, which symbols it uses from
which ka9q module — plus a signature snapshot for every callable symbol.
tests/test_client_contract.py replays this manifest against the installed
ka9q package, so a breaking API change fails in this repo, naming the
affected clients, before it ships to them.

Usage:
    uv run python scripts/gen_client_manifest.py                  # default root
    uv run python scripts/gen_client_manifest.py --root /some/dir --out path.json
"""
from __future__ import annotations

import argparse
import ast
import importlib
import inspect
import json
import re
import sys
from pathlib import Path

DEFAULT_ROOT = Path("/opt/git/sigmond")
DEFAULT_OUT = Path(__file__).resolve().parent.parent / "tests" / "client_usage_manifest.json"
SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules"}
SKIP_REPOS = {"ka9q-python", "ka9q-radio"}

FROM_IMPORT = re.compile(r"^\s*from\s+(ka9q[\w.]*)\s+import\s+(.+)$")


def _symbols(clause: str) -> list[str]:
    clause = clause.split("#")[0].strip().strip("()")
    names = []
    for part in clause.split(","):
        part = part.strip()
        if not part:
            continue
        names.append(part.split(" as ")[0].strip())
    return names


def _scan_text_ast(text: str) -> dict[str, set[str]] | None:
    """Parse ka9q from-imports via the AST; returns None if the file doesn't parse.

    ast.parse naturally handles parenthesized multi-line import clauses
    (`from ka9q.x import (\\n    A, B,\\n)`), which the line-based regex
    fallback below cannot -- it only ever sees one physical line at a time.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    found: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        # level == 0 excludes relative imports (`from ...ka9q_encoding import
        # x`), which the regex fallback never matched either -- it requires
        # "ka9q" immediately after "from ", not after leading dots. A
        # relative "ka9q_encoding" resolves within the *client's own*
        # package tree, not this ka9q-python package.
        if (isinstance(node, ast.ImportFrom) and node.level == 0
                and node.module and node.module.startswith("ka9q")):
            found.setdefault(node.module, set()).update(
                alias.name for alias in node.names
            )
    return found


def _scan_text_regex(text: str) -> dict[str, set[str]]:
    """Line-based fallback for files ast.parse can't handle (e.g. py2, templated)."""
    found: dict[str, set[str]] = {}
    for line in text.splitlines():
        m = FROM_IMPORT.match(line)
        if m:
            found.setdefault(m.group(1), set()).update(_symbols(m.group(2)))
    return found


def scan_repo(path: Path) -> dict[str, list[str]]:
    found: dict[str, set[str]] = {}
    for py in sorted(path.rglob("*.py")):
        if any(seg in SKIP_DIRS for seg in py.parts):
            continue
        try:
            text = py.read_text(errors="replace")
        except OSError:
            continue
        file_found = _scan_text_ast(text)
        if file_found is None:
            file_found = _scan_text_regex(text)
        for mod, syms in file_found.items():
            found.setdefault(mod, set()).update(syms)
    return {mod: sorted(syms) for mod, syms in sorted(found.items())}


def _signature(module: str, symbol: str) -> str | None:
    try:
        obj = getattr(importlib.import_module(module), symbol)
    except Exception:
        return None
    try:
        return str(inspect.signature(obj))
    except (TypeError, ValueError):
        return None


def build_manifest(root: Path, repos: list[str] | None = None) -> dict:
    clients: dict[str, dict] = {}
    for repo in sorted(root.iterdir()):
        if not repo.is_dir() or repo.name in SKIP_REPOS or repo.name.startswith("."):
            continue
        if repos is not None and repo.name not in repos:
            continue
        usage = scan_repo(repo)
        if usage:
            clients[repo.name] = usage
    signatures: dict[str, str | None] = {}
    for usage in clients.values():
        for module, symbols in usage.items():
            for sym in symbols:
                signatures.setdefault(f"{module}:{sym}", _signature(module, sym))
    return {"root": str(root), "clients": clients,
            "signatures": dict(sorted(signatures.items()))}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args()
    manifest = build_manifest(args.root)
    args.out.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(f"wrote {args.out} ({len(manifest['clients'])} clients)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
