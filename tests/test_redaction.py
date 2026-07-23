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
