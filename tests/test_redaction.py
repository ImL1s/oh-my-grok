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
