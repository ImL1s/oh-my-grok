"""Hermetic grok job provider (#69)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from omg_cli.jobs.models import JobState, JobStoreError
from omg_cli.jobs.providers import (
    preflight_grok,
    registered_provider_names,
    resolve_job_provider,
)
from omg_cli.jobs.runtime import start_job, wait_job
from omg_cli.jobs.store import job_dir, read_job_record
from omg_cli.team.launch import launch_worker

pytest_plugins = ["tests.jobs_grok_testutil"]


def test_job_provider_registry_includes_grok() -> None:
    assert registered_provider_names() == ("fake", "antigravity", "grok")
    adapter, meta = resolve_job_provider("grok")
    assert adapter.name == "grok"
    assert meta.allow_fake_flags is False
    assert meta.requires_preflight is True


def test_grok_preflight_rejects_missing_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", "/nonexistent-omg-bin")
    monkeypatch.delenv("OMG_GROK_BIN", raising=False)
    prompt = tmp_path / "p.md"
    prompt.write_text("hi", encoding="utf-8")
    with pytest.raises(JobStoreError) as ei:
        start_job(tmp_path, provider="grok", role="executor", prompt_file=prompt)
    assert ei.value.code == "E_JOB_PROVIDER_MISSING"


def test_grok_preflight_rejects_fake_flags(fake_grok_path: Path, tmp_path: Path) -> None:
    del fake_grok_path
    prompt = tmp_path / "p.md"
    prompt.write_text("hi", encoding="utf-8")
    with pytest.raises(JobStoreError) as ei:
        start_job(
            tmp_path,
            provider="grok",
            role="executor",
            prompt_file=prompt,
            sleep_s=1.0,
        )
    assert ei.value.code == "E_JOB_PROVIDER_OPTIONS"


def test_grok_job_runs_prompt_file(
    fake_grok_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del fake_grok_path
    monkeypatch.setenv("FAKE_GROK_ECHO_PROMPT", "1")
    (tmp_path / ".omg").mkdir()
    prompt = tmp_path / "p.md"
    prompt.write_text("do the grok job", encoding="utf-8")
    result = start_job(
        tmp_path,
        provider="grok",
        role="executor",
        prompt_file=prompt,
        provider_timeout_s=30.0,
    )
    rec = result.record
    assert rec.request["provider_binary"]
    assert rec.request["output_format"] == "text"
    wait_job(tmp_path, rec.job_id, timeout_s=30.0)
    done = read_job_record(tmp_path, rec.job_id)
    assert done.state == JobState.SUCCEEDED


def test_team_job_topology_admits_grok(
    fake_grok_path: Path, tmp_path: Path
) -> None:
    del fake_grok_path
    (tmp_path / ".omg").mkdir()
    handle = launch_worker(
        tmp_path,
        worker_id="w1",
        topology="job",
        provider="grok",
        role="executor",
        prompt_text="team grok job",
        dry_run=False,
    )
    assert handle.provider == "grok"
    assert handle.job_id
    wait_job(tmp_path, handle.job_id, timeout_s=30.0)
    assert read_job_record(tmp_path, handle.job_id).state == JobState.SUCCEEDED


def test_preflight_pins_binary(fake_grok_path: Path) -> None:
    snap = preflight_grok(output_format="text")
    assert snap.provider_binary == str(fake_grok_path)
    assert snap.provider_compat == "compatible"
    assert snap.output_format == "text"
    assert snap.provider_pin_revision == "unpinned"


def test_team_job_stamps_worktree_cwd(
    fake_grok_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del fake_grok_path
    monkeypatch.setenv("FAKE_GROK_ECHO_CWD", "1")
    (tmp_path / ".omg").mkdir()
    wt = tmp_path / "wt-w1"
    wt.mkdir()
    handle = launch_worker(
        tmp_path,
        worker_id="w1",
        topology="job",
        provider="grok",
        role="executor",
        prompt_text="team grok job",
        dry_run=False,
        cwd=wt,
    )
    rec = read_job_record(tmp_path, handle.job_id)
    assert rec.request["cwd"] == str(wt.resolve())
    wait_job(tmp_path, handle.job_id, timeout_s=30.0)
    done = read_job_record(tmp_path, handle.job_id)
    assert done.state == JobState.SUCCEEDED
    result = (job_dir(tmp_path, handle.job_id) / "artifacts" / "result.md").read_text(
        encoding="utf-8"
    )
    assert f"cwd={wt.resolve()}" in result


@pytest.mark.skipif(os.name != "posix", reason="symlink basename probe is POSIX")
def test_discover_binary_accepts_grok_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os as os_mod

    from omg_cli.jobs.grok import discover_binary
    from tests.jobs_grok_testutil import install_fake_grok

    real = install_fake_grok(tmp_path / "lib", name="grok-build-1.0")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    link = bin_dir / "grok"
    try:
        link.symlink_to(real)
    except OSError:
        pytest.skip("symlink creation requires privileges on this host")
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.delenv("OMG_GROK_BIN", raising=False)
    found = discover_binary(env=os_mod.environ)
    assert Path(found).name == "grok"
    assert Path(found).resolve() == real.resolve()


def test_discover_binary_absolutizes_relative_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omg_cli.jobs.grok import discover_binary
    from tests.jobs_grok_testutil import install_fake_grok

    install_fake_grok(tmp_path, name="grok")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OMG_GROK_BIN", "grok")
    found = discover_binary()
    assert os.path.isabs(found)
    assert Path(found).name == "grok"
    assert Path(found) == tmp_path / "grok"
