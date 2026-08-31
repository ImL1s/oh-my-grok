"""Installed Antigravity plugin manifests and lifecycle adapter (#72/#73)."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

from omg_cli.runtime_events import read_runtime_events, source_journal_path
from omg_cli.hooks_registry import (
    ANTIGRAVITY_EVENT_MAP,
    CANONICAL_EVENTS,
    inspect_antigravity_host_manifests,
)


ROOT = Path(__file__).resolve().parents[1]
ADAPTER = ROOT / "hooks" / "bin" / "antigravity_hook.py"


def _run_adapter(event: str, payload: dict, workspace: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PWD"] = str(workspace)
    return subprocess.run(
        [sys.executable, str(ADAPTER), event],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        cwd=workspace,
        env=env,
        timeout=10,
        check=False,
    )


def test_root_antigravity_hooks_manifest_is_live_and_supported_only() -> None:
    payload = json.loads((ROOT / "hooks.json").read_text(encoding="utf-8"))
    assert set(payload) == {"omg-lifecycle"}
    lifecycle = payload["omg-lifecycle"]
    assert lifecycle["enabled"] is True
    assert set(lifecycle) == {"enabled", "PreToolUse", "PostToolUse", "Stop"}
    assert lifecycle["PreToolUse"][0]["matcher"] == "*"
    assert lifecycle["PostToolUse"][0]["matcher"] == "*"
    commands = [
        lifecycle["PreToolUse"][0]["hooks"][0]["command"],
        lifecycle["PostToolUse"][0]["hooks"][0]["command"],
        lifecycle["Stop"][0]["command"],
    ]
    assert all(
        "${extensionPath}/hooks/bin/antigravity_hook.py" in command
        for command in commands
    )
    assert commands[0].endswith(" PreToolUse")
    assert commands[1].endswith(" PostToolUse")
    assert commands[2].endswith(" Stop")
    serialized = json.dumps(payload)
    assert "UserPromptSubmit" not in serialized
    assert "PreInvocation" not in serialized


def test_antigravity_manifest_inspect_and_event_map_are_honest() -> None:
    result = inspect_antigravity_host_manifests(ROOT)
    assert result["configured"] is True
    assert result["loadable"] is True
    assert result["installed"] is False
    assert result["observed"] is False
    assert result["healthy"] is False
    assert result["verified"] is False
    assert result["user_prompt_submit"] == "unsupported"
    assert set(ANTIGRAVITY_EVENT_MAP) == set(CANONICAL_EVENTS)
    assert ANTIGRAVITY_EVENT_MAP["tool.pre"] == "native_blocking"
    assert ANTIGRAVITY_EVENT_MAP["tool.post"] == "native_passive"
    assert ANTIGRAVITY_EVENT_MAP["prompt.submit"] == "unsupported"


def test_pretool_translates_agy_run_command_and_denies_external_agent_cli(
    tmp_path: Path,
) -> None:
    proc = _run_adapter(
        "PreToolUse",
        {
            "conversationId": "conversation-1",
            "workspacePaths": [str(tmp_path)],
            "toolCall": {
                "name": "run_command",
                "args": {"CommandLine": "codex exec unsafe"},
            },
            "stepIdx": 3,
        },
        tmp_path,
    )
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)
    assert result["decision"] == "deny"
    assert "external agent CLI" in result["reason"]
    rows = read_runtime_events(source_journal_path(tmp_path, "antigravity-hook"))
    assert len(rows) == 1
    assert rows[0]["payload"]["canonical_event"] == "tool.pre"
    serialized = json.dumps(rows)
    assert "codex exec unsafe" not in serialized


def test_pretool_malformed_payload_fails_open_without_creating_project_state(
    tmp_path: Path,
) -> None:
    proc = _run_adapter("PreToolUse", {"workspacePaths": "not-a-list"}, tmp_path)
    assert proc.returncode == 0
    assert json.loads(proc.stdout)["decision"] == "allow"
    assert not (tmp_path / ".omg").exists()


def test_posttool_records_success_or_failure_and_emits_empty_agy_result(
    tmp_path: Path,
) -> None:
    ok = _run_adapter(
        "PostToolUse",
        {
            "conversationId": "conversation-2",
            "workspacePaths": [str(tmp_path)],
            "stepIdx": 4,
        },
        tmp_path,
    )
    failed = _run_adapter(
        "PostToolUse",
        {
            "conversationId": "conversation-2",
            "workspacePaths": [str(tmp_path)],
            "stepIdx": 5,
            "error": "secret command output",
        },
        tmp_path,
    )
    assert ok.returncode == failed.returncode == 0
    assert json.loads(ok.stdout) == json.loads(failed.stdout) == {}
    rows = read_runtime_events(source_journal_path(tmp_path, "antigravity-hook"))
    assert [row["payload"]["canonical_event"] for row in rows] == [
        "tool.post",
        "tool.failure",
    ]
    assert "secret command output" not in json.dumps(rows)


def test_stop_allows_by_default_and_never_claims_verified(tmp_path: Path) -> None:
    proc = _run_adapter(
        "Stop",
        {
            "conversationId": "conversation-3",
            "workspacePaths": [str(tmp_path)],
            "terminationReason": "model_stop",
            "fullyIdle": True,
        },
        tmp_path,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {"decision": "allow"}
    rows = read_runtime_events(source_journal_path(tmp_path, "antigravity-hook"))
    assert rows[-1]["payload"]["canonical_event"] == "stop.request"
    assert rows[-1]["payload"]["verified"] is False


def test_root_mcp_config_exposes_read_only_offline_tools_sidecar() -> None:
    payload = json.loads((ROOT / "mcp_config.json").read_text(encoding="utf-8"))
    assert set(payload["mcpServers"]) == {"omg-tools"}
    server = payload["mcpServers"]["omg-tools"]
    assert server["command"] == "python3"
    assert server["args"] == [
        "${extensionPath}/bin/omg",
        "tools",
        "serve",
        "--stdio",
        "--capability-mode",
        "read-only",
    ]
    assert server["env"]["OMG_TOOLS_NETWORK"] == "0"
    assert "serverUrl" not in server


def test_configured_tools_sidecar_stdio_handshake_is_read_only(tmp_path: Path) -> None:
    config = json.loads((ROOT / "mcp_config.json").read_text(encoding="utf-8"))
    server = config["mcpServers"]["omg-tools"]
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "pytest", "version": "1"},
        },
    }
    listed = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
    denied_write = {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {
            "name": "omg.tools.ast.replace",
            "arguments": {
                "pattern": "$X",
                "rewrite": "changed",
                "lang": "python",
                "path": "sample.py",
                "write": True,
            },
        },
    }
    (tmp_path / "sample.py").write_text("original\n", encoding="utf-8")
    proc = subprocess.run(
        [
            server["command"],
            *(arg.replace("${extensionPath}", str(ROOT)) for arg in server["args"]),
        ],
        input=(
            json.dumps(request)
            + "\n"
            + json.dumps(listed)
            + "\n"
            + json.dumps(denied_write)
            + "\n"
        ),
        text=True,
        capture_output=True,
        cwd=tmp_path,
        env={**os.environ, **server["env"], "PWD": str(tmp_path)},
        timeout=10,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    replies = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
    tools = replies[1]["result"]["tools"]
    assert tools
    assert all(tool["name"].startswith("omg.tools.") for tool in tools)
    assert replies[2]["result"]["isError"] is True
    assert "E_READ_ONLY" in replies[2]["result"]["content"][0]["text"]
    assert (tmp_path / "sample.py").read_text(encoding="utf-8") == "original\n"
    assert not (tmp_path / ".omg").exists()


def test_hook_manifest_command_resolves_from_foreign_workspace(tmp_path: Path) -> None:
    config = json.loads((ROOT / "hooks.json").read_text(encoding="utf-8"))
    command = config["omg-lifecycle"]["PreToolUse"][0]["hooks"][0]["command"]
    resolved = command.replace("${extensionPath}", str(ROOT))
    payload = {
        "workspacePaths": [str(tmp_path)],
        "toolCall": {
            "name": "run_command",
            "args": {"CommandLine": "codex exec unsafe"},
        },
    }

    proc = subprocess.run(
        shlex.split(resolved),
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        cwd=tmp_path,
        env={**os.environ, "PWD": str(tmp_path)},
        timeout=10,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["decision"] == "deny"
