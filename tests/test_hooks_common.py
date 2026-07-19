# tests/test_hooks_common.py
import json
import sys
from pathlib import Path

# hooks/bin is not a package; put it on path for imports
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hooks" / "bin"))
from _common import append_event, ensure_omg_dirs, workspace_root  # noqa: E402


def test_ensure_and_append(tmp_path, monkeypatch):
    monkeypatch.setenv("GROK_WORKSPACE_ROOT", str(tmp_path))
    root = workspace_root()
    assert root == tmp_path
    ensure_omg_dirs(root)
    assert (root / ".omg" / "state").is_dir()
    append_event(root, {"event": "test", "status": "ok"})
    lines = (root / ".omg" / "state" / "events.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["event"] == "test"
