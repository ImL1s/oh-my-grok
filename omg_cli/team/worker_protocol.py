"""Copy-pasteable Team worker claim/complete CLI (headless single-turn).

Live grok ``--prompt-file`` is one shot. Vague "then claim-task" text is not
enough: workers must see the **board** task id (numeric) distinct from the
logical worker id (``w1``), and the exact ``omg team api`` argv.
"""

from __future__ import annotations

import json
from typing import Any


def _dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def team_worker_protocol_lines(
    *,
    run_id: str,
    team_id: str,
    worker_id: str,
    api_task_id: str | None,
) -> list[str]:
    """Exact CLI steps a pane worker must run this turn."""
    ack = {
        "run_id": run_id,
        "team_id": team_id,
        "from_worker": worker_id,
        "to_worker": "leader-fixed",
        "body": "ACK",
    }
    lines = [
        "## First actions (required, this turn)",
        "Headless providers are **single-turn**. Run these commands with a shell",
        "tool **before** other work. Do not paraphrase the JSON. Your worker id",
        f"is `{worker_id}` (OMG_TEAM_WORKER_ID); the board task id is numeric.",
        "",
        "1. ACK the leader:",
        "   `OMG_EXPERIMENTAL_TMUX_TEAM=1 omg team api send-message --input "
        f"'{_dumps(ack)}'`",
        "If the API returns team control plane missing / team_not_found,",
        "retry that exact command every 2s for up to 45s — `team.json`",
        "publishes after pane spawn. Do not skip ACK/claim.",
    ]
    if api_task_id:
        claim = {"task_id": str(api_task_id), "worker": worker_id}
        complete = {
            "task_id": str(api_task_id),
            "worker": worker_id,
            "from": "in_progress",
            "to": "completed",
            "claim_token": "CLAIM_TOKEN_FROM_PREVIOUS_JSON",
        }
        lines.extend(
            [
                f"2. Claim board task `{api_task_id}` "
                f"(not `{worker_id}` — that is the worker id):",
                "   `OMG_EXPERIMENTAL_TMUX_TEAM=1 omg team api claim-task --input "
                f"'{_dumps(claim)}'`",
                "3. Do the assignment in this worktree. Then complete using the",
                "`claimToken` string from step 2 stdout (replace the placeholder):",
                "   `OMG_EXPERIMENTAL_TMUX_TEAM=1 omg team api "
                "transition-task-status --input "
                f"'{_dumps(complete)}'`",
            ]
        )
    else:
        listing = {"run_id": run_id, "team_id": team_id}
        claim = {"task_id": "BOARD_TASK_ID_FROM_LIST", "worker": worker_id}
        complete = {
            "task_id": "BOARD_TASK_ID_FROM_LIST",
            "worker": worker_id,
            "from": "in_progress",
            "to": "completed",
            "claim_token": "CLAIM_TOKEN_FROM_PREVIOUS_JSON",
        }
        lines.extend(
            [
                "2. List board tasks, then claim the **numeric** board task id",
                f"(not `{worker_id}` — that is the worker id):",
                "   `OMG_EXPERIMENTAL_TMUX_TEAM=1 omg team api list-tasks --input "
                f"'{_dumps(listing)}'`",
                "   `OMG_EXPERIMENTAL_TMUX_TEAM=1 omg team api claim-task --input "
                f"'{_dumps(claim)}'`",
                "Replace BOARD_TASK_ID_FROM_LIST with the id from list-tasks.",
                "If list-tasks returns team control plane missing / team_not_found,",
                "retry that exact command every 2s for up to 45s.",
                "3. Do the assignment in this worktree. Then complete using the",
                "`claimToken` string from step 2 stdout (replace the placeholder):",
                "   `OMG_EXPERIMENTAL_TMUX_TEAM=1 omg team api "
                "transition-task-status --input "
                f"'{_dumps(complete)}'`",
            ]
        )
    lines.extend(
        [
            "4. Never set `verified` / `passes` under `.omg/state/`.",
            "",
        ]
    )
    return lines
