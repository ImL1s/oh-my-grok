from __future__ import annotations

import json

from omg_cli.redaction import REDACTED, redact_text, redact_value


def test_recursive_redaction_covers_frozen_secret_classes() -> None:
    value = {
        "headers": {
            "Authorization": "Bearer raw-auth-token",
            "Cookie": "sid=raw-cookie",
        },
        "url": "https://example.test/a?token=raw-query&ok=1",
        "env": {"API_KEY": "raw-api-key", "PATH": "/usr/bin"},
        "account": "acct-123",
        "model": "private-model",
        "quota": {"remaining": 7},
        "prompt": "raw prompt body",
        "command": "curl --header 'Authorization: Bearer raw-command-token'",
        "nested": ["password=raw-password", {"safe": "hello"}],
    }

    redacted = redact_value(value)
    body = json.dumps(redacted, sort_keys=True)
    for raw in (
        "raw-auth-token",
        "raw-cookie",
        "raw-query",
        "raw-api-key",
        "acct-123",
        "private-model",
        "raw prompt body",
        "raw-command-token",
        "raw-password",
    ):
        assert raw not in body
    assert redacted["headers"]["Authorization"] == REDACTED
    assert redacted["env"]["PATH"] == "/usr/bin"
    assert redacted["nested"][1]["safe"] == "hello"


def test_text_redaction_is_deterministic_and_preserves_safe_context() -> None:
    source = "failure url=https://x.test/?api_key=secret-value Authorization: Bearer token-value"
    first = redact_text(source)
    assert first == redact_text(source)
    assert "failure" in first and "https://x.test/" in first
    assert "secret-value" not in first and "token-value" not in first
    assert REDACTED in first


def test_redact_value_preserves_boolean_supports_models() -> None:
    """Key substring 'model' must not coerce supports.models bool into a string."""
    body = {
        "supports": {
            "models": True,
            "agents": False,
            "model": "private-model-name",
        },
        "version": "1.1.10 Authorization: Bearer raw-secret-token",
    }
    redacted = redact_value(body)
    assert redacted["supports"]["models"] is True
    assert redacted["supports"]["agents"] is False
    assert redacted["supports"]["model"] == REDACTED
    assert "raw-secret-token" not in redacted["version"]
    assert REDACTED in redacted["version"]


def test_redact_value_redacts_sensitive_integer_values_but_keeps_booleans() -> None:
    redacted = redact_value(
        {
            "token": 123456,
            "account_id": 998877,
            "quota": 42,
            "models": True,
        }
    )

    assert redacted["token"] == REDACTED
    assert redacted["account_id"] == REDACTED
    assert redacted["quota"] == REDACTED
    assert redacted["models"] is True


def test_redact_text_covers_account_model_quota_zero_and_negative() -> None:
    source = "initialization failed account_id=0 quota=-7 model_id:123 command_id=9"
    out = redact_text(source)
    assert "account_id=0" not in out
    assert "quota=-7" not in out
    assert "model_id:123" not in out
    assert "command_id=9" not in out
    assert out.count(REDACTED) >= 4
    assert "initialization failed" in out


def test_redact_value_redacts_free_text_and_mapping_keys_with_sensitive_ints() -> None:
    redacted = redact_value(
        {
            "detail": "account_id=0 quota=-7 model_id:123 command_id=9",
            "account_id=0": "value",
            "nested": ["quota: -3", {"safe": "ok", "token": 1}],
        }
    )
    body = json.dumps(redacted, sort_keys=True)
    assert "account_id=0" not in body
    assert "quota=-7" not in body
    assert "model_id:123" not in body
    assert "command_id=9" not in body
    assert "quota: -3" not in body
    assert redacted["nested"][1]["safe"] == "ok"
    assert "account_id=[REDACTED]" in redacted
    assert redacted["account_id=[REDACTED]"] == REDACTED

