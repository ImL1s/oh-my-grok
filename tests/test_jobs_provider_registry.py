"""Jobs provider registry + Antigravity preflight (#68 PR2)."""

from __future__ import annotations

from pathlib import Path

import pytest

from omg_cli.jobs.models import JobStoreError
from omg_cli.jobs.providers import (
    get_provider_meta,
    preflight_antigravity,
    registered_provider_names,
    resolve_job_provider,
)
from omg_cli.jobs.runtime import start_job
from omg_cli.jobs.store import jobs_root

pytest_plugins = ["tests.antigravity_testutil"]


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / ".omg").mkdir()
    return tmp_path


def _prompt(root: Path, text: str = "do the thing") -> Path:
    p = root / "prompt.md"
    p.write_text(text, encoding="utf-8")
    return p


def test_job_provider_registry_accepts_fake_antigravity_and_grok() -> None:
    assert registered_provider_names() == ("fake", "antigravity", "grok")
    for name in ("fake", "antigravity", "grok"):
        adapter, meta = resolve_job_provider(name)
        assert meta.name == name
        assert adapter.name == name


def test_job_provider_registry_rejects_aliases() -> None:
    for alias in ("agy", "AGY", "Fake", " antigravity ", "claude"):
        # Leading/trailing whitespace is stripped then looked up — " antigravity "
        # becomes antigravity and succeeds; test true aliases only.
        pass
    for alias in ("agy", "claude", "codex", "unknown"):
        with pytest.raises(JobStoreError) as ei:
            resolve_job_provider(alias)
        assert ei.value.code == "E_JOB_PROVIDER"


def test_job_provider_registry_rejects_adapter_name_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omg_cli.jobs import providers as prov

    class _Bad:
        name = "not-fake"

        def discover_binary(self) -> str:
            return "x"

    monkeypatch.setitem(
        prov._REGISTRY,  # type: ignore[attr-defined]
        "fake",
        prov.JobProviderMeta(
            name="fake",
            factory=lambda: _Bad(),  # type: ignore[arg-type,return-value]
            allow_fake_flags=True,
            default_output_format="text",
            requires_preflight=False,
        ),
    )
    with pytest.raises(JobStoreError) as ei:
        resolve_job_provider("fake")
    assert ei.value.code == "E_JOB_PROVIDER"


def test_parent_and_runner_resolve_the_same_job_provider() -> None:
    from omg_cli.jobs import runner as runner_mod

    a, _ = resolve_job_provider("fake")
    b = runner_mod.resolve_adapter("fake")
    assert a.name == b.name == "fake"
    a2, _ = resolve_job_provider("antigravity")
    b2 = runner_mod.resolve_adapter("antigravity")
    assert a2.name == b2.name == "antigravity"
    a3, _ = resolve_job_provider("grok")
    b3 = runner_mod.resolve_adapter("grok")
    assert a3.name == b3.name == "grok"


def test_antigravity_preflight_rejects_missing_binary_before_job_materialization(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", "/nonexistent-omg-bin")
    monkeypatch.delenv("OMG_AGY_BIN", raising=False)
    prompt = _prompt(root)
    with pytest.raises(JobStoreError) as ei:
        start_job(
            root,
            provider="antigravity",
            role="researcher",
            prompt_file=prompt,
        )
    assert ei.value.code == "E_JOB_PROVIDER_MISSING"
    assert not jobs_root(root).exists() or not any(jobs_root(root).iterdir())


def test_antigravity_preflight_rejects_incompatible_binary_before_job_materialization(
    root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.antigravity_testutil import install_fake_agy

    bin_dir = tmp_path / "old-bin"
    install_fake_agy(bin_dir)
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("FAKE_AGY_VERSION", "0.1.0")
    monkeypatch.delenv("OMG_AGY_BIN", raising=False)
    prompt = _prompt(root)
    with pytest.raises(JobStoreError) as ei:
        start_job(
            root,
            provider="antigravity",
            role="researcher",
            prompt_file=prompt,
        )
    assert ei.value.code == "E_JOB_PROVIDER_COMPAT"
    assert not (jobs_root(root).exists() and any(jobs_root(root).iterdir()))


def test_antigravity_preflight_rejects_unsupported_output_format(
    root: Path, fake_agy_path: Path
) -> None:
    del fake_agy_path
    prompt = _prompt(root)
    with pytest.raises(JobStoreError) as ei:
        start_job(
            root,
            provider="antigravity",
            role="researcher",
            prompt_file=prompt,
            output_format="yaml",  # type: ignore[arg-type]
        )
    assert ei.value.code == "E_JOB_PROVIDER_OPTIONS"
    assert not (jobs_root(root).exists() and any(jobs_root(root).iterdir()))


def test_antigravity_preflight_pins_exact_binary_and_request_snapshot(
    root: Path, fake_agy_path: Path
) -> None:
    snap = preflight_antigravity(output_format="stream-json")
    assert snap.provider_binary == str(fake_agy_path)
    assert snap.provider_compat == "compatible"
    assert snap.output_format == "stream-json"
    assert "stream-json" in snap.observed_formats

    prompt = _prompt(root)
    result = start_job(
        root,
        provider="antigravity",
        role="researcher",
        prompt_file=prompt,
        model="grok",
        effort="high",
        mode="plan",
        output_format="json",
        provider_timeout_s=30.0,
    )
    rec = result.record
    assert rec.request["provider_binary"] == str(fake_agy_path)
    assert rec.request["output_format"] == "json"
    assert rec.request["model"] == "grok"
    assert rec.request["effort"] == "high"
    assert rec.request["mode"] == "plan"
    assert float(rec.request["timeout_s"]) == 30.0
    # public status must not expose absolute binary
    pub = rec.public_status()
    assert "provider_binary" not in (pub.get("request") or {})
    assert (pub.get("request") or {}).get("has_provider_binary") is True


def test_antigravity_start_rejects_fake_only_flags_before_job_materialization(
    root: Path, fake_agy_path: Path
) -> None:
    del fake_agy_path
    prompt = _prompt(root)
    with pytest.raises(JobStoreError) as ei:
        start_job(
            root,
            provider="antigravity",
            role="researcher",
            prompt_file=prompt,
            sleep_s=1.0,
        )
    assert ei.value.code == "E_JOB_PROVIDER_OPTIONS"
    assert not (jobs_root(root).exists() and any(jobs_root(root).iterdir()))


def test_get_provider_meta_exact() -> None:
    meta = get_provider_meta("fake")
    assert meta.allow_fake_flags is True
    meta2 = get_provider_meta("antigravity")
    assert meta2.requires_preflight is True
    meta3 = get_provider_meta("grok")
    assert meta3.requires_preflight is True
    assert meta3.allow_fake_flags is False



def test_public_job_start_rejects_internal_acp_provider(root: Path) -> None:
    from omg_cli.jobs.providers import (
        ACP_SESSION_PROVIDER,
        get_provider_meta,
        registered_provider_names,
        resolve_job_provider,
    )

    assert ACP_SESSION_PROVIDER not in registered_provider_names()
    assert "grok-acp-session" not in registered_provider_names()
    with pytest.raises(JobStoreError) as ei:
        resolve_job_provider("grok-acp-session")
    assert ei.value.code == "E_JOB_PROVIDER_INTERNAL"
    with pytest.raises(JobStoreError):
        get_provider_meta("grok-acp-session", allow_internal=False)
    adapter, meta = resolve_job_provider("grok-acp-session", allow_internal=True)
    assert meta.internal is True
    assert adapter.name == "grok-acp-session"

    prompt = _prompt(root)
    with pytest.raises(JobStoreError) as ei2:
        start_job(
            root,
            provider="grok-acp-session",
            role="researcher",
            prompt_file=prompt,
        )
    assert ei2.value.code == "E_JOB_PROVIDER_INTERNAL"


def test_retry_rejects_internal_provider() -> None:
    from omg_cli.jobs.providers import ACP_SESSION_PROVIDER, revalidate_stored_request
    from omg_cli.jobs.retry import assert_retry_admission
    from omg_cli.jobs.models import JobRecord, JobState

    rec = JobRecord(
        job_id="20260809T000000Z-deadbeef",
        created_at="2026-08-09T00:00:00+00:00",
        provider=ACP_SESSION_PROVIDER,
        role="acp-session",
        state=JobState.FAILED,
        attempt=1,
        attempt_budget=3,
        exit={"class": "nonzero", "retryable": True},
    )
    with pytest.raises(JobStoreError) as ei:
        assert_retry_admission(rec, attempt=2)
    assert ei.value.code == "E_JOB_PROVIDER_INTERNAL"
    with pytest.raises(JobStoreError) as ei2:
        revalidate_stored_request(ACP_SESSION_PROVIDER, {})
    assert ei2.value.code == "E_JOB_PROVIDER_INTERNAL"
