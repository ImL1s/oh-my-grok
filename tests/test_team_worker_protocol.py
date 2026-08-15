"""Exact headless claim CLI for single-turn grok --prompt-file."""

from __future__ import annotations

from omg_cli.team.plane import build_team_task_prompt
from omg_cli.team.worker_protocol import team_worker_protocol_lines


def test_protocol_lines_include_numeric_board_id() -> None:
    text = "\n".join(
        team_worker_protocol_lines(
            run_id="run-a",
            team_id="team",
            worker_id="w1",
            api_task_id="1",
        )
    )
    assert "claim-task" in text
    assert '"task_id":"1"' in text
    assert '"worker":"w1"' in text
    assert "not `w1`" in text
    assert "transition-task-status" in text
    assert "CLAIM_TOKEN_FROM_PREVIOUS_JSON" in text
    assert "team_not_found" in text
    assert "retry" in text.lower()


def test_protocol_without_board_id_tells_worker_to_list() -> None:
    text = "\n".join(
        team_worker_protocol_lines(
            run_id="run-a",
            team_id="team",
            worker_id="w2",
            api_task_id=None,
        )
    )
    assert "list-tasks --input" in text
    assert '"run_id":"run-a"' in text
    assert '"team_id":"team"' in text
    assert "claim-task --input" in text
    assert "BOARD_TASK_ID_FROM_LIST" in text
    assert '"task_id":"1"' not in text


def test_build_team_task_prompt_puts_protocol_first(tmp_path) -> None:
    prompt = build_team_task_prompt(
        "do the work",
        run_id="run-p",
        task_id="w1",
        task_index=1,
        task_count=2,
        owned_files=["README.md"],
        worktree=tmp_path,
        team_id="team",
        api_task_id="1",
    )
    assert prompt.startswith("## First actions (required, this turn)")
    assert '"task_id":"1"' in prompt
    assert "## Board task id: 1" in prompt
