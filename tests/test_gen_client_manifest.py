import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
from gen_client_manifest import scan_repo, build_manifest


def make_client(tmp_path, name, source):
    repo = tmp_path / name
    repo.mkdir()
    (repo / "main.py").write_text(source)
    return repo


def test_scan_repo_collects_symbols_per_module(tmp_path):
    repo = make_client(tmp_path, "clientA", (
        "from ka9q import RadiodControl, MultiStream\n"
        "from ka9q.control import encode_int as ei, CMD\n"
        "import ka9q\n"
    ))
    result = scan_repo(repo)
    assert result == {
        "ka9q": ["MultiStream", "RadiodControl"],
        "ka9q.control": ["CMD", "encode_int"],
    }


def test_scan_repo_collects_parenthesized_multiline_import(tmp_path):
    repo = make_client(tmp_path, "clientC", (
        "from ka9q import (\n"
        "    SlotClock,\n"
        "    Encoding,\n"
        ")\n"
    ))
    assert scan_repo(repo) == {"ka9q": ["Encoding", "SlotClock"]}


def test_scan_repo_skips_venv_and_git(tmp_path):
    repo = make_client(tmp_path, "clientB", "from ka9q import SlotClock\n")
    hidden = repo / ".venv" / "lib"
    hidden.mkdir(parents=True)
    (hidden / "noise.py").write_text("from ka9q import RTPRecorder\n")
    assert scan_repo(repo) == {"ka9q": ["SlotClock"]}


def test_build_manifest_shape_and_signatures(tmp_path):
    make_client(tmp_path, "clientA", "from ka9q import RadiodControl\n")
    manifest = build_manifest(tmp_path)
    assert manifest["clients"] == {"clientA": {"ka9q": ["RadiodControl"]}}
    sig = manifest["signatures"]["ka9q:RadiodControl"]
    assert sig is None or sig.startswith("(")
    json.dumps(manifest)  # must be serializable


def test_build_manifest_is_deterministic(tmp_path):
    make_client(tmp_path, "b_client", "from ka9q import SlotClock\n")
    make_client(tmp_path, "a_client", "from ka9q import Encoding\n")
    m1 = json.dumps(build_manifest(tmp_path), sort_keys=True)
    m2 = json.dumps(build_manifest(tmp_path), sort_keys=True)
    assert m1 == m2
