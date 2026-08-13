"""Guardrail: every symbol a sigmond-suite client imports from ka9q must
exist and keep its signature. Regenerate the manifest with
    uv run python scripts/gen_client_manifest.py
after intentional API changes — the diff shows exactly what clients see."""
import importlib
import inspect
import json
from pathlib import Path

import pytest

MANIFEST = json.loads(
    (Path(__file__).parent / "client_usage_manifest.json").read_text()
)


def _clients_using(module: str, symbol: str) -> list[str]:
    return sorted(
        name for name, usage in MANIFEST["clients"].items()
        if symbol in usage.get(module, [])
    )


def _all_usages() -> list[tuple[str, str]]:
    pairs = set()
    for usage in MANIFEST["clients"].values():
        for module, symbols in usage.items():
            for sym in symbols:
                pairs.add((module, sym))
    return sorted(pairs)


@pytest.mark.parametrize("module,symbol", _all_usages())
def test_client_symbol_exists(module, symbol):
    mod = importlib.import_module(module)
    assert hasattr(mod, symbol), (
        f"{module}.{symbol} is gone but still imported by: "
        f"{', '.join(_clients_using(module, symbol))}"
    )


@pytest.mark.parametrize(
    "key,expected", sorted(MANIFEST["signatures"].items())
)
def test_client_symbol_signature_stable(key, expected):
    if expected is None:
        pytest.skip("non-callable or signature not captured")
    module, symbol = key.split(":")
    obj = getattr(importlib.import_module(module), symbol)
    actual = str(inspect.signature(obj))
    assert actual == expected, (
        f"Signature of {module}.{symbol} changed\n"
        f"  was: {expected}\n  now: {actual}\n"
        f"  clients affected: {', '.join(_clients_using(module, symbol))}\n"
        f"  If intentional, regenerate: uv run python scripts/gen_client_manifest.py"
    )
