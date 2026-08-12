from __future__ import annotations

import importlib
import shutil
import subprocess

import pytest

from omg_cli.ask.registry import (
    ALIAS_TO_HARNESS,
    CANONICAL_HARNESS_IDS,
    get_harness_spec,
    list_harness_specs,
    resolve_harness_id,
)
from omg_cli.contracts.advisor_contract import (
    ADVISOR_READ_ONLY_STATES,
    LIFECYCLES,
    PURPOSES,
    RUNTIME_KINDS,
    advisor_harness_spec_digest,
    parse_advisor_harness_spec_v1,
    validate_advisor_taxonomy,
)
from omg_cli.contracts.state_schemas import ContractValidationError
from omg_cli.contracts.writer_chain import canonical_json_bytes, sha256_hex


_SUPPORT_KEYS = (
    "supports_advisor",
    "supports_executor",
    "supports_background",
    "supports_structured_output",
    "supports_resume",
)


def _valid_spec(**overrides: object) -> dict:
    spec: dict = {
        "schema_version": 1,
        "harness_id": "claude-cli",
        "aliases": ["claude", "fable"],
        "binary_names": ["claude"],
        "identity_probe": "none",
        "version_probe": "none",
        "tested_versions": None,
        "platforms": [],
        "supports_advisor": False,
        "supports_executor": False,
        "supports_background": False,
        "supports_structured_output": False,
        "supports_resume": False,
        "prompt_transports": [],
        "preferred_prompt_transport": "",
        "needs_pty": False,
        "cancellation_strategy": "none",
        "default_timeout_s": 600.0,
        "max_output_bytes": 524288,
        "advisor_read_only": "unproven",
        "limitations": ["unproven: no pinned identity/version/behavior fixture"],
    }
    spec.update(overrides)
    return spec


def test_canonical_id_set_and_list_order_exact() -> None:
    assert RUNTIME_KINDS == ("native_host", "external_cli")
    assert PURPOSES == ("advisory", "task_execution")
    assert LIFECYCLES == ("foreground", "background_job", "team_member")
    assert CANONICAL_HARNESS_IDS == (
        "claude-cli",
        "codex-cli",
        "grok-cli",
        "cursor-cli",
        "antigravity-cli",
        "gemini-cli",
    )
    specs = list_harness_specs()
    assert tuple(spec["harness_id"] for spec in specs) == CANONICAL_HARNESS_IDS
    assert set(ALIAS_TO_HARNESS[harness_id] for harness_id in CANONICAL_HARNESS_IDS) == set(
        CANONICAL_HARNESS_IDS
    )


def test_alias_uniqueness_across_registry() -> None:
    seen: dict[str, str] = {}
    for spec in list_harness_specs():
        harness_id = spec["harness_id"]
        assert harness_id not in spec["aliases"]
        for alias in spec["aliases"]:
            assert alias not in CANONICAL_HARNESS_IDS
            assert alias not in seen
            seen[alias] = harness_id
            assert ALIAS_TO_HARNESS[alias] == harness_id
        assert ALIAS_TO_HARNESS[harness_id] == harness_id
    owners = list(ALIAS_TO_HARNESS.values())
    assert set(owners) == set(CANONICAL_HARNESS_IDS)
    assert len(ALIAS_TO_HARNESS) == len(set(ALIAS_TO_HARNESS))


def test_fable_and_antigravity_aliases() -> None:
    assert resolve_harness_id("fable") == "claude-cli"
    assert resolve_harness_id("FABLE") == "claude-cli"
    assert resolve_harness_id(" fable ") == "claude-cli"
    assert resolve_harness_id("agy") == "antigravity-cli"
    assert resolve_harness_id("antigravity") == "antigravity-cli"
    assert resolve_harness_id("claude-cli") == "claude-cli"


def test_agy_is_not_gemini_and_gemini_is_not_antigravity() -> None:
    assert resolve_harness_id("agy") != "gemini-cli"
    assert resolve_harness_id("gemini") != "antigravity-cli"
    assert resolve_harness_id("gemini") == "gemini-cli"
    assert ALIAS_TO_HARNESS["agy"] == "antigravity-cli"
    assert ALIAS_TO_HARNESS["gemini"] == "gemini-cli"
    with pytest.raises(ContractValidationError, match="unknown harness"):
        get_harness_spec("agy")
    with pytest.raises(ContractValidationError, match="unknown harness"):
        get_harness_spec("fable")


def test_every_registry_row_is_unproven_with_no_support_claim() -> None:
    for spec in list_harness_specs():
        parsed = parse_advisor_harness_spec_v1(spec)
        assert parsed["advisor_read_only"] == "unproven"
        assert parsed["advisor_read_only"] in ADVISOR_READ_ONLY_STATES
        for key in _SUPPORT_KEYS:
            assert parsed[key] is False
        assert parsed["tested_versions"] is None
        assert parsed["identity_probe"] == "none"
        assert parsed["version_probe"] == "none"
        assert parsed["platforms"] == []
        assert parsed["prompt_transports"] == []
        assert parsed["preferred_prompt_transport"] == ""
        assert parsed["needs_pty"] is False
        assert parsed["cancellation_strategy"] == "none"
        assert parsed["default_timeout_s"] == 600
        assert parsed["max_output_bytes"] == 524288
        assert parsed["limitations"]
        assert "worker_eligible" not in parsed
        assert "authoritative" not in parsed
        assert "auto_apply" not in parsed
        assert get_harness_spec(parsed["harness_id"])["harness_id"] == parsed["harness_id"]


def test_future_schema_version_fails_closed() -> None:
    with pytest.raises(ContractValidationError, match="schema_version"):
        parse_advisor_harness_spec_v1(_valid_spec(schema_version=2))


def test_unknown_key_fails_closed() -> None:
    raw = _valid_spec()
    raw["unexpected"] = True
    with pytest.raises(ContractValidationError, match="extra"):
        parse_advisor_harness_spec_v1(raw)


def test_unknown_advisor_read_only_enum_fails_closed() -> None:
    with pytest.raises(ContractValidationError, match="advisor_read_only"):
        parse_advisor_harness_spec_v1(_valid_spec(advisor_read_only="maybe"))


@pytest.mark.parametrize("key", ["task_id", "worktree", "token", "member"])
def test_team_forbidden_keys_fail_and_mention_team(key: str) -> None:
    raw = _valid_spec()
    raw[key] = "x"
    with pytest.raises(ContractValidationError, match="team") as excinfo:
        parse_advisor_harness_spec_v1(raw)
    assert "native" not in str(excinfo.value)


@pytest.mark.parametrize("key", ["provider", "catalog", "receipt", "access"])
def test_native_forbidden_keys_fail_and_mention_native(key: str) -> None:
    raw = _valid_spec()
    raw[key] = "x"
    with pytest.raises(ContractValidationError, match="native") as excinfo:
        parse_advisor_harness_spec_v1(raw)
    assert "team" not in str(excinfo.value)


@pytest.mark.parametrize(
    "binary",
    ["/usr/bin/claude", "~/bin/claude", "C:\\claude", "/private/tmp/claude"],
)
def test_absolute_home_private_binary_paths_fail(binary: str) -> None:
    with pytest.raises(ContractValidationError, match="basename"):
        parse_advisor_harness_spec_v1(_valid_spec(binary_names=[binary]))


@pytest.mark.parametrize("key", ["argv", "password", "prompt", "response", "secret"])
def test_argv_credential_prompt_response_keys_fail(key: str) -> None:
    raw = _valid_spec()
    raw[key] = "x"
    with pytest.raises(ContractValidationError):
        parse_advisor_harness_spec_v1(raw)


def test_digest_is_deterministic_and_changes_when_harness_id_changes() -> None:
    spec = parse_advisor_harness_spec_v1(_valid_spec())
    first = advisor_harness_spec_digest(spec)
    second = advisor_harness_spec_digest({"limitations": spec["limitations"], **spec})
    assert first == second
    assert first == sha256_hex(canonical_json_bytes(spec))
    mutated = dict(spec)
    mutated["harness_id"] = "codex-cli"
    assert advisor_harness_spec_digest(mutated) != first
    other = advisor_harness_spec_digest(get_harness_spec("codex-cli"))
    assert other != first


def test_registry_construction_does_not_probe_path_or_subprocess(monkeypatch) -> None:
    def explode(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("registry must not probe PATH or spawn processes")

    monkeypatch.setattr(shutil, "which", explode)
    monkeypatch.setattr(subprocess, "run", explode)
    monkeypatch.setattr(subprocess, "Popen", explode)
    monkeypatch.setattr(subprocess, "check_output", explode)
    monkeypatch.setattr(subprocess, "call", explode)

    contract = importlib.reload(importlib.import_module("omg_cli.contracts.advisor_contract"))
    registry = importlib.reload(importlib.import_module("omg_cli.ask.registry"))
    specs = registry.list_harness_specs()
    assert len(specs) == 6
    assert registry.resolve_harness_id("fable") == "claude-cli"
    assert contract.parse_advisor_harness_spec_v1(specs[0])["harness_id"] == "claude-cli"


@pytest.mark.parametrize(
    "name",
    ["fable\x00", "fable\n", "顾问", "顧問", "a" * 129, None, 1, "", "   "],
)
def test_control_cjk_overlong_and_empty_aliases_rejected(name: object) -> None:
    with pytest.raises(ContractValidationError) as excinfo:
        resolve_harness_id(name)  # type: ignore[arg-type]
    message = str(excinfo.value)
    assert "PATH" not in message
    assert "binary missing" not in message
    if isinstance(name, str) and len(name.strip()) > 128:
        assert "overlong" in message
    elif isinstance(name, str) and name.strip() and "\x00" not in name and "\n" not in name:
        assert "unknown harness" in message


def test_unknown_harness_message_does_not_mention_path() -> None:
    with pytest.raises(ContractValidationError, match="unknown harness") as excinfo:
        resolve_harness_id("not-a-harness")
    assert "PATH" not in str(excinfo.value)
    assert "binary" not in str(excinfo.value).lower()


def test_advisor_flags_cannot_be_true_and_spec_rejects_those_keys() -> None:
    for flag in ("worker_eligible", "authoritative", "auto_apply"):
        accepted = validate_advisor_taxonomy(
            runtime_kind="external_cli",
            purpose="advisory",
            lifecycle="foreground",
            **{flag: False},
        )
        assert accepted[flag] is False
        with pytest.raises(ContractValidationError, match=flag):
            validate_advisor_taxonomy(
                runtime_kind="external_cli",
                purpose="advisory",
                lifecycle="foreground",
                **{flag: True},
            )
        raw = _valid_spec()
        raw[flag] = False
        with pytest.raises(ContractValidationError, match=flag):
            parse_advisor_harness_spec_v1(raw)


def test_validate_advisor_taxonomy_accepts_only_advisor_legal_subset() -> None:
    for lifecycle in ("foreground", "background_job"):
        result = validate_advisor_taxonomy(
            {
                "runtime_kind": "external_cli",
                "purpose": "advisory",
                "lifecycle": lifecycle,
            }
        )
        assert result["runtime_kind"] == "external_cli"
        assert result["purpose"] == "advisory"
        assert result["lifecycle"] == lifecycle
        assert result["worker_eligible"] is False
        assert result["authoritative"] is False
        assert result["auto_apply"] is False

    with pytest.raises(ContractValidationError, match="native_host"):
        validate_advisor_taxonomy(
            runtime_kind="native_host",
            purpose="advisory",
            lifecycle="foreground",
        )
    with pytest.raises(ContractValidationError, match="task_execution"):
        validate_advisor_taxonomy(
            runtime_kind="external_cli",
            purpose="task_execution",
            lifecycle="foreground",
        )
    with pytest.raises(ContractValidationError, match="team_member"):
        validate_advisor_taxonomy(
            runtime_kind="external_cli",
            purpose="advisory",
            lifecycle="team_member",
        )
    with pytest.raises(ContractValidationError, match="unknown runtime_kind"):
        validate_advisor_taxonomy(
            runtime_kind="sidecar",
            purpose="advisory",
            lifecycle="foreground",
        )
