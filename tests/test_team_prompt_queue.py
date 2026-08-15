"""Host prompt-queue consume (#69 catalog v5). Not mailbox / not task ACK."""

from __future__ import annotations

from pathlib import Path

import pytest

from omg_cli.host_models import CAPABILITY_KEYS, HostCapabilitySet
from omg_cli.host_probe import evaluate_feature_gate
from omg_cli.team.api import execute_team_api
from omg_cli.team.plane import EXPERIMENTAL_ENV, WORKER_ENV_MARKERS, start_team
from omg_cli.team.prompt_queue import (
    FORBIDDEN_KINDS,
    PROMPT_QUEUE_CAP_IDS,
    PromptQueueError,
    enqueue_host_prompt,
    list_host_prompt_queue,
    mailbox_dir,
    mark_host_prompt_queue_waiting,
    queue_path,
    reorder_host_prompt_queue,
)

TEAM = "team-api"
SEED_TASKS = [{"task_id": "t-a", "owned_files": ["a.py"]}]


def _git(cwd: Path, *args: str) -> None:
    import subprocess

    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
    )


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init")
    _git(path, "config", "user.email", "omg-test@example.com")
    _git(path, "config", "user.name", "omg-test")
    _git(path, "config", "commit.gpgsign", "false")
    (path / "README.md").write_text("base\n", encoding="utf-8")
    _git(path, "add", "README.md")
    _git(path, "commit", "-m", "initial")


def _env_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(EXPERIMENTAL_ENV, "1")
    for key in WORKER_ENV_MARKERS:
        monkeypatch.delenv(key, raising=False)


def _seed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    _env_on(monkeypatch)
    _init_repo(tmp_path)
    meta = start_team(
        "prompt-queue seed",
        SEED_TASKS,
        root=tmp_path,
        dry_run=True,
        env={EXPERIMENTAL_ENV: "1"},
        check_binary=False,
    )
    return str(meta["run_id"])


def test_host_probe_does_not_advertise_prompt_queue_caps() -> None:
    for cap_id in PROMPT_QUEUE_CAP_IDS:
        assert cap_id not in CAPABILITY_KEYS
        gate = evaluate_feature_gate(cap_id, HostCapabilitySet(), required=False)
        assert gate.state == "BLOCKED"


def test_queue_preserves_order_and_is_visible_while_waiting(tmp_path: Path) -> None:
    first = enqueue_host_prompt(
        tmp_path, run_id="run-q", team_id="team-q", body="alpha-one"
    )
    second = enqueue_host_prompt(
        tmp_path, run_id="run-q", team_id="team-q", body="bravo-two"
    )
    listing = list_host_prompt_queue(tmp_path, run_id="run-q", team_id="team-q")
    assert listing["prompt_ids"] == [first["prompt_id"], second["prompt_id"]]
    waiting = mark_host_prompt_queue_waiting(
        tmp_path, run_id="run-q", team_id="team-q", waiting=True
    )
    assert waiting["waiting"] is True
    assert waiting["prompt_ids"] == listing["prompt_ids"]
    assert waiting["consume"]["not_mailbox"] is True
    assert waiting["consume"]["not_task_ack"] is True
    assert waiting["consume"]["gate_state"] == "LEGACY"
    assert not mailbox_dir(tmp_path, "run-q", "team-q").exists()


def test_reorder_is_permutation_and_refuses_partial(tmp_path: Path) -> None:
    a = enqueue_host_prompt(tmp_path, run_id="run-q", team_id="team-q", body="a")
    b = enqueue_host_prompt(tmp_path, run_id="run-q", team_id="team-q", body="b")
    c = enqueue_host_prompt(tmp_path, run_id="run-q", team_id="team-q", body="c")
    reordered = reorder_host_prompt_queue(
        tmp_path,
        run_id="run-q",
        team_id="team-q",
        order=[c["prompt_id"], a["prompt_id"], b["prompt_id"]],
    )
    assert reordered["prompt_ids"] == [c["prompt_id"], a["prompt_id"], b["prompt_id"]]
    with pytest.raises(PromptQueueError) as ei:
        reorder_host_prompt_queue(
            tmp_path,
            run_id="run-q",
            team_id="team-q",
            order=[a["prompt_id"], b["prompt_id"]],
        )
    assert ei.value.code == "E_TEAM_PROMPT_QUEUE_ORDER"


def test_forbidden_kinds_are_not_task_or_mailbox_acks(tmp_path: Path) -> None:
    for kind in sorted(FORBIDDEN_KINDS):
        with pytest.raises(PromptQueueError) as ei:
            enqueue_host_prompt(
                tmp_path, run_id="run-q", team_id="team-q", body="nope", kind=kind
            )
        assert ei.value.code == "E_TEAM_PROMPT_QUEUE_KIND"
    assert not mailbox_dir(tmp_path, "run-q", "team-q").exists()
    assert not queue_path(tmp_path, "run-q", "team-q").exists()


def test_api_enqueue_list_reorder_and_worker_acl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _seed(tmp_path, monkeypatch)
    code, first = execute_team_api(
        "enqueue-host-prompt",
        {"run_id": run_id, "team_id": TEAM, "body": "prompt-a"},
        root=tmp_path,
    )
    assert code == 0
    code, second = execute_team_api(
        "enqueue-host-prompt",
        {"run_id": run_id, "team_id": TEAM, "body": "prompt-b"},
        root=tmp_path,
    )
    assert code == 0
    a = first["data"]["entry"]["prompt_id"]
    b = second["data"]["entry"]["prompt_id"]
    code, listing = execute_team_api(
        "list-host-prompt-queue",
        {"run_id": run_id, "team_id": TEAM},
        root=tmp_path,
    )
    assert code == 0
    assert listing["data"]["prompt_ids"] == [a, b]
    code, reordered = execute_team_api(
        "reorder-host-prompt-queue",
        {"run_id": run_id, "team_id": TEAM, "order": [b, a]},
        root=tmp_path,
    )
    assert code == 0
    assert reordered["data"]["prompt_ids"] == [b, a]

    monkeypatch.setenv("OMG_TEAM_WORKER", "1")
    monkeypatch.setenv("OMG_TEAM_WORKER_ID", "t-a")
    monkeypatch.setenv("OMG_TEAM_RUN_ID", run_id)
    monkeypatch.setenv("OMG_TEAM_ID", TEAM)
    code, listed = execute_team_api(
        "list-host-prompt-queue",
        {"run_id": run_id, "team_id": TEAM},
        root=tmp_path,
    )
    assert code == 0
    code, denied = execute_team_api(
        "enqueue-host-prompt",
        {"run_id": run_id, "team_id": TEAM, "body": "from-worker"},
        root=tmp_path,
    )
    assert code == 2
    assert denied["error"]["code"] == "E_TEAM_API_GATE"
    code, denied2 = execute_team_api(
        "reorder-host-prompt-queue",
        {"run_id": run_id, "team_id": TEAM, "order": [a, b]},
        root=tmp_path,
    )
    assert code == 2
    assert denied2["error"]["code"] == "E_TEAM_API_GATE"


def test_load_rejects_incomplete_and_tampered_entries(tmp_path: Path) -> None:
    from omg_cli.contracts.path_keys import DATA_FILE_MODE, atomic_write_bytes
    from omg_cli.contracts.writer_chain import canonical_json_bytes, parse_canonical_json_bytes

    enqueue_host_prompt(tmp_path, run_id="run-q", team_id="team-q", body="alpha-one")
    path = queue_path(tmp_path, "run-q", "team-q")
    raw = parse_canonical_json_bytes(path.read_bytes())
    assert isinstance(raw, dict)
    incomplete = dict(raw)
    incomplete["entries"] = [{k: v for k, v in raw["entries"][0].items() if k != "content_hash"}]
    atomic_write_bytes(
        path, canonical_json_bytes(incomplete), mode=DATA_FILE_MODE, replace=True
    )
    with pytest.raises(PromptQueueError, match="invalid|content_hash"):
        list_host_prompt_queue(tmp_path, run_id="run-q", team_id="team-q")

    enqueue_host_prompt(tmp_path, run_id="run-q2", team_id="team-q", body="bravo-two")
    path2 = queue_path(tmp_path, "run-q2", "team-q")
    raw2 = parse_canonical_json_bytes(path2.read_bytes())
    assert isinstance(raw2, dict)
    tampered = dict(raw2)
    entry = dict(raw2["entries"][0])
    entry["content_hash"] = "0" * 64
    tampered["entries"] = [entry]
    atomic_write_bytes(
        path2, canonical_json_bytes(tampered), mode=DATA_FILE_MODE, replace=True
    )
    with pytest.raises(PromptQueueError, match="content_hash mismatch"):
        list_host_prompt_queue(tmp_path, run_id="run-q2", team_id="team-q")
