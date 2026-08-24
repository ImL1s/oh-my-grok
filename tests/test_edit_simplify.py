"""Public simplify CLI: assignment-only default plus grok Jobs proposal (#76 leftover)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from omg_cli.hash_edit.descriptor import HASH_EDIT_KIND
from omg_cli.jobs.models import JobState
from omg_cli.main import build_parser, main

JOB_ID = "20260824T000000Z-abcd1234"


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / ".omg").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OMG_PROJECT_ROOT", str(tmp_path))
    monkeypatch.delenv("OMG_CAPABILITY_MODE", raising=False)
    monkeypatch.delenv("OMG_RUN_ID", raising=False)
    monkeypatch.delenv("OMG_TASK_ID", raising=False)
    return tmp_path


def _out(capsys: pytest.CaptureFixture[str]) -> dict:
    raw = capsys.readouterr().out
    assert raw.strip(), "expected JSON on stdout"
    return json.loads(raw)


def _code(payload: dict) -> str:
    return (payload.get("error") or {}).get("code") or payload.get("error_code")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _descriptor(path: str, current: str, *, old_text: str, replacement: str) -> dict:
    before_context = ""
    after_context = "\n" if current.endswith("\n") else ""
    return {
        "schema_version": 1,
        "kind": HASH_EDIT_KIND,
        "edit_id": "edit-simplify-grok-1",
        "producer": "omg-code-simplifier",
        "path": path,
        "base_sha256": _sha(current),
        "old_text": old_text,
        "replacement": replacement,
        "before_context": before_context,
        "after_context": after_context,
        "old_text_sha256": _sha(old_text),
        "replacement_sha256": _sha(replacement),
        "before_context_sha256": _sha(before_context),
        "after_context_sha256": _sha(after_context),
    }


class _FakeGrokJob:
    def __init__(
        self,
        output: str,
        *,
        fail: bool = False,
        job_id: str = JOB_ID,
    ) -> None:
        self.output = output
        self.fail = fail
        self.job_id = job_id
        self.start_calls: list[dict] = []

    def start(self, project_root: Path, **kwargs: object) -> SimpleNamespace:
        self.start_calls.append(dict(kwargs))
        art = Path(project_root) / ".omg" / "jobs" / self.job_id / "artifacts"
        art.mkdir(parents=True, exist_ok=True)
        (art / "result.md").write_text(self.output, encoding="utf-8")
        return SimpleNamespace(record=SimpleNamespace(job_id=self.job_id), launched=True)

    def wait(self, project_root: Path, job_id: str, **kwargs: object) -> tuple[SimpleNamespace, bool]:
        del project_root, kwargs
        state = JobState.FAILED if self.fail else JobState.SUCCEEDED
        return SimpleNamespace(state=state, job_id=job_id), False

    def collect(self, project_root: Path, job_id: str) -> dict:
        del project_root
        state = JobState.FAILED.value if self.fail else JobState.SUCCEEDED.value
        return {
            "job_id": job_id,
            "state": state,
            "result": "artifacts/result.md",
            "exit": {"ok": not self.fail, "class": "success" if not self.fail else "error"},
        }


def _install_fake(monkeypatch: pytest.MonkeyPatch, fake: _FakeGrokJob) -> None:
    monkeypatch.setattr("omg_cli.edit_hygiene.simplify.start_job", fake.start)
    monkeypatch.setattr("omg_cli.edit_hygiene.simplify.wait_job", fake.wait)
    monkeypatch.setattr("omg_cli.edit_hygiene.simplify.collect_job", fake.collect)
    monkeypatch.setattr(
        "omg_cli.edit_hygiene.simplify.cancel_job", lambda *_args, **_kwargs: None
    )


def _git_init(path: Path) -> None:
    import subprocess

    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t"], cwd=path, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.name", "t"], cwd=path, check=True, capture_output=True
    )
    subprocess.run(["git", "add", "-A"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-m", "i"],
        cwd=path,
        check=True,
        capture_output=True,
    )


def test_parser_wires_simplify_provider_grok() -> None:
    parser = build_parser()
    ns = parser.parse_args(
        ["edit", "simplify", "--paths", "a.py", "--enable", "--provider", "grok"]
    )
    assert ns.edit_action == "simplify"
    assert ns.simplify_provider == "grok"
    ns = parser.parse_args(["edit", "simplify", "--paths", "a.py", "--enable"])
    assert ns.simplify_provider is None


def test_simplify_enable_without_provider_is_assignment(
    project: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (project / "app.py").write_text("x = 1\n", encoding="utf-8")
    called: list[object] = []

    def _boom(*_args: object, **_kwargs: object) -> None:
        called.append(1)
        raise AssertionError("start_job must not run without --provider")

    monkeypatch.setattr("omg_cli.edit_hygiene.simplify.start_job", _boom)
    rc = main(["--json", "edit", "simplify", "--paths", "app.py", "--enable"])
    assert rc == 1
    payload = _out(capsys)
    assert _code(payload) == "E_SIMPLIFY_ASSIGNMENT"
    assert payload.get("ok") is False
    assert called == []
    assert payload["blocked"] is True
    assert payload["assignment"]["role"] == "omg-code-simplifier"
    guard = json.loads(
        (project / ".omg" / "state" / "simplify-guard.json").read_text(encoding="utf-8")
    )
    assert guard["status"] == "assigned"
    assert "verified" not in guard
    assert "passes" not in guard


def test_simplify_provider_grok_writes_proposal_without_applying(
    project: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = "x = 1\n"
    src = project / "app.py"
    src.write_text(current, encoding="utf-8")
    descriptor = _descriptor("app.py", current, old_text="x = 1", replacement="x = 2")
    fake = _FakeGrokJob(
        "```json\n" + json.dumps({"descriptors": [descriptor]}) + "\n```\n"
    )
    _install_fake(monkeypatch, fake)

    rc = main(
        [
            "--json",
            "edit",
            "simplify",
            "--paths",
            "app.py",
            "--enable",
            "--provider",
            "grok",
        ]
    )
    assert rc == 0
    payload = _out(capsys)
    assert payload["ok"] is True
    assert payload["verified"] is False
    assert "passes" not in payload
    assert payload["status"] == "proposed"
    assert payload["provider"] == "grok"
    assert payload["job_id"] == JOB_ID
    assert payload["kind"] == "omg.edit.simplify.proposal.v1"
    assert payload["self_approve"] is False
    assert payload["independent_review_required"] is True
    assert payload["assignment"]["role"] == "omg-code-simplifier"
    assert src.read_text(encoding="utf-8") == current
    assert fake.start_calls
    assert fake.start_calls[0]["provider"] == "grok"
    assert fake.start_calls[0]["role"] == "omg-code-simplifier"
    prompt = str(fake.start_calls[0]["prompt_text"])
    assert "ONLY JSON" in prompt
    assert "x = 1" in prompt
    assert fake.start_calls[0]["provider_timeout_s"] == 90.0
    cwd = Path(str(fake.start_calls[0]["cwd"]))
    assert "simplify-sandbox" in cwd.as_posix()

    proposal_rel = payload["proposal"]
    assert proposal_rel == payload["artifact"]
    proposal_path = project.joinpath(*proposal_rel.split("/"))
    body = json.loads(proposal_path.read_text(encoding="utf-8"))
    assert body["kind"] == "omg.edit.simplify.proposal.v1"
    assert body["provider"] == "grok"
    assert body["job_id"] == JOB_ID
    assert "verified" not in body
    assert "passes" not in body
    assert body["descriptors"][0]["path"] == "app.py"
    assert body["descriptors"][0]["old_text"] == "x = 1"
    assert body["descriptors"][0]["replacement"] == "x = 2"

    guard = json.loads(
        (project / ".omg" / "state" / "simplify-guard.json").read_text(encoding="utf-8")
    )
    assert guard["status"] == "assigned"
    assert "verified" not in guard
    assert "passes" not in guard
    state_root = project / ".omg" / "state"
    for path in state_root.rglob("*"):
        if path.is_file():
            text = path.read_text(encoding="utf-8")
            assert '"verified": true' not in text
            assert '"passes": true' not in text


def test_simplify_provider_does_not_clobber_concurrent_user_edits(
    project: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = "x = 1\n"
    src = project / "app.py"
    src.write_text(current, encoding="utf-8")
    descriptor = _descriptor("app.py", current, old_text="x = 1", replacement="x = 2")
    fake = _FakeGrokJob(
        "```json\n" + json.dumps({"descriptors": [descriptor]}) + "\n```\n"
    )
    real_wait = fake.wait

    def _wait_and_edit(project_root: Path, job_id: str, **kwargs: object):
        src.write_text("user-edit\n", encoding="utf-8")
        return real_wait(project_root, job_id, **kwargs)

    fake.wait = _wait_and_edit  # type: ignore[method-assign]
    _install_fake(monkeypatch, fake)
    rc = main(
        [
            "--json",
            "edit",
            "simplify",
            "--paths",
            "app.py",
            "--enable",
            "--provider",
            "grok",
        ]
    )
    assert rc == 1
    payload = _out(capsys)
    assert _code(payload) == "E_SIMPLIFY_PROVIDER"
    assert src.read_text(encoding="utf-8") == "user-edit\n"


def test_simplify_provider_cancels_job_on_recovery_required(
    project: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omg_cli.jobs.models import JobStoreError

    (project / "app.py").write_text("x = 1\n", encoding="utf-8")
    fake = _FakeGrokJob("unused")
    cancelled: list[str] = []

    def _wait(*_args: object, **_kwargs: object) -> tuple[object, bool]:
        raise JobStoreError(
            "job requires recovery (health=lease_stale_live)",
            code="E_JOB_RECOVERY_REQUIRED",
        )

    def _cancel(_root: Path, job_id: str, *, reason: str = "") -> None:
        cancelled.append(f"{job_id}:{reason}")

    monkeypatch.setattr("omg_cli.edit_hygiene.simplify.start_job", fake.start)
    monkeypatch.setattr("omg_cli.edit_hygiene.simplify.wait_job", _wait)
    monkeypatch.setattr("omg_cli.edit_hygiene.simplify.cancel_job", _cancel)
    rc = main(
        [
            "--json",
            "edit",
            "simplify",
            "--paths",
            "app.py",
            "--enable",
            "--provider",
            "grok",
        ]
    )
    assert rc == 1
    payload = _out(capsys)
    assert _code(payload) == "E_SIMPLIFY_PROVIDER"
    assert cancelled == [f"{JOB_ID}:simplify-provider-recovery"]


def test_simplify_provider_grok_job_failure_records_assignment(
    project: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = "x = 1\n"
    src = project / "app.py"
    src.write_text(current, encoding="utf-8")
    fake = _FakeGrokJob("not json", fail=True)
    _install_fake(monkeypatch, fake)

    rc = main(
        [
            "--json",
            "edit",
            "simplify",
            "--paths",
            "app.py",
            "--enable",
            "--provider",
            "grok",
        ]
    )
    assert rc == 1
    payload = _out(capsys)
    assert _code(payload) == "E_SIMPLIFY_PROVIDER"
    assert payload.get("ok") is False
    assert payload.get("verified") is not True
    assert payload["assignment"]["role"] == "omg-code-simplifier"
    assert payload["job_id"] == JOB_ID
    assert src.read_text(encoding="utf-8") == current
    guard = json.loads(
        (project / ".omg" / "state" / "simplify-guard.json").read_text(encoding="utf-8")
    )
    assert guard["status"] == "assigned"
    assert "verified" not in guard
    arts = list((project / ".omg" / "artifacts" / "edit").glob("*.json"))
    assert arts
    assert all("omg.edit.simplify.proposal.v1" not in p.read_text(encoding="utf-8") for p in arts)


def test_simplify_provider_grok_missing_binary_records_assignment(
    project: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = "x = 1\n"
    (project / "app.py").write_text(current, encoding="utf-8")
    monkeypatch.setenv("PATH", "/nonexistent-omg-bin")
    monkeypatch.delenv("OMG_GROK_BIN", raising=False)

    def _no_wait(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("wait_job must not run when grok preflight fails")

    monkeypatch.setattr("omg_cli.edit_hygiene.simplify.wait_job", _no_wait)
    rc = main(
        [
            "--json",
            "edit",
            "simplify",
            "--paths",
            "app.py",
            "--enable",
            "--provider",
            "grok",
        ]
    )
    assert rc == 1
    payload = _out(capsys)
    assert _code(payload) == "E_SIMPLIFY_PROVIDER"
    assert payload["assignment"]["paths"] == ["app.py"]
    guard = json.loads(
        (project / ".omg" / "state" / "simplify-guard.json").read_text(encoding="utf-8")
    )
    assert guard["status"] == "assigned"
    assert (project / "app.py").read_text(encoding="utf-8") == current
    assert not (project / ".omg" / "jobs").exists()


def test_simplify_provider_grok_invalid_output_is_provider_error(
    project: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = "x = 1\n"
    (project / "app.py").write_text(current, encoding="utf-8")
    fake = _FakeGrokJob("sorry, here is a poem about simplicity")
    _install_fake(monkeypatch, fake)
    rc = main(
        [
            "--json",
            "edit",
            "simplify",
            "--paths",
            "app.py",
            "--enable",
            "--provider",
            "grok",
        ]
    )
    assert rc == 1
    payload = _out(capsys)
    assert _code(payload) == "E_SIMPLIFY_PROVIDER"
    assert payload["assignment"]["role"] == "omg-code-simplifier"
    assert (project / "app.py").read_text(encoding="utf-8") == current


def test_simplify_provider_cannot_combine_with_apply_edits(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (project / "app.py").write_text("x = 1\n", encoding="utf-8")
    edits = project / "edits.json"
    edits.write_text("[]", encoding="utf-8")
    rc = main(
        [
            "--json",
            "edit",
            "simplify",
            "--paths",
            "app.py",
            "--enable",
            "--provider",
            "grok",
            "--apply-edits",
            str(edits),
        ]
    )
    assert rc == 1
    payload = _out(capsys)
    assert _code(payload) == "E_SIMPLIFY_PROVIDER"
    assert not (project / ".omg" / "state" / "simplify-guard.json").exists()


def test_simplify_provider_cancels_job_on_non_recovery_wait_error(
    project: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omg_cli.jobs.models import JobStoreError

    (project / "app.py").write_text("x = 1\n", encoding="utf-8")
    fake = _FakeGrokJob("unused")
    cancelled: list[str] = []

    def _wait(*_args: object, **_kwargs: object) -> tuple[object, bool]:
        raise JobStoreError("orphan provider still live", code="E_JOB_ORPHAN")

    def _cancel(_root: Path, job_id: str, *, reason: str = "") -> None:
        cancelled.append(f"{job_id}:{reason}")

    monkeypatch.setattr("omg_cli.edit_hygiene.simplify.start_job", fake.start)
    monkeypatch.setattr("omg_cli.edit_hygiene.simplify.wait_job", _wait)
    monkeypatch.setattr("omg_cli.edit_hygiene.simplify.cancel_job", _cancel)
    rc = main(
        [
            "--json",
            "edit",
            "simplify",
            "--paths",
            "app.py",
            "--enable",
            "--provider",
            "grok",
        ]
    )
    assert rc == 1
    payload = _out(capsys)
    assert _code(payload) == "E_SIMPLIFY_PROVIDER"
    assert cancelled == [f"{JOB_ID}:simplify-provider-wait"]


def test_simplify_provider_cancel_unproven_is_fail_closed(
    project: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omg_cli.jobs.models import JobStoreError

    (project / "app.py").write_text("x = 1\n", encoding="utf-8")
    fake = _FakeGrokJob("unused")

    def _wait(*_args: object, **_kwargs: object) -> tuple[object, bool]:
        raise JobStoreError(
            "job requires recovery (health=orphan_provider_live)",
            code="E_JOB_RECOVERY_REQUIRED",
        )

    def _cancel(_root: Path, job_id: str, *, reason: str = "") -> None:
        del job_id, reason
        raise JobStoreError("cancel unproven", code="E_JOB_CANCEL_UNPROVEN")

    monkeypatch.setattr("omg_cli.edit_hygiene.simplify.start_job", fake.start)
    monkeypatch.setattr("omg_cli.edit_hygiene.simplify.wait_job", _wait)
    monkeypatch.setattr("omg_cli.edit_hygiene.simplify.cancel_job", _cancel)
    rc = main(
        [
            "--json",
            "edit",
            "simplify",
            "--paths",
            "app.py",
            "--enable",
            "--provider",
            "grok",
        ]
    )
    assert rc == 1
    payload = _out(capsys)
    assert _code(payload) == "E_SIMPLIFY_PROVIDER"
    assert "cancel failed" in str(payload.get("error", {}).get("message") or payload)


def test_simplify_provider_detects_unselected_tracked_mutation(
    project: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = "x = 1\n"
    (project / "app.py").write_text(current, encoding="utf-8")
    other = project / "other.py"
    other.write_text("y = 1\n", encoding="utf-8")
    _git_init(project)
    descriptor = _descriptor("app.py", current, old_text="x = 1", replacement="x = 2")
    fake = _FakeGrokJob(
        "```json\n" + json.dumps({"descriptors": [descriptor]}) + "\n```\n"
    )
    real_wait = fake.wait

    def _wait_and_edit(project_root: Path, job_id: str, **kwargs: object):
        other.write_text("y = 2\n", encoding="utf-8")
        return real_wait(project_root, job_id, **kwargs)

    fake.wait = _wait_and_edit  # type: ignore[method-assign]
    _install_fake(monkeypatch, fake)
    rc = main(
        [
            "--json",
            "edit",
            "simplify",
            "--paths",
            "app.py",
            "--enable",
            "--provider",
            "grok",
        ]
    )
    assert rc == 1
    payload = _out(capsys)
    assert _code(payload) == "E_SIMPLIFY_PROVIDER"
    assert (project / "app.py").read_text(encoding="utf-8") == current
    assert other.read_text(encoding="utf-8") == "y = 2\n"


def test_simplify_provider_rejects_wrong_producer(
    project: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = "x = 1\n"
    (project / "app.py").write_text(current, encoding="utf-8")
    descriptor = _descriptor("app.py", current, old_text="x = 1", replacement="x = 2")
    descriptor["producer"] = "omg-code-reviewer"
    fake = _FakeGrokJob(
        "```json\n" + json.dumps({"descriptors": [descriptor]}) + "\n```\n"
    )
    _install_fake(monkeypatch, fake)
    rc = main(
        [
            "--json",
            "edit",
            "simplify",
            "--paths",
            "app.py",
            "--enable",
            "--provider",
            "grok",
        ]
    )
    assert rc == 1
    payload = _out(capsys)
    assert _code(payload) == "E_SIMPLIFY_PROVIDER"
    assert (project / "app.py").read_text(encoding="utf-8") == current


def test_simplify_provider_empty_descriptors_are_no_changes(
    project: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = "x = 1\n"
    (project / "app.py").write_text(current, encoding="utf-8")
    fake = _FakeGrokJob('{"descriptors": []}\n')
    _install_fake(monkeypatch, fake)
    rc = main(
        [
            "--json",
            "edit",
            "simplify",
            "--paths",
            "app.py",
            "--enable",
            "--provider",
            "grok",
        ]
    )
    assert rc == 0
    payload = _out(capsys)
    assert payload["ok"] is True
    assert payload["verified"] is False
    assert payload["status"] == "no_changes"
    assert payload["descriptor_count"] == 0
    assert "apply-edits" not in str(payload.get("next_action") or "")
    assert (project / "app.py").read_text(encoding="utf-8") == current
