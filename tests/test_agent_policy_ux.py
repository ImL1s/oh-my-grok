"""Host-neutral agent/model policy UX (#134)."""

from __future__ import annotations

import json
import os
from types import SimpleNamespace

from omg_cli.agent_policy import PolicyReason, resolve_agent_policy
from omg_cli.agent_policy_ux import (
    POLICY_NATIVE_NOTE,
    color_enabled,
    display_width,
    format_doctor_routing_human,
    format_presentation_human,
    pad_display,
    render_explain_human,
    render_list_human,
    terminal_width,
    truncate_display,
    width_band,
)
from omg_cli.host_capabilities import stock_grok_snapshot
from omg_cli.main import main
from omg_cli.team.plane import format_status_table
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _view(**overrides: object) -> SimpleNamespace:
    base = dict(
        agent_id="omg-verifier",
        aliases=("verifier", "验证器"),
        category="verifier",
        tier="verifier",
        capability_floor="read-only",
        tool_floor=(),
        policy_id="verifier.default",
        policy_digest="abc",
        policy_source="canonical",
        baseline_mode="inherit",
        baseline_model=None,
        requested_extension="medley.native-ordered-candidates.v1",
        candidate_ids=("review-primary-example", "review-fallback-example"),
        prompt_profile="generic",
        reasoning_preference=None,
        host_capabilities=(
            {"capability_id": "host.native-inherit-model.v1", "state": "supported"},
        ),
        selected_model_ref=None,
        route_kind="native",
        route_receipt_digest=None,
        attempt=1,
        status="ready",
        reasons=(
            PolicyReason(
                code="E_EXTENSION_NOT_AUTHORIZED",
                message="optional extension is unsupported",
                next_action="no action on original Grok Build",
            ),
        ),
        host_facts={
            "medley_capability_outcome": "unsupported",
            "route_specific_facts": "unavailable",
        },
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_width_bands() -> None:
    assert width_band(40) == "narrow"
    assert width_band(80) == "normal"
    assert width_band(160) == "wide"


def test_cjk_display_width_and_padding() -> None:
    assert display_width("验证器") == 6
    assert display_width("abc") == 3
    padded = pad_display("验证器", 8)
    assert display_width(padded) == 8
    assert truncate_display("验证器-extra", 5).endswith("…")


def test_list_narrow_preserves_identity_and_status() -> None:
    text = render_list_human((_view(),), columns=40)
    assert "omg-verifier" in text
    assert "验证器" in text
    assert "status: ready" in text
    assert "Host policy" not in text
    assert "\x1b[" not in text


def test_list_normal_and_wide_tables() -> None:
    rows = (_view(), _view(agent_id="omg-executor", aliases=("executor",), requested_extension=None, candidate_ids=(), reasons=()))
    normal = render_list_human(rows, columns=100)
    assert "Agent" in normal
    assert "Status" in normal
    assert "omg-verifier" in normal
    assert "ready" in normal
    wide = render_list_human(rows, columns=160)
    assert "Source" in wide
    assert "Floor" in wide


def test_explain_progressive_disclosure() -> None:
    view = _view()
    narrow = render_explain_human(view, columns=40)
    assert "Identity" in narrow
    assert "Next action" in narrow
    assert "Host capability registry" not in narrow
    assert "unsupported" in narrow
    normal = render_explain_human(view, columns=100)
    assert "Capability-gated Medley policy and candidate order" in normal
    assert "Resume/attempt lineage" in normal
    wide = render_explain_human(view, columns=160)
    assert "Host capability registry" in wide
    assert "host.native-inherit-model.v1=supported" in wide
    assert POLICY_NATIVE_NOTE in wide


def test_no_color_never_emits_ansi(monkeypatch) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    assert color_enabled() is False
    text = render_list_human((_view(),), columns=100, env=os.environ)
    assert "\x1b[" not in text
    monkeypatch.delenv("NO_COLOR", raising=False)
    assert color_enabled() is False


def test_terminal_width_override_and_columns(monkeypatch) -> None:
    monkeypatch.setenv("COLUMNS", "42")
    assert terminal_width() == 42
    assert terminal_width(override=120) == 120


def test_cli_width_snapshots(capsys, monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".omg").mkdir()
    rc = main(
        [
            "agents",
            "list",
            "--width",
            "40",
            "--project-root",
            str(tmp_path),
        ]
    )
    assert rc == 0
    narrow = capsys.readouterr().out
    assert "omg-verifier" in narrow
    assert "status:" in narrow
    rc = main(
        [
            "agents",
            "explain",
            "omg-verifier",
            "--width",
            "160",
            "--project-root",
            str(tmp_path),
        ]
    )
    assert rc == 0
    wide = capsys.readouterr().out
    assert "Host capability registry" in wide
    assert "unsupported" in wide
    blob = json.dumps(resolve_agent_policy("omg-verifier", root=ROOT).to_json())
    for needle in ("api_key", "sk-", "bearer ", "account_id"):
        assert needle not in blob.lower()


def test_doctor_routing_human_is_honest() -> None:
    text = format_doctor_routing_human(stock_grok_snapshot())
    assert "unsupported" in text
    assert "not installation failed" in text
    assert "requires medley" not in text.lower()
    assert "routing-availability" not in text
    assert "\x1b[" not in text


def test_presentation_human_labels_route_kind() -> None:
    text = format_presentation_human(
        {
            "run_id": "run-example-1",
            "team_id": "team",
            "members": [
                {
                    "logical_worker_id": "t1",
                    "role": "executor",
                    "route": {"kind": "external_executor", "executor": "grok"},
                    "current_attempt": {"attempt": 1, "status": "dry_run"},
                }
            ],
        }
    )
    assert "external_executor" in text
    assert POLICY_NATIVE_NOTE in text
    assert "omg team status --json" in text
    assert "t1" in text


def test_status_table_route_kind_does_not_touch_locked_json() -> None:
    table = format_status_table(
        {
            "run_id": "r1",
            "session": None,
            "dry_run": True,
            "workspace_mode": "worktree",
            "tasks": [
                {
                    "task_id": "t1",
                    "window_index": 0,
                    "alive": False,
                    "status": "dry_run",
                    "worktree": "wt",
                    "route": {"kind": "external_executor"},
                }
            ],
        }
    )
    assert "route=external_executor" in table
    assert "io_mode" in table
    from omg_cli.team.plane import STATUS_TOP_KEYS

    assert STATUS_TOP_KEYS == (
        "run_id",
        "session",
        "dry_run",
        "workspace_mode",
        "tasks",
    )
