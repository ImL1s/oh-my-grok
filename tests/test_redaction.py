from __future__ import annotations

import json
import time

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
    assert REDACTED in out
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


def test_redact_text_matches_is_sensitive_key_compound_forms() -> None:
    """Free-text must honor the same predicate as structured keys."""
    cases = (
        ("account_number=123", "123"),
        ("model_name=-7", "-7"),
        ("quota.remaining=42", "42"),
        ("auth_token=abc", "abc"),
        ("customer_account_ref=9", "9"),
        ('prompt="hello world"', "hello world"),
        ("command='rm -rf /tmp/x'", "rm -rf"),
        ("https://x.test/?model_name=secret&ok=1", "secret"),
    )
    for source, leaked in cases:
        out = redact_text(source)
        assert leaked not in out, (source, out)
        assert REDACTED in out, (source, out)


def test_redact_text_leaves_non_sensitive_assignments() -> None:
    source = "path=/usr/bin count=3 ok=true"
    assert redact_text(source) == source


def test_redact_text_quoted_query_and_nested_assign_forms() -> None:
    quoted = 'https://x.test/?prompt="hello world"&ok=1'
    out = redact_text(quoted)
    assert "hello world" not in out
    assert 'world"' not in out
    assert "ok=1" in out
    assert REDACTED in out

    nested = "detail=token=secret-value"
    out2 = redact_text(nested)
    assert "secret-value" not in out2
    assert "token=[REDACTED]" in out2

    key_leak = redact_value({"detail=token=secret-value": "x"})
    body = json.dumps(key_leak)
    assert "secret-value" not in body


def test_redact_text_odd_key_shapes_reach_is_sensitive_key() -> None:
    cases = (
        "2fa_token=secret",
        "_api_key=secret",
        "api key=secret",
        "headers[api_key]=secret",
        "api%2Ekey=secret",
        "api+key=secret",
        '?api+key=super-secret&ok=1',
        'headers["api_key"]=super-secret',
        "headers['api_key']=super-secret",
    )
    for source in cases:
        out = redact_text(source)
        assert "secret" not in out, (source, out)
        assert REDACTED in out, (source, out)


def test_redact_text_unclosed_quotes_and_equals_in_values() -> None:
    unclosed_q = '?prompt="first super-secret'
    out = redact_text(unclosed_q)
    assert "super-secret" not in out
    assert REDACTED in out

    unclosed_h = 'Authorization: Bearer "first super-secret'
    out_h = redact_text(unclosed_h)
    assert "super-secret" not in out_h
    assert out_h.startswith("Authorization: [REDACTED]")

    for source in (
        "token=first=super-secret",
        "?token=first=super-secret&ok=1",
        "detail=token=first=super-secret",
        "token==super-secret",
    ):
        out = redact_text(source)
        assert "super-secret" not in out, (source, out)
        assert "token=[REDACTED]" in out or "?token=[REDACTED]" in out, (source, out)


def test_redact_text_authorization_redacts_full_header_line() -> None:
    digest = (
        'Authorization: Digest username="alice", response="super-secret"'
    )
    out = redact_text(digest)
    assert "alice" not in out and "super-secret" not in out
    assert out == "Authorization: [REDACTED]"

    aws = (
        "Authorization: AWS4-HMAC-SHA256 Credential=AKIAEXAMPLE/x, "
        "SignedHeaders=host, Signature=super-secret"
    )
    out_aws = redact_text(aws)
    assert "AKIAEXAMPLE" not in out_aws and "super-secret" not in out_aws
    assert out_aws == "Authorization: [REDACTED]"


def test_redact_text_closes_pr94n_residual_p2_classes() -> None:
    quoted_comma = 'prompt="hello, super-secret"'
    out = redact_text(quoted_comma)
    assert "super-secret" not in out
    assert out == f"prompt={REDACTED}"

    quoted_semi = 'prompt="hello; super-secret"'
    assert "super-secret" not in redact_text(quoted_semi)

    for source in (
        "api/key=secret",
        'headers[ "api_key" ]=secret',
        r'headers[\"api_key\"]=secret',
    ):
        out = redact_text(source)
        assert "secret" not in out, (source, out)
        assert REDACTED in out, (source, out)

    env_auth = (
        'HTTP_AUTHORIZATION=Digest username="u", response="deadbeef"'
    )
    out_env = redact_text(env_auth)
    assert "deadbeef" not in out_env and "username" not in out_env
    assert out_env == f"HTTP_AUTHORIZATION={REDACTED}"

    bracket_auth = (
        'headers["Authorization"]=AWS4-HMAC-SHA256 Credential=AKIA/x, '
        "Signature=abcdef"
    )
    out_b = redact_text(bracket_auth)
    assert "AKIA" not in out_b and "abcdef" not in out_b
    assert REDACTED in out_b

    for source, leaked in (
        ("password=;hunter2", "hunter2"),
        ("token=,secret", "secret"),
        ("command=;rm -rf /", "rm -rf"),
    ):
        out = redact_text(source)
        assert leaked not in out, (source, out)
        assert REDACTED in out, (source, out)


def test_redact_text_closes_pr94o_residual_p2_classes() -> None:
    overlong = "token" + ("a" * 257) + "=super-secret"
    out = redact_text(overlong)
    assert "super-secret" not in out
    assert REDACTED in out

    multi = "prompt=hello super-secret trailing"
    out_m = redact_text(multi)
    assert "super-secret" not in out_m
    assert out_m == f"prompt={REDACTED}"

    for source, leaked in (
        ("token://super-secret", "super-secret"),
        ("password://hunter2", "hunter2"),
    ):
        out = redact_text(source)
        assert leaked not in out, (source, out)
        assert REDACTED in out, (source, out)

    # Non-sensitive URL schemes must remain intact.
    assert "https://example.test/x" in redact_text("url=https://example.test/x")


def test_redact_text_closes_pr94p_residual_p2_classes() -> None:
    """Close Pro reaudit P2s: overlong key, quote/& tails, delimiter split, flood."""
    # Former 4096-cap fail-open: full key must still reach is_sensitive_key.
    overlong = "token" + ("a" * 4092) + "=super-secret"
    out = redact_text(overlong)
    assert "super-secret" not in out
    assert out.endswith(f"={REDACTED}")

    for source, leaked in (
        ('token="Bearer" super-secret', "super-secret"),
        ('command="rm" -rf /', "-rf /"),
        ('prompt="""hello super-secret"""', "super-secret"),
        ("command=echo safe & curl super-secret", "super-secret"),
        ("token=Bearer first&second-secret", "second-secret"),
    ):
        out = redact_text(source)
        assert leaked not in out, (source, out)
        assert REDACTED in out, (source, out)

    for source, leaked in (
        ("to;ken://super-secret", "super-secret"),
        ("api;key=super-secret", "super-secret"),
        ('headers["api;key"]=super-secret', "super-secret"),
    ):
        out = redact_text(source)
        assert leaked not in out, (source, out)
        assert REDACTED in out, (source, out)

    # Separator flood must stay O(n) (no per-separator 4096 backscan).
    flood = ":" * 12_000
    started = time.perf_counter()
    assert redact_text(flood) == flood
    assert time.perf_counter() - started < 1.0


def test_redact_text_closes_pr94q_residual_p2_classes() -> None:
    """Close Pro reaudit P2s: shell &, quoted bracket delimiters, ? flood."""
    # Bare shell ``&`` must not open query mode (EOL consumer for plain assign).
    for source, leaked in (
        ('echo safe & token="Bearer" super-secret', "super-secret"),
        ('echo safe & command="rm" -rf /', "-rf /"),
        ("echo safe & command=echo safe & curl super-secret", "super-secret"),
    ):
        out = redact_text(source)
        assert leaked not in out, (source, out)
        assert REDACTED in out, (source, out)

    # Quoted bracket keys keep &/?/:/= inside quotes so is_sensitive_key sees them.
    for source in (
        'headers["api&key"]=super-secret',
        'headers["api?key"]=super-secret',
        'headers["api:key"]=super-secret',
        'headers["api=key"]=super-secret',
        '["api&key"]=super-secret',
        '["api?key"]=super-secret',
        '["api:key"]=super-secret',
        '["api=key"]=super-secret',
    ):
        out = redact_text(source)
        assert "super-secret" not in out, (source, out)
        assert REDACTED in out, (source, out)
        assert out.endswith(f"={REDACTED}"), (source, out)

    # Query-marker flood must stay O(n) like colon flood (no per-marker find).
    n = 50_000
    q_flood = "?" * n
    c_flood = ":" * n
    t0 = time.perf_counter()
    assert redact_text(q_flood) == q_flood
    q_elapsed = time.perf_counter() - t0
    t0 = time.perf_counter()
    assert redact_text(c_flood) == c_flood
    c_elapsed = time.perf_counter() - t0
    # Allow generous slack for CI noise; catastrophic O(n²) is ~10–50× worse.
    assert q_elapsed < 1.0 and c_elapsed < 1.0
    assert q_elapsed < max(0.05, c_elapsed * 8.0), (q_elapsed, c_elapsed)

