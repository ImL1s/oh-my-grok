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
        "publishes after pane spawn. Do not skip ACK.",
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
                "Do not skip claim — a numeric board task is already bound.",
                "3. Do the assignment in this worktree. Then complete using the",
                "`claimToken` string from step 2 stdout (replace the placeholder):",
                "   `OMG_EXPERIMENTAL_TMUX_TEAM=1 omg team api "
                "transition-task-status --input "
                f"'{_dumps(complete)}'`",
            ]
        )
    else:
        listing = {"run_id": run_id, "team_id": team_id}
        lines.extend(
            [
                "2. No board task was bound for this pane. Discover, do not block:",
                "   `OMG_EXPERIMENTAL_TMUX_TEAM=1 omg team api list-tasks --input "
                f"'{_dumps(listing)}'`",
                "If list-tasks returns team control plane missing / team_not_found,",
                "retry that exact command every 2s for up to 45s.",
                "If the list is empty or has no numeric board id for this worker,",
                "**skip claim** and do the assignment in this worktree.",
                "If a numeric board task id is present, claim it (not "
                f"`{worker_id}`):",
                "   `OMG_EXPERIMENTAL_TMUX_TEAM=1 omg team api claim-task --input "
                f"'{_dumps({'task_id': '<BOARD_ID>', 'worker': worker_id})}'`",
                "then `transition-task-status` to `completed` with `claim_token`",
                "from that claim response.",
                "3. Do the assignment in this worktree.",
            ]
        )
    lines.extend(
        [
            "4. Never set `verified` / `passes` under `.omg/state/`.",
            "",
        ]
    )
    return lines
