"""Outbound-only SSRF-safe HTTPS notification adapter."""
from __future__ import annotations

import http.client
import ipaddress
import re
import socket
import ssl
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any
from urllib.parse import urlsplit

from omg_cli.contracts.writer_chain import canonical_json_bytes
from omg_cli.notify.events import notification_outcome, notification_payload, owner_matches


MAX_URL_BYTES = 2_048
MAX_PAYLOAD_BYTES = 16_384
MAX_RESPONSE_BYTES = 65_536
MAX_DNS_RESULTS = 64
MAX_DNS_WORKERS = 4
_HOST = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)
_HEADER = re.compile(r"^[A-Za-z0-9-]{1,64}$")
_BLOCKED_SUFFIXES = (
    ".localhost",
    ".local",
    ".internal",
    ".home",
    ".lan",
    ".onion",
)
_BLOCKED_HOSTS = {"localhost", "metadata.google.internal"}
_HOP_HEADERS = {"connection", "content-length", "host", "transfer-encoding"}
_NAT64 = ipaddress.IPv6Network("64:ff9b::/96")
_DNS_SLOTS = threading.BoundedSemaphore(MAX_DNS_WORKERS)
_TRANSPORT_SLOTS = threading.BoundedSemaphore(MAX_DNS_WORKERS)

Resolver = Callable[[str], Sequence[Any]]
Transport = Callable[[Mapping[str, Any]], Mapping[str, Any]]


def public_address(address: str, family: int) -> bool:
    """Return true only for globally routable addresses, including embeddings."""

    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    if parsed.version != family:
        return False
    if isinstance(parsed, ipaddress.IPv4Address):
        return parsed.is_global
    mapped = parsed.ipv4_mapped
    if mapped is not None:
        return mapped.is_global
    if parsed in _NAT64:
        return ipaddress.IPv4Address(int(parsed) & 0xFFFFFFFF).is_global
    # IPv4-compatible ::a.b.c.d addresses are also subject to IPv4 policy.
    if int(parsed) >> 32 == 0 and int(parsed) > 1:
        return ipaddress.IPv4Address(int(parsed) & 0xFFFFFFFF).is_global
    if parsed.sixtofour is not None or parsed.teredo is not None:
        return False
    return parsed.is_global


def _normalize_host(value: str) -> str:
    return value.lower().rstrip(".")


def _valid_host(value: str) -> bool:
    return (
        _HOST.fullmatch(value) is not None
        and value not in _BLOCKED_HOSTS
        and not value.endswith(_BLOCKED_SUFFIXES)
    )


def validate_https_endpoint(target: Mapping[str, Any]) -> dict[str, Any] | None:
    """Validate and normalize a persisted or runtime HTTPS target."""

    url = target.get("url")
    hosts = target.get("allowed_hosts")
    timeout = target.get("timeout_ms", 3_000)
    headers = target.get("headers", {})
    if not isinstance(url, str) or not url or len(url.encode("utf-8")) > MAX_URL_BYTES:
        return None
    if not isinstance(hosts, list) or not 1 <= len(hosts) <= 32:
        return None
    normalized_hosts: list[str] = []
    for host in hosts:
        if not isinstance(host, str):
            return None
        normalized = _normalize_host(host)
        if not _valid_host(normalized) or normalized in normalized_hosts:
            return None
        normalized_hosts.append(normalized)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 100 <= timeout <= 5_000:
        return None
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return None
    hostname = _normalize_host(parsed.hostname or "")
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.query
        or parsed.fragment
        or not _valid_host(hostname)
        or hostname not in normalized_hosts
        or not parsed.path.startswith("/")
        or len(parsed.path.encode("utf-8")) > MAX_URL_BYTES
        or any(ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F for char in parsed.path)
    ):
        return None
    normalized_headers = _normalize_headers(headers)
    if normalized_headers is None:
        return None
    return {
        "hostname": hostname,
        "path": parsed.path or "/",
        "timeout_ms": timeout,
        "headers": normalized_headers,
        "allowed_hosts": normalized_hosts,
    }


def _normalize_headers(value: object) -> dict[str, str] | None:
    if not isinstance(value, Mapping) or len(value) > 16:
        return None
    result: dict[str, str] = {}
    for raw_name, raw_value in sorted(value.items(), key=lambda item: str(item[0]).lower()):
        if not isinstance(raw_name, str) or _HEADER.fullmatch(raw_name) is None:
            return None
        name = raw_name.lower()
        if name in _HOP_HEADERS or name in result:
            return None
        if (
            not isinstance(raw_value, str)
            or len(raw_value.encode("utf-8")) > 4_096
            or any(
                ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F
                for char in raw_value
            )
        ):
            return None
        result[name] = raw_value
    return result


def _default_resolver(hostname: str) -> list[dict[str, Any]]:
    rows = socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
    return [
        {"address": row[4][0], "family": 4 if row[0] == socket.AF_INET else 6}
        for row in rows
        if row[0] in {socket.AF_INET, socket.AF_INET6}
    ]


def _resolve_with_timeout(
    resolver: Resolver,
    hostname: str,
    timeout_seconds: float,
) -> Sequence[Any]:
    """Bound DNS wall time and leaked workers when platform DNS stalls."""

    if timeout_seconds <= 0 or not _DNS_SLOTS.acquire(timeout=timeout_seconds):
        raise TimeoutError("DNS resolution slot timed out")
    done = threading.Event()
    box: dict[str, Any] = {}

    def resolve() -> None:
        try:
            box["value"] = resolver(hostname)
        except Exception as exc:  # noqa: BLE001 - replayed on the caller thread
            box["error"] = exc
        finally:
            _DNS_SLOTS.release()
            done.set()

    worker = threading.Thread(target=resolve, name="omg-notify-dns", daemon=True)
    try:
        worker.start()
    except Exception:
        _DNS_SLOTS.release()
        raise
    if not done.wait(timeout_seconds):
        raise TimeoutError("DNS resolution timed out")
    error = box.get("error")
    if isinstance(error, Exception):
        raise error
    value = box.get("value")
    if not isinstance(value, Sequence):
        raise TypeError("DNS result must be a sequence")
    return value


def _transport_with_timeout(
    transport: Transport,
    request: Mapping[str, Any],
    timeout_seconds: float,
) -> Mapping[str, Any]:
    """Hard-bound arbitrary transports to the remaining total deadline."""

    if timeout_seconds <= 0 or not _TRANSPORT_SLOTS.acquire(timeout=timeout_seconds):
        raise TimeoutError("HTTPS transport slot timed out")
    done = threading.Event()
    box: dict[str, Any] = {}

    def send() -> None:
        try:
            box["value"] = transport(request)
        except Exception as exc:  # noqa: BLE001 - replayed on the caller thread
            box["error"] = exc
        finally:
            _TRANSPORT_SLOTS.release()
            done.set()

    worker = threading.Thread(target=send, name="omg-notify-https", daemon=True)
    try:
        worker.start()
    except Exception:
        _TRANSPORT_SLOTS.release()
        raise
    if not done.wait(timeout_seconds):
        raise TimeoutError("HTTPS total deadline exceeded")
    error = box.get("error")
    if isinstance(error, Exception):
        raise error
    value = box.get("value")
    if not isinstance(value, Mapping):
        raise TypeError("HTTPS transport result must be an object")
    return value


def _normalize_addresses(rows: Sequence[Any]) -> list[dict[str, Any]] | None:
    if (
        isinstance(rows, (str, bytes))
        or not isinstance(rows, Sequence)
        or not rows
        or len(rows) > MAX_DNS_RESULTS
    ):
        return None
    result: dict[tuple[int, str], dict[str, Any]] = {}
    for raw in rows:
        if isinstance(raw, Mapping):
            address = raw.get("address")
            family = raw.get("family")
        elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)) and len(raw) == 2:
            address, family = raw
        else:
            return None
        if isinstance(family, bool) or family not in {4, 6} or not isinstance(address, str):
            return None
        try:
            normalized = ipaddress.ip_address(address).compressed.lower()
        except ValueError:
            return None
        if not public_address(normalized, family):
            return None
        result[(family, normalized)] = {"address": normalized, "family": family}
    if not result:
        return None
    return [result[key] for key in sorted(result)]


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, hostname: str, address: str, timeout: float) -> None:
        self._omg_context = ssl.create_default_context()
        super().__init__(hostname, 443, timeout=timeout, context=self._omg_context)
        self._pinned_address = address

    def connect(self) -> None:  # pragma: no cover - exercised only by safe live opt-in
        if getattr(self, "_tunnel_host", None):
            raise OSError("HTTPS proxy tunnelling is forbidden")
        raw = socket.create_connection((self._pinned_address, 443), self.timeout)
        try:
            self.sock = self._omg_context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


def _default_transport(request: Mapping[str, Any]) -> dict[str, Any]:
    address = request["address"]
    connection = _PinnedHTTPSConnection(
        str(request["hostname"]),
        str(address["address"]),
        float(request["timeout_ms"]) / 1_000,
    )
    try:
        payload = str(request["payload"]).encode("utf-8")
        headers = {
            **dict(request["headers"]),
            "content-type": "application/json",
            "content-length": str(len(payload)),
            "user-agent": "oh-my-grok-notify/1",
        }
        connection.request("POST", str(request["path"]), body=payload, headers=headers)
        deadline = request.get("deadline_monotonic")
        if not isinstance(deadline, (int, float)) or isinstance(deadline, bool):
            raise OSError("HTTPS deadline is missing")
        remaining = float(deadline) - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("HTTPS total deadline exceeded")
        if connection.sock is not None:
            connection.sock.settimeout(remaining)
        response = connection.getresponse()
        response_bytes = 0
        while True:
            remaining = float(deadline) - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("HTTPS total deadline exceeded")
            if connection.sock is not None:
                connection.sock.settimeout(remaining)
            chunk = response.read(min(8_192, MAX_RESPONSE_BYTES + 1 - response_bytes))
            response_bytes += len(chunk)
            if response_bytes > MAX_RESPONSE_BYTES:
                raise OSError("HTTPS response exceeded byte bound")
            if not chunk:
                break
        return {"status_code": response.status, "response_bytes": response_bytes}
    finally:
        connection.close()


def notify_https(
    event: dict[str, Any],
    target: Mapping[str, Any],
    *,
    owner: dict[str, Any] | None = None,
    resolver: Resolver | None = None,
    transport: Transport | None = None,
) -> dict[str, Any]:
    """Deliver one event over pinned HTTPS; never follows redirects."""

    destination = str(target.get("url") or "")
    if target.get("enabled") is not True:
        return notification_outcome("https", "skipped", "HTTPS_DISABLED", event, destination)
    if not owner_matches(event, owner):
        return notification_outcome("https", "failed", "HTTPS_OWNER_MISMATCH", event, destination)
    endpoint = validate_https_endpoint(target)
    if endpoint is None:
        return notification_outcome("https", "failed", "HTTPS_ENDPOINT_REJECTED", event, destination)
    safe_event = notification_payload(event)
    if safe_event is None:
        return notification_outcome(
            "https", "failed", "HTTPS_PAYLOAD_REJECTED", event, destination
        )
    payload = canonical_json_bytes(
        {
            "store_kind": "omg_notification_delivery",
            "schema_version": 1,
            "repository_id": "OMG",
            "event": safe_event,
        }
    )
    if len(payload) > MAX_PAYLOAD_BYTES:
        return notification_outcome("https", "failed", "HTTPS_PAYLOAD_REJECTED", event, destination)
    resolve = resolver or _default_resolver
    try:
        deadline = time.monotonic() + (float(endpoint["timeout_ms"]) / 1_000)
        first = _normalize_addresses(
            _resolve_with_timeout(resolve, endpoint["hostname"], deadline - time.monotonic())
        )
        second = _normalize_addresses(
            _resolve_with_timeout(resolve, endpoint["hostname"], deadline - time.monotonic())
        )
    except Exception as exc:  # noqa: BLE001 - optional adapter failure is contained
        return notification_outcome(
            "https", "failed", "HTTPS_DNS_FAILED", event, destination, type(exc).__name__
        )
    if first is None or second is None or first != second:
        return notification_outcome(
            "https", "failed", "HTTPS_DNS_REVALIDATION_REJECTED", event, destination
        )
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return notification_outcome(
            "https", "failed", "HTTPS_DEADLINE_EXCEEDED", event, destination
        )
    request = {
        "hostname": endpoint["hostname"],
        "path": endpoint["path"],
        "address": first[0],
        "timeout_ms": max(1, int(remaining * 1_000)),
        "headers": endpoint["headers"],
        "payload": payload.decode("utf-8"),
        "maximum_response_bytes": MAX_RESPONSE_BYTES,
        "tls_server_name": endpoint["hostname"],
        "redirects_allowed": False,
        "deadline_monotonic": deadline,
    }
    send = transport or _default_transport
    try:
        response = _transport_with_timeout(send, request, deadline - time.monotonic())
    except TimeoutError:
        return notification_outcome(
            "https", "failed", "HTTPS_DEADLINE_EXCEEDED", event, destination
        )
    except Exception as exc:  # noqa: BLE001 - optional adapter failure is contained
        return notification_outcome(
            "https", "failed", "HTTPS_DELIVERY_FAILED", event, destination, type(exc).__name__
        )
    if time.monotonic() >= deadline:
        return notification_outcome(
            "https", "failed", "HTTPS_DEADLINE_EXCEEDED", event, destination
        )
    if not isinstance(response, Mapping):
        return notification_outcome("https", "failed", "HTTPS_DELIVERY_FAILED", event, destination)
    status = response.get("status_code")
    response_bytes = response.get("response_bytes")
    if (
        isinstance(status, bool)
        or not isinstance(status, int)
        or isinstance(response_bytes, bool)
        or not isinstance(response_bytes, int)
        or response_bytes < 0
        or response_bytes > MAX_RESPONSE_BYTES
    ):
        return notification_outcome("https", "failed", "HTTPS_RESPONSE_REJECTED", event, destination)
    if 200 <= status < 300:
        return notification_outcome("https", "delivered", "HTTPS_DELIVERED", event, destination)
    # Includes every redirect; no Location is read or followed.
    return notification_outcome("https", "failed", "HTTPS_STATUS_REJECTED", event, destination)


__all__ = [
    "MAX_DNS_RESULTS",
    "MAX_PAYLOAD_BYTES",
    "MAX_RESPONSE_BYTES",
    "notify_https",
    "public_address",
    "validate_https_endpoint",
]
