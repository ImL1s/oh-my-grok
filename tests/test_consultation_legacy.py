from __future__ import annotations

import importlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from omg_cli.ask.legacy import LEGACY_ASK_PROVIDERS, map_legacy_ask_record
from omg_cli.ask.registry import list_harness_specs, resolve_harness_id
from omg_cli.contracts.advisor_contract import reject_advisor_forbidden_keys
from omg_cli.contracts.consultation_contract import (
    CONSULTATION_RECEIPT_V1_KEYS,
    CONSULTATION_REQUEST_V1_KEYS,
    CONSULTATION_VIEW_V1_KEYS,
    COUNCIL_FAILURE_STATUSES,
    COUNCIL_STATUSES,
    consultation_receipt_digest,
    consultation_request_digest,
    consultation_view_from_receipt,
    parse_consultation_attempt_v1,
    parse_consultation_receipt_v1,
    parse_consultation_request_v1,
    parse_consultation_view_v1,
    parse_council_receipt_v1,
    parse_council_request_v1,
    parse_council_view_v1,
    validate_council_count_invariants,
)
from omg_cli.contracts.state_schemas import ContractValidationError
from omg_cli.contracts.writer_chain import canonical_json_bytes, sha256_hex


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "advisors"
DIGEST_A = "a" * 64
DIGEST_B = "b" * 64
DIGEST_C = "c" * 64
DIGEST_D = "d" * 64
STARTED = "2026-08-12T19:40:13Z"
TERMINAL = "2026-08-12T19:41:00Z"
LEGACY_OUTPUT_KEYS = {
    "legacy_field",
    "source_kind",
    "source_provider",
    "harness_id",
    "runtime_kind",
    "purpose",
    "lifecycle",
    "worker_eligible",
    "authoritative",
    "auto_apply",
}


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _prompt_artifact(**overrides: object) -> dict:
    artifact = {
        "kind": "prompt",
        "relative_path": "docs/prompts/consult.txt",
        "sha256": DIGEST_A,
        "byte_length": 12,
    }
    artifact.update(overrides)
    return artifact


def _valid_request(**overrides: object) -> dict:
    raw: dict = {
        "schema_version": 1,
        "consultation_id": "consult-1",
        "runtime_kind": "external_cli",
        "purpose": "advisory",
        "lifecycle": "foreground",
        "harness_id": "claude-cli",
        "original_task_digest": DIGEST_A,
        "prompt_artifact": _prompt_artifact(),
        "role_id": None,
        "role_prompt_digest": None,
        "requested_model": None,
        "requested_output": "text",
        "cwd_descriptor": {"kind": "repository", "relative_path": "."},
        "attachment_descriptors": [],
        "run_id": None,
        "timeout_s": 600.0,
        "max_output_bytes": 524288,
        "attempt_budget": 1,
        "policy_digest": DIGEST_B,
    }
    raw.update(overrides)
    return raw


def _valid_attempt(**overrides: object) -> dict:
    raw: dict = {
        "schema_version": 1,
        "attempt": 1,
        "harness_id": "claude-cli",
        "harness_version": None,
        "platform": "unspecified",
        "read_only_qualification": "unproven",
        "prompt_transport": "stdin",
        "job_id": None,
        "started_at": STARTED,
        "terminal_at": TERMINAL,
        "status": "succeeded",
        "exit_class": "ok",
        "output_present": True,
        "output_truncated": False,
        "response_digest": DIGEST_C,
        "receipt_digest": DIGEST_D,
    }
    raw.update(overrides)
    return raw


def _bound_attempt(receipt: dict, **overrides: object) -> dict:
    raw = _valid_attempt(
        harness_id=receipt["harness_id"],
        attempt=receipt["attempt"],
        receipt_digest=consultation_receipt_digest(receipt),
    )
    raw.update(overrides)
    return raw


def _valid_receipt(**overrides: object) -> dict:
    raw: dict = {
        "schema_version": 1,
        "consultation_id": "consult-1",
        "request_digest": DIGEST_A,
        "runtime_kind": "external_cli",
        "purpose": "advisory",
        "lifecycle": "foreground",
        "harness_id": "claude-cli",
        "attempt": 1,
        "status": "succeeded",
        "read_only_qualification": "unproven",
        "role_id": None,
        "requested_model": None,
        "selected_model": None,
        "job_id": None,
        "started_at": STARTED,
        "terminal_at": TERMINAL,
        "exit_class": "ok",
        "artifact_descriptors": [
            {
                "kind": "response",
                "relative_path": "docs/responses/consult.txt",
                "sha256": DIGEST_C,
                "byte_length": 20,
            }
        ],
        "private_transcript_available": False,
        "response_digest": DIGEST_C,
        "authoritative": False,
        "auto_apply": False,
        "worker_eligible": False,
    }
    raw.update(overrides)
    return raw


def _valid_council_request(**overrides: object) -> dict:
    claude = _valid_request()
    agy = _valid_request(
        consultation_id="consult-agy",
        harness_id="antigravity-cli",
        prompt_artifact=_prompt_artifact(relative_path="docs/prompts/agy.txt"),
    )
    raw: dict = {
        "schema_version": 1,
        "council_id": "council-1",
        "advisor_requests": [claude, agy],
        "concurrency_limit": 2,
        "timeout_s": 600.0,
        "minimum_successes": 1,
        "synthesis_mode": "none",
        "conflict_policy": "preserve_dissent",
        "policy_digest": DIGEST_B,
    }
    raw.update(overrides)
    return raw


def _lane_digests(count: int) -> list[str]:
    return [f"{index:064x}" for index in range(count)]


def _valid_council_receipt(**overrides: object) -> dict:
    raw: dict = {
        "schema_version": 1,
        "council_id": "council-1",
        "request_digest": DIGEST_A,
        "lane_receipt_digests": [DIGEST_B, DIGEST_C],
        "success_count": 1,
        "minimum_successes": 1,
        "status": "mixed",
        "synthesis_mode": "none",
        "synthesis_receipt_digest": None,
        "authoritative": False,
        "auto_apply": False,
        "worker_eligible": False,
    }
    raw.update(overrides)
    return raw


def _valid_council_view(**overrides: object) -> dict:
    raw: dict = {
        "schema_version": 1,
        "council_id": "council-1",
        "status": "mixed",
        "success_count": 1,
        "minimum_successes": 1,
        "lane_count": 2,
        "receipt_digest": DIGEST_A,
        "reasons": [],
        "authoritative": False,
        "auto_apply": False,
        "worker_eligible": False,
    }
    raw.update(overrides)
    return raw


def _allowed_terminal_statuses(
    *, success_count: int, minimum_successes: int, lane_count: int
) -> set[str]:
    if success_count == lane_count:
        return {"succeeded"}
    if minimum_successes <= success_count < lane_count:
        return {"mixed"}
    return set(COUNCIL_FAILURE_STATUSES)


def test_happy_path_request_attempt_receipt_view_flags_false() -> None:
    request = parse_consultation_request_v1(_valid_request())
    attempt = parse_consultation_attempt_v1(_valid_attempt())
    receipt = parse_consultation_receipt_v1(_valid_receipt())
    view = parse_consultation_view_v1(
        consultation_view_from_receipt(receipt, attempt=_bound_attempt(receipt))
    )
    assert request["timeout_s"] == 600
    assert request["harness_id"] == "claude-cli"
    assert set(request) == set(CONSULTATION_REQUEST_V1_KEYS)
    assert attempt["harness_version"] is None
    assert attempt["platform"] == "unspecified"
    assert set(receipt) == set(CONSULTATION_RECEIPT_V1_KEYS)
    assert set(view) == set(CONSULTATION_VIEW_V1_KEYS)
    for document in (receipt, view):
        assert document["authoritative"] is False
        assert document["auto_apply"] is False
        assert document["worker_eligible"] is False
        assert document["runtime_kind"] == "external_cli"
        assert document["purpose"] == "advisory"
        assert document["lifecycle"] == "foreground"


def test_request_digest_is_deterministic_and_changes_with_harness_id() -> None:
    parsed = parse_consultation_request_v1(_valid_request())
    first = consultation_request_digest(parsed)
    second = consultation_request_digest({"policy_digest": parsed["policy_digest"], **parsed})
    assert first == second
    assert first == sha256_hex(canonical_json_bytes(parsed))
    mutated = dict(parsed)
    mutated["harness_id"] = "codex-cli"
    assert consultation_request_digest(mutated) != first


@pytest.mark.parametrize(
    "parser,factory",
    [
        (parse_consultation_request_v1, _valid_request),
        (parse_consultation_receipt_v1, _valid_receipt),
        (parse_council_request_v1, _valid_council_request),
        (parse_council_receipt_v1, _valid_council_receipt),
        (parse_council_view_v1, _valid_council_view),
    ],
)
def test_future_schema_version_fails_closed(parser, factory) -> None:
    with pytest.raises(ContractValidationError, match="schema_version"):
        parser(factory(schema_version=2))


def test_unknown_key_fails_closed() -> None:
    raw = _valid_request()
    raw["unexpected"] = True
    with pytest.raises(ContractValidationError, match="extra"):
        parse_consultation_request_v1(raw)


@pytest.mark.parametrize("key", ["task_id", "worktree", "token", "member"])
def test_team_keys_on_request_mention_team(key: str) -> None:
    raw = _valid_request()
    raw[key] = "x"
    with pytest.raises(ContractValidationError, match="team") as excinfo:
        parse_consultation_request_v1(raw)
    assert "native" not in str(excinfo.value)


@pytest.mark.parametrize("key", ["provider", "catalog", "receipt", "access"])
def test_native_keys_on_request_mention_native(key: str) -> None:
    raw = _valid_request()
    raw[key] = "x"
    with pytest.raises(ContractValidationError, match="native") as excinfo:
        parse_consultation_request_v1(raw)
    assert "team" not in str(excinfo.value)


@pytest.mark.parametrize("key", ["argv", "prompt", "response", "api_key", "endpoint"])
def test_argv_prompt_response_credential_keys_rejected(key: str) -> None:
    raw = _valid_request()
    raw[key] = "x"
    with pytest.raises(ContractValidationError):
        parse_consultation_request_v1(raw)
    reject_payload = {"schema_version": 1, key: "x"}
    with pytest.raises(ContractValidationError):
        reject_advisor_forbidden_keys(reject_payload)


@pytest.mark.parametrize(
    "path",
    ["/usr/prompt.txt", "~/prompt.txt", "C:\\prompt.txt", "/private/tmp/prompt.txt"],
)
def test_absolute_private_artifact_paths_rejected(path: str) -> None:
    with pytest.raises(ContractValidationError, match="path"):
        parse_consultation_request_v1(
            _valid_request(prompt_artifact=_prompt_artifact(relative_path=path))
        )


@pytest.mark.parametrize(
    "path",
    ["/tmp/workspace", "~", "C:\\repo", ".omg/state", ".omg/state/runs/run-1"],
)
def test_absolute_private_cwd_rejected(path: str) -> None:
    with pytest.raises(ContractValidationError, match="path"):
        parse_consultation_request_v1(
            _valid_request(cwd_descriptor={"kind": "repository", "relative_path": path})
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("selected_model", "eyJhbGciOiJIUzI1NiJ9"),
        ("selected_model", "/Users/foo/model"),
        ("selected_model", "Bearer abc"),
    ],
)
def test_receipt_rejects_jwt_users_path_and_bearer(field: str, value: str) -> None:
    with pytest.raises(ContractValidationError, match="secret|private-path"):
        parse_consultation_receipt_v1(_valid_receipt(**{field: value}))


@pytest.mark.parametrize(
    "value",
    ["eyJhbGciOiJIUzI1NiJ9", "/Users/foo/model", "Bearer abc"],
)
def test_view_rejects_jwt_users_path_and_bearer(value: str) -> None:
    receipt = parse_consultation_receipt_v1(_valid_receipt(selected_model="safe-model"))
    view = consultation_view_from_receipt(receipt, attempt=1)
    view["reasons"] = [{"code": "leak", "message": value}]
    with pytest.raises(ContractValidationError, match="secret|private-path"):
        parse_consultation_view_v1(view)


def test_authoritative_true_on_receipt_rejected() -> None:
    with pytest.raises(ContractValidationError, match="authoritative"):
        parse_consultation_receipt_v1(_valid_receipt(authoritative=True))


def test_view_projector_copies_allowlist_only() -> None:
    receipt = parse_consultation_receipt_v1(
        _valid_receipt(requested_model="gpt-test", selected_model=None)
    )
    view = consultation_view_from_receipt(receipt, attempt=1, reasons=())
    assert set(view) == set(CONSULTATION_VIEW_V1_KEYS)
    assert "prompt_artifact" not in view
    assert "artifact_descriptors" not in view
    assert "request_digest" not in view
    assert view["model"] == "gpt-test"
    assert view["receipt_digest"] == consultation_receipt_digest(receipt)
    assert view["authoritative"] is False


def test_view_from_bound_attempt_matches_receipt_identity() -> None:
    receipt = parse_consultation_receipt_v1(_valid_receipt())
    attempt = parse_consultation_attempt_v1(_bound_attempt(receipt))
    view = consultation_view_from_receipt(receipt, attempt=attempt)
    assert view["consultation_id"] == receipt["consultation_id"]
    assert view["harness_id"] == receipt["harness_id"]
    assert view["attempt"] == receipt["attempt"]
    assert view["receipt_digest"] == consultation_receipt_digest(receipt)
    assert view["receipt_digest"] == attempt["receipt_digest"]


def test_view_rejects_attempt_harness_id_mismatch() -> None:
    receipt = parse_consultation_receipt_v1(_valid_receipt())
    attempt = _bound_attempt(receipt, harness_id="codex-cli")
    with pytest.raises(ContractValidationError, match="harness_id"):
        consultation_view_from_receipt(receipt, attempt=attempt)


def test_view_rejects_attempt_number_mismatch() -> None:
    receipt = parse_consultation_receipt_v1(_valid_receipt())
    attempt = _bound_attempt(receipt, attempt=2)
    with pytest.raises(ContractValidationError, match="attempt"):
        consultation_view_from_receipt(receipt, attempt=attempt)


@pytest.mark.parametrize("bad_digest", [DIGEST_D, "e" * 64])
def test_view_rejects_attempt_receipt_digest_mismatch(bad_digest: str) -> None:
    receipt = parse_consultation_receipt_v1(_valid_receipt())
    attempt = _bound_attempt(receipt, receipt_digest=bad_digest)
    with pytest.raises(ContractValidationError, match="receipt_digest"):
        consultation_view_from_receipt(receipt, attempt=attempt)


def test_view_rejects_attempt_injected_from_another_consultation() -> None:
    receipt_a = parse_consultation_receipt_v1(
        _valid_receipt(consultation_id="consult-1")
    )
    receipt_b = parse_consultation_receipt_v1(
        _valid_receipt(consultation_id="consult-2")
    )
    assert receipt_a["consultation_id"] != receipt_b["consultation_id"]
    assert consultation_receipt_digest(receipt_a) != consultation_receipt_digest(
        receipt_b
    )
    injected = _bound_attempt(receipt_b)
    with pytest.raises(ContractValidationError, match="receipt_digest"):
        consultation_view_from_receipt(receipt_a, attempt=injected)


def test_view_rejects_integer_attempt_mismatch() -> None:
    receipt = parse_consultation_receipt_v1(_valid_receipt())
    with pytest.raises(ContractValidationError, match="attempt"):
        consultation_view_from_receipt(receipt, attempt=2)


@pytest.mark.parametrize(
    "parser,factory",
    [
        (parse_consultation_attempt_v1, _valid_attempt),
        (parse_consultation_receipt_v1, _valid_receipt),
    ],
)
def test_v1_rejects_qualified_on_attempt_and_receipt(parser, factory) -> None:
    with pytest.raises(ContractValidationError, match="qualified"):
        parser(factory(read_only_qualification="qualified"))


def test_v1_rejects_qualified_on_view() -> None:
    receipt = parse_consultation_receipt_v1(_valid_receipt())
    view = consultation_view_from_receipt(receipt, attempt=_bound_attempt(receipt))
    view["read_only_qualification"] = "qualified"
    with pytest.raises(ContractValidationError, match="qualified"):
        parse_consultation_view_v1(view)


def test_request_rejects_structured_verdict_without_support() -> None:
    with pytest.raises(ContractValidationError, match="structured_verdict_v1"):
        parse_consultation_request_v1(
            _valid_request(requested_output="structured_verdict_v1")
        )


def test_council_rejects_advisor_synthesis_when_unproven() -> None:
    with pytest.raises(ContractValidationError, match="unproven"):
        parse_council_request_v1(
            _valid_council_request(synthesis_mode="advisor:claude-cli")
        )
    with pytest.raises(ContractValidationError, match="unproven"):
        parse_council_receipt_v1(
            _valid_council_receipt(synthesis_mode="advisor:claude-cli")
        )
    parsed = parse_council_request_v1(
        _valid_council_request(synthesis_mode="native:compose")
    )
    assert parsed["synthesis_mode"] == "native:compose"


@pytest.mark.parametrize("exit_class", ["error", "timeout", "cancelled", "usage", "missing"])
def test_succeeded_rejects_non_ok_exit_class(exit_class: str) -> None:
    with pytest.raises(ContractValidationError, match="succeeded"):
        parse_consultation_attempt_v1(_valid_attempt(exit_class=exit_class))
    with pytest.raises(ContractValidationError, match="succeeded"):
        parse_consultation_receipt_v1(_valid_receipt(exit_class=exit_class))


@pytest.mark.parametrize("exit_class", ["policy", "config", "auth"])
def test_policy_config_auth_are_not_v1_exit_classes(exit_class: str) -> None:
    with pytest.raises(ContractValidationError, match="exit_class"):
        parse_consultation_attempt_v1(_valid_attempt(exit_class=exit_class))
    with pytest.raises(ContractValidationError, match="exit_class"):
        parse_consultation_receipt_v1(_valid_receipt(exit_class=exit_class))


def test_output_present_equals_response_digest_presence() -> None:
    with pytest.raises(ContractValidationError, match="output_present"):
        parse_consultation_attempt_v1(
            _valid_attempt(output_present=True, response_digest=None)
        )
    with pytest.raises(ContractValidationError, match="output_present"):
        parse_consultation_attempt_v1(
            _valid_attempt(output_present=False, response_digest=DIGEST_C)
        )


def test_output_truncated_requires_output_present() -> None:
    with pytest.raises(ContractValidationError, match="output_truncated"):
        parse_consultation_attempt_v1(
            _valid_attempt(
                output_present=False,
                output_truncated=True,
                response_digest=None,
            )
        )
    parsed = parse_consultation_attempt_v1(
        _valid_attempt(output_present=True, output_truncated=True)
    )
    assert parsed["output_truncated"] is True
    receipt = parse_consultation_receipt_v1(_valid_receipt())
    view = consultation_view_from_receipt(receipt, attempt=1)
    view["output_present"] = False
    view["output_truncated"] = True
    with pytest.raises(ContractValidationError, match="output_truncated"):
        parse_consultation_view_v1(view)


def test_receipt_response_digest_matches_response_artifact() -> None:
    with pytest.raises(ContractValidationError, match="response_digest"):
        parse_consultation_receipt_v1(_valid_receipt(response_digest=None))
    with pytest.raises(ContractValidationError, match="response_digest"):
        parse_consultation_receipt_v1(_valid_receipt(response_digest=DIGEST_A))
    empty = parse_consultation_receipt_v1(
        _valid_receipt(response_digest=None, artifact_descriptors=[])
    )
    assert empty["response_digest"] is None
    assert empty["artifact_descriptors"] == []


def test_canonical_success_and_exit_0_empty_output() -> None:
    success = parse_consultation_receipt_v1(_valid_receipt())
    assert success["status"] == "succeeded"
    assert success["exit_class"] == "ok"
    assert success["response_digest"] == DIGEST_C
    empty = parse_consultation_receipt_v1(
        _valid_receipt(response_digest=None, artifact_descriptors=[])
    )
    assert empty["status"] == "succeeded"
    assert empty["exit_class"] == "ok"
    assert empty["response_digest"] is None
    empty_attempt = parse_consultation_attempt_v1(
        _valid_attempt(output_present=False, response_digest=None)
    )
    assert empty_attempt["output_present"] is False
    view = consultation_view_from_receipt(
        empty, attempt=_bound_attempt(empty, output_present=False, response_digest=None)
    )
    assert view["output_present"] is False
    assert view["output_truncated"] is False


@pytest.mark.parametrize(
    "field,value",
    [
        ("read_only_qualification", "unsupported"),
        ("job_id", "job-other"),
        ("started_at", "2026-08-12T18:00:00Z"),
        ("terminal_at", "2026-08-12T20:00:00Z"),
        ("status", "failed"),
        ("response_digest", DIGEST_A),
    ],
)
def test_view_rejects_one_field_attempt_receipt_mismatch(
    field: str, value: object
) -> None:
    receipt = parse_consultation_receipt_v1(_valid_receipt())
    kwargs = {field: value}
    if field == "status":
        kwargs["exit_class"] = "error"
    attempt = _bound_attempt(receipt, **kwargs)
    with pytest.raises(ContractValidationError, match=field):
        consultation_view_from_receipt(receipt, attempt=attempt)


def test_view_rejects_exit_class_mismatch_on_failed_receipt() -> None:
    receipt = parse_consultation_receipt_v1(
        _valid_receipt(
            status="failed",
            exit_class="error",
            response_digest=None,
            artifact_descriptors=[],
        )
    )
    attempt = _bound_attempt(
        receipt,
        status="failed",
        exit_class="timeout",
        output_present=False,
        response_digest=None,
    )
    with pytest.raises(ContractValidationError, match="exit_class"):
        consultation_view_from_receipt(receipt, attempt=attempt)


def test_view_derives_output_flags_from_receipt_digest() -> None:
    receipt = parse_consultation_receipt_v1(_valid_receipt())
    attempt = _bound_attempt(receipt, output_truncated=True)
    view = consultation_view_from_receipt(receipt, attempt=attempt)
    assert view["output_present"] is True
    assert view["output_truncated"] is True
    assert view["status"] == receipt["status"]
    assert view["read_only_qualification"] == receipt["read_only_qualification"]
    assert view["job_id"] == receipt["job_id"]
    assert view["started_at"] == receipt["started_at"]
    assert view["terminal_at"] == receipt["terminal_at"]


def test_council_request_nested_parse_unique_harness_and_agy_not_gemini() -> None:
    parsed = parse_council_request_v1(_valid_council_request())
    harness_ids = [item["harness_id"] for item in parsed["advisor_requests"]]
    assert harness_ids == ["claude-cli", "antigravity-cli"]
    assert "gemini-cli" not in harness_ids
    assert resolve_harness_id("agy") == "antigravity-cli"
    assert parsed["timeout_s"] == 600
    duplicate = _valid_council_request()
    duplicate["advisor_requests"][1]["harness_id"] = "claude-cli"
    with pytest.raises(ContractValidationError, match="harness_id"):
        parse_council_request_v1(duplicate)


def test_council_receipt_none_synthesis_requires_null_digest() -> None:
    parsed = parse_council_receipt_v1(_valid_council_receipt())
    assert parsed["synthesis_mode"] == "none"
    assert parsed["synthesis_receipt_digest"] is None
    with pytest.raises(ContractValidationError, match="synthesis_receipt_digest"):
        parse_council_receipt_v1(
            _valid_council_receipt(synthesis_receipt_digest=DIGEST_D)
        )


def test_council_receipt_lane_count_is_exact_digest_count() -> None:
    parsed = parse_council_receipt_v1(
        _valid_council_receipt(lane_receipt_digests=_lane_digests(3), success_count=2)
    )
    assert len(parsed["lane_receipt_digests"]) == 3
    view = parse_council_view_v1(
        _valid_council_view(lane_count=3, success_count=2, minimum_successes=2)
    )
    assert view["lane_count"] == 3
    assert view["success_count"] == 2


@pytest.mark.parametrize(
    "success_count,minimum_successes,lane_count",
    [(-1, 1, 2), (3, 1, 2), (1, 0, 2), (1, 3, 2), (0, 1, 0), (0, 1, 9)],
)
def test_council_count_bounds_rejected_by_helper(
    success_count: int, minimum_successes: int, lane_count: int
) -> None:
    with pytest.raises(ContractValidationError):
        validate_council_count_invariants(
            lane_count=lane_count,
            success_count=success_count,
            minimum_successes=minimum_successes,
            status="queued",
        )


def test_council_parsers_reject_negative_success_and_zero_minimum() -> None:
    with pytest.raises(ContractValidationError, match="success_count"):
        parse_council_receipt_v1(_valid_council_receipt(success_count=-1))
    with pytest.raises(ContractValidationError, match="success_count"):
        parse_council_view_v1(_valid_council_view(success_count=-1))
    with pytest.raises(ContractValidationError, match="minimum_successes"):
        parse_council_receipt_v1(_valid_council_receipt(minimum_successes=0))
    with pytest.raises(ContractValidationError, match="minimum_successes"):
        parse_council_view_v1(_valid_council_view(minimum_successes=0))
    with pytest.raises(ContractValidationError, match="lane_count"):
        parse_council_view_v1(_valid_council_view(lane_count=0, status="queued"))
    with pytest.raises(ContractValidationError, match="lane_count"):
        parse_council_view_v1(_valid_council_view(lane_count=9, status="queued"))


def test_council_view_and_receipt_share_count_helper() -> None:
    with pytest.raises(ContractValidationError, match="success_count"):
        parse_council_receipt_v1(
            _valid_council_receipt(
                lane_receipt_digests=_lane_digests(2),
                success_count=3,
                minimum_successes=1,
                status="queued",
            )
        )
    with pytest.raises(ContractValidationError, match="success_count"):
        parse_council_view_v1(
            _valid_council_view(
                lane_count=2, success_count=3, minimum_successes=1, status="queued"
            )
        )
    with pytest.raises(ContractValidationError, match="minimum_successes"):
        parse_council_receipt_v1(
            _valid_council_receipt(
                lane_receipt_digests=_lane_digests(2),
                success_count=0,
                minimum_successes=3,
                status="queued",
            )
        )
    with pytest.raises(ContractValidationError, match="minimum_successes"):
        parse_council_view_v1(
            _valid_council_view(
                lane_count=2, success_count=0, minimum_successes=3, status="queued"
            )
        )


def _council_matrix_cases() -> list[tuple[int, int, int, str]]:
    cases: list[tuple[int, int, int, str]] = []
    for lane_count, minimum_successes in ((3, 2), (1, 1)):
        for success_count in range(lane_count + 1):
            for status in sorted(COUNCIL_STATUSES):
                cases.append(
                    (lane_count, minimum_successes, success_count, status)
                )
    return cases


@pytest.mark.parametrize(
    "lane_count,minimum_successes,success_count,status",
    _council_matrix_cases(),
)
def test_council_terminal_truth_table(
    lane_count: int,
    minimum_successes: int,
    success_count: int,
    status: str,
) -> None:
    allowed_terminal = _allowed_terminal_statuses(
        success_count=success_count,
        minimum_successes=minimum_successes,
        lane_count=lane_count,
    )
    should_accept = status in {"queued", "running"} or status in allowed_terminal
    receipt = _valid_council_receipt(
        lane_receipt_digests=_lane_digests(lane_count),
        success_count=success_count,
        minimum_successes=minimum_successes,
        status=status,
    )
    view = _valid_council_view(
        lane_count=lane_count,
        success_count=success_count,
        minimum_successes=minimum_successes,
        status=status,
    )
    if should_accept:
        validate_council_count_invariants(
            lane_count=lane_count,
            success_count=success_count,
            minimum_successes=minimum_successes,
            status=status,
        )
        assert parse_council_receipt_v1(receipt)["status"] == status
        assert parse_council_view_v1(view)["status"] == status
        return
    with pytest.raises(ContractValidationError):
        validate_council_count_invariants(
            lane_count=lane_count,
            success_count=success_count,
            minimum_successes=minimum_successes,
            status=status,
        )
    with pytest.raises(ContractValidationError):
        parse_council_receipt_v1(receipt)
    with pytest.raises(ContractValidationError):
        parse_council_view_v1(view)


def test_council_mixed_impossible_when_lane_and_minimum_are_one() -> None:
    with pytest.raises(ContractValidationError, match="mixed"):
        parse_council_receipt_v1(
            _valid_council_receipt(
                lane_receipt_digests=_lane_digests(1),
                success_count=1,
                minimum_successes=1,
                status="mixed",
            )
        )
    with pytest.raises(ContractValidationError, match="mixed"):
        parse_council_view_v1(
            _valid_council_view(
                lane_count=1, success_count=0, minimum_successes=1, status="mixed"
            )
        )
    parsed = parse_council_receipt_v1(
        _valid_council_receipt(
            lane_receipt_digests=_lane_digests(1),
            success_count=1,
            minimum_successes=1,
            status="succeeded",
        )
    )
    assert parsed["status"] == "succeeded"
    assert len(parsed["lane_receipt_digests"]) == 1


@pytest.mark.parametrize(
    "name,provider,harness_id",
    [
        ("legacy-ask-claude.json", "claude", "claude-cli"),
        ("legacy-ask-fable.json", "fable", "claude-cli"),
        ("legacy-ask-agy.json", "agy", "antigravity-cli"),
        ("legacy-ask-codex.json", "codex", "codex-cli"),
        ("legacy-ask-gemini.json", "gemini", "gemini-cli"),
    ],
)
def test_legacy_map_each_provider(name: str, provider: str, harness_id: str) -> None:
    source = _load(name)
    mapped = map_legacy_ask_record(source)
    assert mapped["legacy_field"] is True
    assert mapped["source_kind"] == "ask"
    assert mapped["source_provider"] == provider
    assert mapped["harness_id"] == harness_id
    assert mapped["runtime_kind"] == "external_cli"
    assert mapped["purpose"] == "advisory"
    assert mapped["lifecycle"] == "foreground"
    assert mapped["worker_eligible"] is False
    assert mapped["authoritative"] is False
    assert mapped["auto_apply"] is False
    assert set(mapped) == LEGACY_OUTPUT_KEYS
    assert "argv" not in mapped
    assert "cwd" not in mapped
    assert "prompt" not in mapped
    assert "artifact" not in mapped
    if provider == "agy":
        assert mapped["harness_id"] != "gemini-cli"
    if provider == "fable":
        assert mapped["harness_id"] == "claude-cli"


def test_legacy_field_true_for_all_five_providers() -> None:
    assert LEGACY_ASK_PROVIDERS == frozenset(
        {"codex", "claude", "fable", "gemini", "agy"}
    )
    for name in (
        "legacy-ask-claude.json",
        "legacy-ask-fable.json",
        "legacy-ask-agy.json",
        "legacy-ask-codex.json",
        "legacy-ask-gemini.json",
    ):
        assert map_legacy_ask_record(_load(name))["legacy_field"] is True


@pytest.mark.parametrize(
    "name,family",
    [
        ("negative-team-worker-envelope.json", "team"),
        ("negative-native-spawn-receipt.json", "team|native"),
        ("negative-medley-catalog.json", "native"),
        ("negative-native-spawn-only.json", "native"),
    ],
)
def test_team_and_native_fixtures_are_not_consultations(name: str, family: str) -> None:
    with pytest.raises(ContractValidationError, match=family) as excinfo:
        map_legacy_ask_record(_load(name))
    message = str(excinfo.value)
    assert "consultation" not in message


@pytest.mark.parametrize(
    "key,value",
    [
        ("runtime_kind", "native_host"),
        ("purpose", "task_execution"),
        ("lifecycle", "team_member"),
        ("lifecycle", "background_job"),
        ("worker_eligible", True),
        ("authoritative", True),
        ("auto_apply", True),
    ],
)
def test_mapper_rejects_contradictory_taxonomy_or_flags(
    key: str, value: object
) -> None:
    raw = _load("legacy-ask-claude.json")
    raw[key] = value
    with pytest.raises(ContractValidationError, match=key):
        map_legacy_ask_record(raw)


def test_mapper_accepts_matching_supplied_taxonomy() -> None:
    raw = _load("legacy-ask-claude.json")
    raw["runtime_kind"] = "external_cli"
    raw["purpose"] = "advisory"
    raw["lifecycle"] = "foreground"
    raw["worker_eligible"] = False
    raw["authoritative"] = False
    raw["auto_apply"] = False
    mapped = map_legacy_ask_record(raw)
    assert mapped["runtime_kind"] == "external_cli"
    assert mapped["authoritative"] is False


def test_mapper_accepts_genuine_historical_advisor_route() -> None:
    mapped = map_legacy_ask_record(_load("legacy-ask-claude-route.json"))
    assert mapped["harness_id"] == "claude-cli"
    assert mapped["source_provider"] == "claude"
    assert "advisor_route" not in mapped


def test_mapper_accepts_alias_provider_inside_matching_route() -> None:
    raw = _load("legacy-ask-claude.json")
    raw["advisor_route"] = {"provider": "fable"}
    mapped = map_legacy_ask_record(raw)
    assert mapped["harness_id"] == "claude-cli"


@pytest.mark.parametrize(
    "key",
    ["task", "member", "worktree", "token", "medley", "provider_route"],
)
def test_mapper_rejects_one_key_team_or_native_on_ask_meta(key: str) -> None:
    raw = _load("legacy-ask-claude.json")
    raw[key] = "x"
    with pytest.raises(ContractValidationError, match="team|native"):
        map_legacy_ask_record(raw)


def test_mapper_rejects_unknown_ask_meta_keys() -> None:
    raw = _load("legacy-ask-claude.json")
    raw["verified"] = True
    with pytest.raises(ContractValidationError, match="extra"):
        map_legacy_ask_record(raw)


def test_mapper_rejects_bool_version() -> None:
    raw = _load("legacy-ask-claude.json")
    raw["version"] = True
    with pytest.raises(ContractValidationError, match="version"):
        map_legacy_ask_record(raw)


def test_mapper_rejects_null_nested_provider() -> None:
    raw = _load("legacy-ask-claude.json")
    raw["advisor_route"] = {"provider": None}
    with pytest.raises(ContractValidationError, match="provider"):
        map_legacy_ask_record(raw)


@pytest.mark.parametrize(
    "route",
    [
        {"provider": "codex"},
        {"authoritative": True},
        {"worker_eligible": True},
        {"auto_apply": True},
        {"runtime_kind": "native_host"},
        {"purpose": "task_execution"},
        {"lifecycle": "background_job"},
        {"task_id": "task-1"},
        {"model_route": "x"},
    ],
)
def test_mapper_rejects_contradictory_or_foreign_advisor_route(
    route: dict,
) -> None:
    raw = _load("legacy-ask-claude.json")
    raw["advisor_route"] = route
    with pytest.raises(ContractValidationError):
        map_legacy_ask_record(raw)


@pytest.mark.parametrize("provider", ["grok", "cursor", "fake", "antigravity"])
def test_ask_shaped_unsupported_providers_rejected(provider: str) -> None:
    raw = _load("legacy-ask-claude.json")
    raw["provider"] = provider
    with pytest.raises(ContractValidationError):
        map_legacy_ask_record(raw)


def test_mapper_and_parsers_do_not_probe_path_or_subprocess(monkeypatch) -> None:
    def explode(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("consultation/legacy must not probe PATH or spawn processes")

    monkeypatch.setattr(shutil, "which", explode)
    monkeypatch.setattr(subprocess, "run", explode)
    monkeypatch.setattr(subprocess, "Popen", explode)
    monkeypatch.setattr(subprocess, "check_output", explode)
    monkeypatch.setattr(subprocess, "call", explode)

    contract = importlib.reload(
        importlib.import_module("omg_cli.contracts.consultation_contract")
    )
    legacy = importlib.reload(importlib.import_module("omg_cli.ask.legacy"))
    parsed = contract.parse_consultation_request_v1(_valid_request())
    assert parsed["timeout_s"] == 600
    mapped = legacy.map_legacy_ask_record(_load("legacy-ask-fable.json"))
    assert mapped["legacy_field"] is True
    assert mapped["harness_id"] == "claude-cli"


def test_s1_registry_remains_unproven() -> None:
    specs = list_harness_specs()
    assert specs
    assert all(spec["advisor_read_only"] == "unproven" for spec in specs)
