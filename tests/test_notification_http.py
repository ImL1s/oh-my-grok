"""HTTPS notifications are allowlisted, DNS-revalidated, pinned, and bounded."""
from __future__ import annotations

import pytest

from omg_cli.notify import http as http_adapter
from omg_cli.notify.events import create_notification_event
from omg_cli.notify.http import notify_https, public_address


OWNER = {
    "owner_id": "owner",
    "generation": 1,
    "owner_nonce": "owner-nonce-123456",
}
EVENT = create_notification_event(
    severity="warning",
    title="Gate",
    message="Review required token=private",
    created_at="2026-07-22T00:00:00Z",
    **OWNER,
)
TARGET = {
    "adapter": "https",
    "enabled": True,
    "url": "https://hooks.acme.example.net/omg",
    "allowed_hosts": ["hooks.acme.example.net"],
    "timeout_ms": 1000,
    "headers": {},
}


def test_https_disabled_performs_no_dns_or_transport():
    calls: list[str] = []
    result = notify_https(
        EVENT,
        {**TARGET, "enabled": False},
        owner=OWNER,
        resolver=lambda host: calls.append(f"dns:{host}"),
        transport=lambda request: calls.append(f"http:{request}"),
    )
    assert result["status"] == "skipped"
    assert calls == []


def test_https_revalidates_dns_pins_address_and_sends_redacted_payload():
    dns_calls: list[str] = []
    requests: list[dict] = []

    def resolve(host):
        dns_calls.append(host)
        return [{"address": "93.184.216.34", "family": 4}]

    def send(request):
        requests.append(request)
        return {"status_code": 204, "response_bytes": 0}

    result = notify_https(EVENT, TARGET, owner=OWNER, resolver=resolve, transport=send)
    assert result["status"] == "delivered"
    assert len(dns_calls) == 2
    assert requests[0]["address"] == {"address": "93.184.216.34", "family": 4}
    assert requests[0]["hostname"] == "hooks.acme.example.net"
    assert "private" not in requests[0]["payload"]
    assert OWNER["owner_nonce"] not in requests[0]["payload"]
    assert TARGET["url"] not in str(result)


@pytest.mark.parametrize(
    "address,family",
    [
        ("127.0.0.1", 4),
        ("10.0.0.1", 4),
        ("::1", 6),
        ("fc00::1", 6),
        ("::ffff:10.0.0.1", 6),
        ("::10.0.0.1", 6),
        ("64:ff9b::7f00:1", 6),
    ],
)
def test_https_rejects_non_public_dns(address, family):
    sent: list[dict] = []
    result = notify_https(
        EVENT,
        TARGET,
        owner=OWNER,
        resolver=lambda _host: [{"address": address, "family": family}],
        transport=lambda request: sent.append(request),
    )
    assert result["code"] == "HTTPS_DNS_REVALIDATION_REJECTED"
    assert sent == []


def test_https_fails_closed_on_dns_rebinding():
    count = 0

    def resolve(_host):
        nonlocal count
        count += 1
        address = "93.184.216.34" if count == 1 else "8.8.8.8"
        return [{"address": address, "family": 4}]

    result = notify_https(
        EVENT,
        TARGET,
        owner=OWNER,
        resolver=resolve,
        transport=lambda _request: {"status_code": 204, "response_bytes": 0},
    )
    assert result["code"] == "HTTPS_DNS_REVALIDATION_REJECTED"


@pytest.mark.parametrize(
    "url",
    [
        "http://hooks.acme.example.net/omg",
        "https://127.0.0.1/omg",
        "https://localhost/omg",
        "https://hooks.acme.example.net:444/omg",
        "https://hooks.acme.example.net/omg?token=secret",
        "https://other.example.net/omg",
    ],
)
def test_https_rejects_unsafe_endpoint_before_dns(url):
    calls: list[str] = []
    result = notify_https(
        EVENT,
        {**TARGET, "url": url},
        owner=OWNER,
        resolver=lambda host: calls.append(host),
    )
    assert result["code"] == "HTTPS_ENDPOINT_REJECTED"
    assert calls == []


def test_https_rejects_empty_or_mixed_dns_answers():
    for addresses in (
        [],
        [
            {"address": "93.184.216.34", "family": 4},
            {"address": "10.0.0.1", "family": 4},
        ],
    ):
        result = notify_https(
            EVENT,
            TARGET,
            owner=OWNER,
            resolver=lambda _host, rows=addresses: rows,
            transport=lambda _request: {"status_code": 204, "response_bytes": 0},
        )
        assert result["code"] == "HTTPS_DNS_REVALIDATION_REJECTED"


def test_https_rejects_header_injection_redirect_and_response_overflow():
    def dns(_host):
        return [{"address": "93.184.216.34", "family": 4}]
    unsafe = notify_https(
        EVENT,
        {**TARGET, "headers": {"x-test": "safe\r\ninjected: yes"}},
        owner=OWNER,
        resolver=dns,
    )
    assert unsafe["code"] == "HTTPS_ENDPOINT_REJECTED"

    redirect = notify_https(
        EVENT,
        TARGET,
        owner=OWNER,
        resolver=dns,
        transport=lambda _request: {"status_code": 302, "response_bytes": 0},
    )
    assert redirect["code"] == "HTTPS_STATUS_REJECTED"

    overflow = notify_https(
        EVENT,
        TARGET,
        owner=OWNER,
        resolver=dns,
        transport=lambda _request: {"status_code": 204, "response_bytes": 65_537},
    )
    assert overflow["code"] == "HTTPS_RESPONSE_REJECTED"


def test_https_rejects_payload_overflow_before_transport():
    sent: list[dict] = []
    event = {**EVENT, "message": "x" * 20_000}
    result = notify_https(
        event,
        TARGET,
        owner=OWNER,
        resolver=lambda _host: [{"address": "93.184.216.34", "family": 4}],
        transport=lambda request: sent.append(dict(request)),
    )
    assert result["code"] == "HTTPS_PAYLOAD_REJECTED"
    assert sent == []


def test_https_never_serializes_unallowlisted_event_fields():
    requests: list[dict] = []
    result = notify_https(
        {**EVENT, "raw_secret": "token=must-not-leak"},
        TARGET,
        owner=OWNER,
        resolver=lambda _host: [{"address": "93.184.216.34", "family": 4}],
        transport=lambda request: requests.append(dict(request))
        or {"status_code": 204, "response_bytes": 0},
    )
    assert result["code"] == "HTTPS_DELIVERED"
    assert "raw_secret" not in requests[0]["payload"]
    assert "must-not-leak" not in requests[0]["payload"]


def test_public_address_classification():
    assert public_address("8.8.8.8", 4) is True
    assert public_address("198.51.100.1", 4) is False
    assert public_address("2606:4700:4700::1111", 6) is True
    assert public_address("2001:db8::1", 6) is False


def test_https_rejects_excessive_dns_results():
    rows = [{"address": "93.184.216.34", "family": 4}] * 65
    result = notify_https(
        EVENT,
        TARGET,
        owner=OWNER,
        resolver=lambda _host: rows,
    )
    assert result["code"] == "HTTPS_DNS_REVALIDATION_REJECTED"


def test_https_bounds_dns_wall_time():
    import time

    def stalled(_host):
        time.sleep(0.25)
        return [{"address": "93.184.216.34", "family": 4}]

    started = time.monotonic()
    result = notify_https(
        EVENT,
        {**TARGET, "timeout_ms": 100},
        owner=OWNER,
        resolver=stalled,
    )
    elapsed = time.monotonic() - started
    assert result["code"] == "HTTPS_DNS_FAILED"
    assert elapsed < 0.2


def test_https_total_deadline_bounds_slow_transport_and_stream():
    import time

    def slow_stream(_request):
        time.sleep(0.25)
        return {"status_code": 204, "response_bytes": 0}

    started = time.monotonic()
    result = notify_https(
        EVENT,
        {**TARGET, "timeout_ms": 100},
        owner=OWNER,
        resolver=lambda _host: [{"address": "93.184.216.34", "family": 4}],
        transport=slow_stream,
    )
    assert result["code"] == "HTTPS_DEADLINE_EXCEEDED"
    assert time.monotonic() - started < 0.2


def test_https_total_deadline_is_shared_by_dns_and_transport():
    import time

    requested: list[int] = []

    def dns(_host):
        time.sleep(0.03)
        return [{"address": "93.184.216.34", "family": 4}]

    def transport(request):
        requested.append(request["timeout_ms"])
        time.sleep(0.08)
        return {"status_code": 204, "response_bytes": 0}

    started = time.monotonic()
    result = notify_https(
        EVENT,
        {**TARGET, "timeout_ms": 120},
        owner=OWNER,
        resolver=dns,
        transport=transport,
    )
    assert result["code"] == "HTTPS_DEADLINE_EXCEEDED"
    assert requested and requested[0] < 120
    assert time.monotonic() - started < 0.22


def test_pinned_connection_uses_resolved_address_and_hostname_sni(monkeypatch):
    calls: dict[str, object] = {}

    class RawSocket:
        def close(self):
            calls["raw_closed"] = True

    class WrappedSocket:
        def close(self):
            calls["wrapped_closed"] = True

    class Context:
        # http.client.HTTPSConnection.__init__ reads verify_mode / check_hostname
        # before connect(); a bare mock must expose them or Linux CI fails with
        # AttributeError even though wrap_socket is never reached in __init__.
        verify_mode = http_adapter.ssl.CERT_REQUIRED
        check_hostname = True

        def wrap_socket(self, raw, *, server_hostname):
            calls["raw"] = raw
            calls["server_hostname"] = server_hostname
            return WrappedSocket()

    context = Context()
    monkeypatch.setattr(http_adapter.ssl, "create_default_context", lambda: context)
    monkeypatch.setattr(
        http_adapter.socket,
        "create_connection",
        lambda address, timeout: calls.update(address=address, timeout=timeout) or RawSocket(),
    )
    connection = http_adapter._PinnedHTTPSConnection(
        "hooks.acme.example.net", "93.184.216.34", 1.25
    )
    connection.connect()
    assert calls["address"] == ("93.184.216.34", 443)
    assert calls["timeout"] == 1.25
    assert calls["server_hostname"] == "hooks.acme.example.net"
    connection.close()
