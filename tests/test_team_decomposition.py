"""Goal decomposition for shorthand team launch."""

from __future__ import annotations

from omg_cli.team.decomposition import decompose_goal


def test_numbered_items() -> None:
    tasks = decompose_goal(
        "1. fix a.py\n2. fix b.py\n3. fix c.py",
        workers=3,
        role="executor",
    )
    assert [t["task_id"] for t in tasks] == ["w1", "w2", "w3"]
    assert "a.py" in tasks[0]["subject"]
    assert tasks[0]["owned_files"][0].startswith(".omg/team-lanes/")


def test_atomic_lanes_have_dependencies() -> None:
    tasks = decompose_goal("fix the flaky suite", workers=3, role="executor")
    assert len(tasks) == 3
    assert tasks[0]["depends_on"] == []
    assert tasks[1]["depends_on"] == ["w1"]
    assert tasks[2]["depends_on"] == ["w2"]
