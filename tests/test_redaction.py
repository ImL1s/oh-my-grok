from __future__ import annotations

import json
import sys
import time

from omg_cli.redaction import REDACTED, is_sensitive_key, redact_text, redact_value


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


def test_redact_text_closes_pr94r_residual_p2_classes() -> None:
    """Close Pro reaudit P2s: prose ``?`` query-mode leak; global quote skip."""
    # Arbitrary prose/shell ``?`` must not open query mode (EOL consumer).
    for source, leaked in (
        ('safe ? command="rm" -rf /', "-rf /"),
        ('safe ? x & token="Bearer" super-secret', "super-secret"),
        ("safe ? x & command=echo safe & curl super-secret", "super-secret"),
        ('maybe? token="Bearer" super-secret', "super-secret"),
        ('q?token="Bearer" super-secret', "super-secret"),
    ):
        out = redact_text(source)
        assert leaked not in out, (source, out)
        assert REDACTED in out, (source, out)

    # Real URL / clear query-string context still stops at ``&``.
    url = "https://x.test/?token=secret-value&ok=1"
    out_url = redact_text(url)
    assert "secret-value" not in out_url
    assert out_url == f"https://x.test/?token={REDACTED}&ok=1"
    assert redact_text("?api+key=super-secret&ok=1") == f"?api+key={REDACTED}&ok=1"

    # Free-text quoted assignments must redact (quote awareness is bracket-only).
    for source in (
        '"token=super-secret"',
        'echo "token=super-secret"',
        "msg 'api_key=super-secret'",
        r'\"token=super-secret\"',
        'say "unclosed token=super-secret',
        'prefix "token=super-secret" suffix',
    ):
        out = redact_text(source)
        assert "super-secret" not in out, (source, out)
        assert REDACTED in out, (source, out)

    # Bracket-quoted keys with embedded delimiters still redact (prior FIXED).
    for source in (
        'headers["api&key"]=super-secret',
        'headers["api?key"]=super-secret',
    ):
        out = redact_text(source)
        assert "super-secret" not in out, (source, out)
        assert out.endswith(f"={REDACTED}"), (source, out)


def test_redact_text_closes_pr94s_residual_p2_classes() -> None:
    """Close Pro reaudit P2s: query-token boundary, array quotes, key fragments."""
    # P2-1: real URL query must not leave query mode across whitespace into shell ``&``.
    for source, leaked in (
        (
            "https://x.test/?ok=1 safe & command=echo safe & curl super-secret",
            "super-secret",
        ),
        (
            "https://x.test/?ok=1 safe & token=Bearer first&second-secret",
            "second-secret",
        ),
    ):
        out = redact_text(source)
        assert leaked not in out, (source, out)
        assert REDACTED in out, (source, out)
        assert "super-secret" not in out and "second-secret" not in out

    # Real same-token query continuation still stops at ``&``.
    assert (
        redact_text("https://x.test/?token=secret-value&ok=1")
        == f"https://x.test/?token={REDACTED}&ok=1"
    )

    # P2-2: array / malformed bracket quotes are not key-quotes — still redact.
    for source in (
        '["token=super-secret"]',
        'args=["--token=super-secret"]',
        'echo [ "token=super-secret"',
        'headers["api_key]=super-secret',
        '[["token=super-secret"]]',
        'args=["--flag", "--token=super-secret"]',
    ):
        out = redact_text(source)
        assert "super-secret" not in out, (source, out)
        assert REDACTED in out, (source, out)

    # Real quoted bracket-keys still keep embedded delimiters literal.
    for source in (
        'headers["api&key"]=super-secret',
        'headers["api=key"]=super-secret',
        '["api=key"]=super-secret',
    ):
        out = redact_text(source)
        assert "super-secret" not in out, (source, out)
        assert out.endswith(f"={REDACTED}"), (source, out)

    # P2-3: structural delimiters must not fragment keys before is_sensitive_key.
    assert is_sensitive_key("api:key")
    assert is_sensitive_key("api=key")
    assert is_sensitive_key("headers[api:key]")
    assert is_sensitive_key("headers[api=key]")
    for source in (
        "api:key=super-secret",
        "api=key=super-secret",
        "headers[api:key]=super-secret",
        "headers[api=key]=super-secret",
    ):
        out = redact_text(source)
        assert "super-secret" not in out, (source, out)
        assert out.endswith(f"={REDACTED}"), (source, out)

    # Structured mapping and free-text share the same predicate (parity).
    assert redact_value({"api:key": "super-secret"}) == {"api:key": REDACTED}
    assert "super-secret" not in redact_text("api:key=super-secret")

    # Bracket / separator floods stay O(n).
    n = 50_000
    for flood in ("[" * n, ("?&" * (n // 2)), (":=" * (n // 2))):
        t0 = time.perf_counter()
        redact_text(flood)
        assert time.perf_counter() - t0 < 1.0


def test_redact_text_closes_pr94t_residual_p2_classes() -> None:
    """Close Pro reaudit P2s: query closers, bracket interior, quoted JSON keys, O(n)."""
    # P2-1: query state must not survive closing quote / JSON comma / shell ``&&``.
    for source, leaked in (
        (
            'curl "https://x.test/?ok=1"&&token=Bearer first&second-secret',
            "second-secret",
        ),
        (
            '{"url":"https://x.test/?ok=1","token":"Bearer first&second-secret"}',
            "second-secret",
        ),
    ):
        out = redact_text(source)
        assert leaked not in out, (source, out)
        assert "super-secret" not in out and "second-secret" not in out
        assert REDACTED in out, (source, out)

    # Prior whitespace query-boundary cases remain FIXED.
    for source, leaked in (
        (
            "https://x.test/?ok=1 safe & command=echo safe & curl super-secret",
            "super-secret",
        ),
        (
            "https://x.test/?ok=1 safe & token=Bearer first&second-secret",
            "second-secret",
        ),
    ):
        out = redact_text(source)
        assert leaked not in out, (source, out)
        assert REDACTED in out, (source, out)

    # P2-2: key-brackets are not opaque — interior assignments still redact.
    for source in (
        'headers["token=super-secret"]=safe',
        '["token=super-secret"]=safe',
    ):
        out = redact_text(source)
        assert "super-secret" not in out, (source, out)
        assert REDACTED in out, (source, out)

    mapped = redact_value({'headers["token=super-secret"]=safe': "x"})
    body = json.dumps(mapped)
    assert "super-secret" not in body
    assert REDACTED in body

    # Array / malformed cases from pr94s remain FIXED.
    for source in (
        '["token=super-secret"]',
        'args=["--token=super-secret"]',
        'echo [ "token=super-secret"',
        'headers["api_key]=super-secret',
    ):
        out = redact_text(source)
        assert "super-secret" not in out, (source, out)
        assert REDACTED in out, (source, out)

    # P2-3: quoted JSON keys keep ?/&/:// so is_sensitive_key parity holds.
    assert is_sensitive_key("api?key")
    assert is_sensitive_key("api&key")
    assert is_sensitive_key("api://key")
    for source in (
        '{"api?key":"super-secret"}',
        '{"api&key":"super-secret"}',
        '{"api://key":"super-secret"}',
    ):
        out = redact_text(source)
        assert "super-secret" not in out, (source, out)
        assert REDACTED in out, (source, out)

    assert redact_value({"api?key": "super-secret"}) == {"api?key": REDACTED}
    assert redact_value({"api&key": "super-secret"}) == {"api&key": REDACTED}
    assert redact_value({"api://key": "super-secret"}) == {"api://key": REDACTED}

    # P2-4: balanced nested non-key brackets must stay O(n), not O(n²).
    timings: list[float] = []
    for n in (1_000, 2_000, 4_000):
        source = "[" * n + "]" * n
        t0 = time.perf_counter()
        assert redact_text(source) == source
        timings.append(time.perf_counter() - t0)
    # Doubling n must not ~4× the time (quadratic). Absolute floor is above
    # 50ms so brief CI scheduler noise (observed ~70–90ms) does not flake.
    assert timings[2] < max(0.15, timings[0] * 10.0), timings
    n = 50_000
    t0 = time.perf_counter()
    redact_text("[" * n + "]" * n)
    assert time.perf_counter() - t0 < 1.0


def test_redact_text_closes_pr94u_residual_p2_classes() -> None:
    """Close Pro reaudit P2s: redirect query-end, quoted-key interior, floods."""
    # P2-1: query state must not survive shell redirection into a later ``&``.
    for source in (
        "curl https://x.test/?ok=1>out&token=Bearer first&second-secret",
        "curl https://x.test/?ok=1<in&token=Bearer first&second-secret",
        "curl https://x.test/?ok=1>>out&token=Bearer first&second-secret",
        "curl https://x.test/?ok=1<<in&token=Bearer first&second-secret",
        "curl https://x.test/?ok=12>out&token=Bearer first&second-secret",
    ):
        out = redact_text(source)
        assert "second-secret" not in out, (source, out)
        assert REDACTED in out, (source, out)

    # Prior query closers remain FIXED.
    for source, leaked in (
        (
            'curl "https://x.test/?ok=1"&&token=Bearer first&second-secret',
            "second-secret",
        ),
        (
            "https://x.test/?ok=1 safe & token=Bearer first&second-secret",
            "second-secret",
        ),
    ):
        out = redact_text(source)
        assert leaked not in out, (source, out)

    # P2-2: quoted object-key interiors are not opaque — assignments still redact.
    for source in (
        '{"token=super-secret":"safe"}',
        '{"api_key=super-secret":"safe"}',
    ):
        out = redact_text(source)
        assert "super-secret" not in out, (source, out)
        assert REDACTED in out, (source, out)

    mapped = redact_value({'"token=super-secret"': "safe"})
    body = json.dumps(mapped)
    assert "super-secret" not in body
    assert REDACTED in body

    # Quoted JSON keys still keep ?/&/:// as key material (pr94t FIXED).
    for source in (
        '{"api?key":"super-secret"}',
        '{"api&key":"super-secret"}',
        '{"api://key":"super-secret"}',
    ):
        out = redact_text(source)
        assert "super-secret" not in out, (source, out)
        assert REDACTED in out, (source, out)

    # Bracket interiors remain FIXED.
    for source in (
        'headers["token=super-secret"]=safe',
        '["token=super-secret"]=safe',
    ):
        out = redact_text(source)
        assert "super-secret" not in out, (source, out)

    # P2-3: escaped-quote flood must stay O(n), not O(n²) via _quoted_key_close.
    timings: list[float] = []
    for n in (1_600, 3_200, 6_400):
        source = '\\"' * n
        t0 = time.perf_counter()
        redact_text(source)
        timings.append(time.perf_counter() - t0)
    # Escaped-quote flood must stay near-linear; floor absorbs CI noise.
    assert timings[2] < max(0.15, timings[0] * 10.0), timings
    assert timings[2] < 1.0

    # P2-4: nested key-brackets must stay O(n) and must not RecursionError.
    old_limit = sys.getrecursionlimit()
    sys.setrecursionlimit(1000)
    try:
        nest_timings: list[float] = []
        for n in (200, 400, 800):
            source = "[" * n + "safe" + "]=x" * n
            t0 = time.perf_counter()
            redact_text(source)
            nest_timings.append(time.perf_counter() - t0)
        assert nest_timings[2] < max(0.15, nest_timings[0] * 10.0), nest_timings
        # Depth past the recursion ceiling of the prior sliced implementation.
        source = "[" * 975 + "safe" + "]=x" * 975
        t0 = time.perf_counter()
        redact_text(source)
        assert time.perf_counter() - t0 < 1.0
    finally:
        sys.setrecursionlimit(old_limit)


def test_redact_text_closes_pr94v_residual_p2_classes() -> None:
    """Close Pro reaudit P2s: sensitive-query clearer swallow; nest rewrite O(n)."""
    # P2-1: ``_consume_value`` must not jump over ``<>`` / quotes / JSON clearers
    # while leaving query mode on — later ``&token=…&second-secret`` would leak.
    for source, leaked in (
        (
            "curl https://x.test/?api_key=foo>out&token=Bearer first&second-secret",
            "second-secret",
        ),
        (
            "curl https://x.test/?token=x<in&command=echo safe&curl-secret",
            "curl-secret",
        ),
        (
            '?token=x","token":"Bearer first&second-secret"',
            "second-secret",
        ),
        (
            '?token=x"&&token=Bearer first&second-secret',
            "second-secret",
        ),
        (
            '{"url":"https://x.test/?token=foo","token":"Bearer first&second-secret"}',
            "second-secret",
        ),
    ):
        out = redact_text(source)
        assert leaked not in out, (source, out)
        assert "second-secret" not in out and "curl-secret" not in out
        assert REDACTED in out, (source, out)

    # Prior named redirect repros remain FIXED.
    for source in (
        "curl https://x.test/?ok=1>out&token=Bearer first&second-secret",
        "curl https://x.test/?ok=1<in&token=Bearer first&second-secret",
    ):
        out = redact_text(source)
        assert "second-secret" not in out, (source, out)

    # Real same-token query continuation still stops at ``&`` (no false EOL).
    assert (
        redact_text("https://x.test/?token=secret-value&ok=1")
        == f"https://x.test/?token={REDACTED}&ok=1"
    )

    # P2-2: nested key-bracket *rewrite* path must stay O(n), not O(n²).
    # (Unchanged ``safe`` nests were already linear; ``token=secret`` was not.)
    rewrite_timings: list[float] = []
    for n in (200, 400, 800, 1_600):
        source = "[" * n + "token=secret" + "]=x" * n
        t0 = time.perf_counter()
        out = redact_text(source)
        rewrite_timings.append(time.perf_counter() - t0)
        assert "secret" not in out, n
        assert REDACTED in out, n
    # Doubling n must not ~4× the time (quadratic). Allow CI slack.
    # Nest rewrite must stay near-linear. Absolute floor is slightly above
    # 50ms so brief CI scheduler noise (observed ~60ms) does not flake the
    # O(n) ratio guard.
    assert rewrite_timings[3] < max(0.1, rewrite_timings[0] * 10.0), rewrite_timings
    assert rewrite_timings[3] < 1.0, rewrite_timings
    # Small functional shape.
    assert "secret" not in redact_text("[[[token=secret]=x]=x]=x")


def test_redact_text_closes_pr94w_escaped_quote_key_p2() -> None:
    """P2: escaped quote inside quoted object-key must not split sensitive keys."""
    assert is_sensitive_key('to\\"ken')
    assert is_sensitive_key("to\\'ken")

    for source in (
        '{"to\\"ken=super-secret":"safe"}',
        "{'to\\'ken=super-secret':'safe'}",
        '{"api_\\"token=super-secret":"safe"}',
    ):
        out = redact_text(source)
        assert "super-secret" not in out, (source, out)
        assert REDACTED in out, (source, out)

    # Prior quoted-key interior FIXED cases still redact.
    for source in (
        '{"token=super-secret":"safe"}',
        '{"api_key=super-secret":"safe"}',
    ):
        out = redact_text(source)
        assert "super-secret" not in out, (source, out)

    # Structured value whose free-text field carries the same shape.
    mapped = redact_value({"body": '{"to\\"ken=super-secret":"safe"}'})
    body = json.dumps(mapped)
    assert "super-secret" not in body
    assert REDACTED in body


def test_redact_text_closes_pr94x_query_state_ws_bracket_hash_p2() -> None:
    """P2: whitespace / ``]`` / ``#`` must clear query mode (no ``&second-secret`` leak)."""
    for source in (
        "curl https://x.test/?api_key=foo bar&token=Bearer first&second-secret",
        "curl https://x.test/?api_key=foo]bar&token=Bearer first&second-secret",
        "curl https://x.test/?api_key=foo#frag&token=Bearer first&second-secret",
        # Tab mid-value (same class as space).
        "curl https://x.test/?api_key=foo\tbar&token=Bearer first&second-secret",
    ):
        out = redact_text(source)
        assert "second-secret" not in out, (source, out)
        assert "Bearer first" not in out, (source, out)
        assert REDACTED in out, (source, out)

    # Legitimate same-token query continuation still stops at ``&``.
    assert (
        redact_text("https://x.test/?token=secret-value&ok=1")
        == f"https://x.test/?token={REDACTED}&ok=1"
    )
    assert (
        redact_text('https://x.test/?prompt="hello world"&ok=1')
        == f"https://x.test/?prompt={REDACTED}&ok=1"
    )
    # Prior <> / quote clearer residuals remain FIXED.
    for source in (
        "curl https://x.test/?api_key=foo>out&token=Bearer first&second-secret",
        '?token=x","token":"Bearer first&second-secret"',
    ):
        out = redact_text(source)
        assert "second-secret" not in out, (source, out)


def test_redact_text_closes_pr94y_hash_quote_interior_cmdsub_p2() -> None:
    """Close Pro reaudit P2s: ``#`` key parity, quote-interior punct, ``$()``."""
    # P2-1: ``#`` clears query but must NOT hard-cut sensitive keys (predicate parity).
    assert is_sensitive_key("api#key")
    assert is_sensitive_key("to#ken")
    assert "super-secret" not in redact_text("api#key=super-secret")
    assert redact_text("api#key=super-secret").endswith(f"={REDACTED}")
    assert redact_value({"api#key": "super-secret"}) == {"api#key": REDACTED}
    out_hash_key = redact_text('{"to#ken=super-secret":"safe"}')
    assert "super-secret" not in out_hash_key
    assert REDACTED in out_hash_key
    # Fragment clearer still stops query inheritance (pr94x FIXED).
    out_frag = redact_text(
        "curl https://x.test/?api_key=foo#frag&token=Bearer first&second-secret"
    )
    assert "second-secret" not in out_frag and "Bearer first" not in out_frag
    assert REDACTED in out_frag

    # P2-2: quoted object-key interiors must not apply shell/query grammar to
    # literal punctuation — ``to?ken=`` / ``to'ken=`` stay one sensitive key.
    assert is_sensitive_key("to?ken")
    assert is_sensitive_key("to'ken")
    for source in (
        '{"to?ken=super-secret":"safe"}',
        '{"to\'ken=super-secret":"safe"}',
        '{"to#ken=super-secret":"safe"}',
        '{"to&ken=super-secret":"safe"}',
        '{"to,ken=super-secret":"safe"}',
    ):
        out = redact_text(source)
        assert "super-secret" not in out, (source, out)
        assert REDACTED in out, (source, out)
    # Prior escaped-quote + simple interior FIXED cases still redact.
    for source in (
        '{"to\\"ken=super-secret":"safe"}',
        '{"token=super-secret":"safe"}',
        '{"api?key":"super-secret"}',
        '{"api&key":"super-secret"}',
    ):
        out = redact_text(source)
        assert "super-secret" not in out, (source, out)

    # P2-3: query state must not cross shell ``$(`` into ``&second-secret``.
    cmdsub = r"curl https://x.test/?ok=1$(token=first\&second-secret)"
    out_cmd = redact_text(cmdsub)
    assert "second-secret" not in out_cmd, out_cmd
    assert "first" not in out_cmd, out_cmd
    assert REDACTED in out_cmd
    # Grouping opener without ``$`` is the same clearer class.
    out_paren = redact_text(
        "curl https://x.test/?ok=1(token=first&second-secret)"
    )
    assert "second-secret" not in out_paren
    assert REDACTED in out_paren


def test_redact_text_closes_pr94z_dollar_brace_bracket_query_p2() -> None:
    """Close Pro reaudit P2: ``${…}`` / ``$[…]`` clear query like ``$(``."""
    # Digraph clearers — with and without backslash before ``&``.
    for source in (
        r"curl https://x.test/?api_key=foo${token=first\&second-secret}",
        "curl https://x.test/?api_key=foo${token=first&second-secret}",
        r"?api_key=foo${token=first\&second-secret}",
        "?api_key=foo${token=first&second-secret}",
        r"curl https://x.test/?api_key=foo$[token=first\&second-secret]",
        "curl https://x.test/?api_key=foo$[token=first&second-secret]",
        r"?api_key=foo$[token=first\&second-secret]",
        "?api_key=foo$[token=first&second-secret]",
    ):
        out = redact_text(source)
        assert "second-secret" not in out, (source, out)
        assert "first" not in out, (source, out)
        assert REDACTED in out, (source, out)
    # Bare ``$`` alone must NOT clear_eol (contrast: digraphs above).
    bare = "?api_key=foo$bar&ok=visible-tail"
    assert redact_text(bare) == f"?api_key={REDACTED}&ok=visible-tail"


def test_redact_text_closes_pr94aa_bash_special_param_query_p2() -> None:
    """Close Pro reaudit P2: Bash special-parameter digraphs clear query.

    ``$@ $$ $* $? $- $! $0`` are complete shell expansions (not ``$`` + id).
    Mid-span clear must apply clear_eol so ``&second-secret`` cannot leak.
    """
    specials = ("$@", "$$", "$*", "$?", "$-", "$!", "$0")
    sources: list[str] = []
    for digraph in specials:
        sources.extend(
            (
                rf"curl https://x.test/?api_key=foo{digraph}token=first\&second-secret",
                f"curl https://x.test/?api_key=foo{digraph}token=first&second-secret",
                rf"?api_key=foo{digraph}token=first\&second-secret",
                f"?api_key=foo{digraph}token=first&second-secret",
            )
        )
    for source in sources:
        out = redact_text(source)
        assert "second-secret" not in out, (source, out)
        assert "first" not in out, (source, out)
        assert REDACTED in out, (source, out)
    # Bare ``$`` + normal identifier must NOT clear (contrast: specials above).
    bare = "?api_key=foo$bar&ok=visible-tail"
    assert redact_text(bare) == f"?api_key={REDACTED}&ok=visible-tail"


def test_redact_text_closes_pr94ab_hash_special_param_query_p2() -> None:
    """Close Pro reaudit P2: Bash ``$#`` digraph clears query before fragment ``#``.

    ``$#`` is the positional-parameter count; ``_consume_value`` must not treat
    the ``#`` as a URL fragment stop, and mid-span clear_eol must hide
    ``&second-secret``. Ordinary ``#`` fragment / ``api#key=`` key-parity stay.
    """
    for source in (
        r"curl https://x.test/?api_key=foo$#\&second-secret",
        "curl https://x.test/?api_key=foo$#&second-secret",
        r"?api_key=foo$#\&second-secret",
        "?api_key=foo$#&second-secret",
        r"curl https://x.test/?api_key=foo$#token=first\&second-secret",
        "curl https://x.test/?api_key=foo$#token=first&second-secret",
    ):
        out = redact_text(source)
        assert "second-secret" not in out, (source, out)
        assert "first" not in out, (source, out)
        assert REDACTED in out, (source, out)
    # Ordinary fragment ``#`` still clears query (pr94x/pr94y FIXED).
    out_frag = redact_text(
        "curl https://x.test/?api_key=foo#frag&token=Bearer first&second-secret"
    )
    assert "second-secret" not in out_frag and "Bearer first" not in out_frag
    assert REDACTED in out_frag
    # Key-parity: ``#`` inside keys must still reach is_sensitive_key.
    assert is_sensitive_key("api#key")
    assert "super-secret" not in redact_text("api#key=super-secret")
    # Bare ``$bar`` must NOT clear_eol (contrast: ``$#`` above).
    bare = "?api_key=foo$bar&ok=visible-tail"
    assert redact_text(bare) == f"?api_key={REDACTED}&ok=visible-tail"

