"""Import-time network/subprocess probe for stock-host isolation.

Import this module only after ``create_isolation`` / ``_isolate_stock_host``.
Collection-time import is unguarded and forbidden. This module must not
import ``omg_cli``.
"""
from __future__ import annotations

import socket
import subprocess

from tests.stock_host_medley_absent_support import _NETWORK_DENIED, _SUBPROCESS_DENIED

NETWORK_DENIED = False
NETWORK_ERROR: str | None = None
SUBPROCESS_DENIED = False
SUBPROCESS_ERROR: str | None = None


def _is_denied(exc: BaseException, needle: str) -> bool:
    return isinstance(exc, OSError) and needle in str(exc)


_network_ok = True
_network_errors: list[str] = []
try:
    socket.socket()
    _network_ok = False
    _network_errors.append("socket.socket() was not denied")
except OSError as exc:
    if not _is_denied(exc, _NETWORK_DENIED):
        _network_ok = False
    _network_errors.append(str(exc))

NETWORK_DENIED = _network_ok
NETWORK_ERROR = "; ".join(_network_errors) if _network_errors else None

try:
    proc = subprocess.Popen(
        ["curl", "https://example.com"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
except PermissionError as exc:
    SUBPROCESS_DENIED = _SUBPROCESS_DENIED in str(exc)
    SUBPROCESS_ERROR = str(exc)
except OSError as exc:
    SUBPROCESS_DENIED = False
    SUBPROCESS_ERROR = str(exc)
else:
    SUBPROCESS_DENIED = False
    SUBPROCESS_ERROR = None
    try:
        proc.kill()
        proc.wait(timeout=1)
    except Exception:
        pass
