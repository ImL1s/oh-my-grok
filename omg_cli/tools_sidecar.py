"""OMG-owned tools sidecar (#73 first cut).

Semantic LSP / AST-grep / CodeGraph / research live here — never on
``omg lsp`` (host-owned ``.lsp.json`` probe) and never on ``omg mcp-server``
(``lsp.*`` names stay forbidden).

Honesty:
- Not Grok-native LSP.
- Not a live Antigravity MCP install.
- AST-grep missing → blocked, not fake success.
- Shared CodeGraph indexes are not worktree-accurate.
- CodeGraph ``occurrences`` are SCIP-inspired JSON, not SCIP protobuf.
- Network research is opt-in (``OMG_TOOLS_NETWORK=1``).
- Never writes ``passes`` / ``verified``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import select
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, NoReturn, Protocol, Sequence
from urllib.parse import unquote, urlparse

SCHEMA = "omg-tools-sidecar/v1"
MAX_RESULT_BYTES = 65_536
MAX_AST_MATCHES = 200
MAX_MEDIA_BYTES = 10 * 1024 * 1024
ALLOWED_MEDIA_MIMES = frozenset(
    {"image/png", "image/jpeg", "image/webp", "image/gif"}
)
CODEGRAPH_MODES = ("off", "auto", "shared", "local")
CODEGRAPH_INDEX_SCHEMA = "omg-tools-codegraph/v1"
CODEGRAPH_INDEXER = "import_symbol_scan"
MAX_INDEX_FILES = 400
MAX_INDEX_FILE_BYTES = 200_000
MAX_SYMBOLS_PER_FILE = 80
MAX_OCCURRENCES_PER_FILE = 120
MAX_OCCURRENCES = 2_000
MAX_CODEGRAPH_HITS = 80
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_FAKE_LSP_SYMBOL = "Fake"
_FAKE_LSP_RANGE = {
    "start": {"line": 0, "character": 0},
    "end": {"line": 0, "character": 4},
}
INDEX_SKIP_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".omg",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        "dist",
        "build",
        "target",
        ".tox",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "site-packages",
    }
)
INDEX_SUFFIXES = frozenset(
    {
        ".py",
        ".pyi",
        ".ts",
        ".tsx",
        ".js",
        ".jsx",
        ".mjs",
        ".cjs",
        ".go",
        ".rs",
        ".java",
    }
)
READ_WRITE = "read-write"
READ_ONLY = "read-only"
MUTATING_OPS = frozenset(
    {
        "lsp.rename",
        "lsp.code_action",
        "ast.replace",
    }
)
LSP_OPERATIONS = (
    "hover",
    "definition",
    "references",
    "document_symbols",
    "workspace_symbols",
    "diagnostics",
    "prepare_rename",
    "rename",
    "code_action",
    "code_action_resolve",
    "servers",
)
# Document semantic ops must not run against a silently truncated prefix.
LSP_SEMANTIC_DOC_OPS = frozenset(
    {
        "hover",
        "definition",
        "references",
        "document_symbols",
        "diagnostics",
        "prepare_rename",
        "rename",
        "code_action",
    }
)
COMMON_LSP_SERVERS = (
    "pylsp",
    "pyright-langserver",
    "typescript-language-server",
    "gopls",
    "rust-analyzer",
    "clangd",
)
# rust-analyzer on a tiny crate needs well more than 5s for initialize + first
# hover/definition. Bounded, not infinite.
DEFAULT_LSP_TIMEOUT_S = 30.0
LSP_SEMANTIC_RETRY_S = 0.25
# LSP ContentModified: client may retry (rust-analyzer while indexing/didOpen).
LSP_CONTENT_MODIFIED = -32801
_CONTENT_MODIFIED_MSG = "content modified"
_CONTENT_MODIFIED_CODES = frozenset({LSP_CONTENT_MODIFIED, "-32801"})
_MAX_LSP_STDERR_BYTES = 4096
_LSP_CONTINUE = object()

SIDECAR_TOOL_NAMES: tuple[str, ...] = (
    "omg.tools.doctor",
    "omg.tools.lsp.servers",
    "omg.tools.lsp.hover",
    "omg.tools.lsp.definition",
    "omg.tools.lsp.references",
    "omg.tools.lsp.document_symbols",
    "omg.tools.lsp.workspace_symbols",
    "omg.tools.lsp.diagnostics",
    "omg.tools.lsp.prepare_rename",
    "omg.tools.lsp.rename",
    "omg.tools.lsp.code_action",
    "omg.tools.lsp.code_action_resolve",
    "omg.tools.ast.search",
    "omg.tools.ast.replace",
    "omg.tools.codegraph.status",
    "omg.tools.codegraph.query",
    "omg.tools.codegraph.index",
    "omg.tools.research.status",
    "omg.tools.research.search",
)


class ToolsError(ValueError):
    """Structured sidecar failure (never a silent success)."""

    def __init__(self, code: str, message: str, *, details: Any | None = None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details is not None:
            payload["details"] = self.details
        return payload


class LspTransport(Protocol):
    def request(self, method: str, params: Mapping[str, Any] | None = None) -> Any:
        """Send one JSON-RPC request and return the result."""

    def close(self) -> None:
        """Release the language server process / socket."""


def _env(env: Mapping[str, str] | None) -> Mapping[str, str]:
    return env if env is not None else os.environ


def capability_mode_of(value: str | None) -> str:
    mode = (value or READ_ONLY).strip().lower()
    if mode in {"execute", "all"}:
        raise ToolsError(
            "E_CAPABILITY_MODE",
            "tools sidecar never uses execute/all",
        )
    if mode not in {READ_ONLY, READ_WRITE}:
        raise ToolsError("E_CAPABILITY_MODE", f"unsupported capability_mode {mode!r}")
    return mode


def require_read_write(mode: str, operation: str) -> None:
    if capability_mode_of(mode) != READ_WRITE:
        raise ToolsError(
            "E_READ_ONLY",
            f"{operation} requires capability_mode=read-write (preview-only otherwise)",
        )


_MODE_RANK = {READ_ONLY: 0, READ_WRITE: 1}


def effective_capability_mode(server: str, requested: Any) -> str:
    """Server launch mode is the ceiling; clients cannot escalate."""
    floor = capability_mode_of(server)
    if requested is None or str(requested).strip() == "":
        return floor
    asked = capability_mode_of(str(requested))
    if _MODE_RANK[asked] > _MODE_RANK[floor]:
        raise ToolsError(
            "E_CAPABILITY_MODE",
            f"cannot escalate capability_mode from {floor} to {asked}",
        )
    return asked


def lsp_position(line: Any, character: Any) -> dict[str, int]:
    if line is None:
        line = 0
    if character is None:
        character = 0
    if isinstance(line, bool) or not isinstance(line, int) or line < 0:
        raise ToolsError("E_LSP_POSITION", "line must be a non-negative int")
    if isinstance(character, bool) or not isinstance(character, int) or character < 0:
        raise ToolsError("E_LSP_POSITION", "character must be a non-negative int")
    return {"line": line, "character": character}


def confine_path(root: Path, candidate: str | Path) -> Path:
    """Windows-safe workspace confinement (resolve + relative_to).

    Does **not** use POSIX ``path_keys`` (fail-closed on Windows). Rejects
    NUL, URI schemes other than file, and paths that escape *root*.
    """
    if candidate is None:
        raise ToolsError("E_PATH", "path is required")
    text = str(candidate)
    if "\x00" in text:
        raise ToolsError("E_PATH", "path contains NUL")
    if text.startswith("file:"):
        parsed = urlparse(text)
        text = unquote(parsed.path)
        if os.name == "nt" and text.startswith("/") and len(text) > 2 and text[2] == ":":
            text = text[1:]
    base = Path(root).resolve()
    path = Path(text)
    if not path.is_absolute():
        path = base / path
    try:
        resolved = path.resolve()
        resolved.relative_to(base)
    except (OSError, ValueError) as exc:
        raise ToolsError("E_PATH_ESCAPE", f"path escapes workspace: {candidate}") from exc
    return resolved


def media_descriptor(
    *,
    mime: str,
    byte_length: int,
    sha256: str | None = None,
    relpath: str | None = None,
    inline_bytes: Any = None,
    base64: Any = None,
) -> dict[str, Any]:
    """Bounded image/file descriptor. Never inlines raw bytes or base64."""
    if inline_bytes not in (None, False) or base64 not in (None, False, ""):
        raise ToolsError(
            "E_MEDIA_INLINE",
            "MCP image output must be a descriptor, not raw bytes/base64",
        )
    if mime not in ALLOWED_MEDIA_MIMES:
        raise ToolsError("E_MEDIA_MIME", f"unsupported or missing mime {mime!r}")
    if not isinstance(byte_length, int) or isinstance(byte_length, bool):
        raise ToolsError("E_MEDIA_SIZE", "byte_length must be int")
    if byte_length < 0 or byte_length > MAX_MEDIA_BYTES:
        raise ToolsError("E_MEDIA_SIZE", "byte_length out of bounds")
    if relpath is not None and (relpath.startswith("/") or ".." in Path(relpath).parts):
        raise ToolsError("E_MEDIA_PATH", "media path must be a relative workspace path")
    return {
        "kind": "image_descriptor",
        "mime": mime,
        "byte_length": byte_length,
        "sha256": sha256,
        "path": relpath,
        "inline_bytes": False,
        "base64": None,
        "note": "descriptor only; never store raw image bytes in run state",
    }


def _bounded(value: Any) -> Any:
    encoded = json.dumps(value, ensure_ascii=False).encode("utf-8")
    if len(encoded) > MAX_RESULT_BYTES:
        raise ToolsError("E_RESULT_SIZE", "tool result exceeds bounded size")
    if isinstance(value, dict) and (
        value.get("verified") is True or value.get("passes") is True
    ):
        raise ToolsError("E_VERIFIED", "tools sidecar cannot set verified/passes")
    return value


def _workspace_configuration_result(
    params: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    """Empty settings for each LSP ``workspace/configuration`` item.

    Real language servers often request this during initialize/hover. An empty
    object per item is enough for them to proceed; ``[]`` when items is absent.
    """
    if not isinstance(params, Mapping):
        return []
    items = params.get("items")
    if not isinstance(items, list):
        return []
    return [{} for _ in items]


def lsp_command_argv(
    command: Sequence[str] | None,
    extra: Sequence[str] | None = None,
) -> list[str]:
    """Build language-server argv. Never auto-appends ``--stdio``.

    rust-analyzer speaks stdio by default and rejects ``--stdio``
    (``unexpected flag: --stdio``). Servers that need the flag must pass it
    after ``--``. Explicit extras are preserved so a bad flag fails honestly.
    """
    argv = [str(part) for part in (command or []) if str(part)]
    if not argv:
        raise ToolsError("E_LSP_COMMAND", "lsp command is empty")
    argv.extend(str(part) for part in (extra or []))
    name = Path(argv[0]).name.lower()
    if name in {"rust-analyzer", "rust-analyzer.exe"}:
        argv = [part for part in argv if part != "--stdio"]
    return argv


def normalize_lsp_argv(argv: list[str]) -> list[str]:
    """Drop ``--stdio`` for rust-analyzer; leave other servers unchanged."""
    if not argv:
        return []
    try:
        return lsp_command_argv(argv, None)
    except ToolsError:
        return list(argv)


def _hover_contents_inspectable(contents: Any) -> bool:
    if contents is None or contents == "" or contents == [] or contents == {}:
        return False
    if isinstance(contents, str):
        return bool(contents.strip())
    if isinstance(contents, dict):
        value = contents.get("value")
        if isinstance(value, str):
            return bool(value.strip())
        return bool(contents)
    if isinstance(contents, list):
        return any(_hover_contents_inspectable(item) for item in contents)
    return True


def _location_inspectable(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    if item.get("uri"):
        return True
    return bool(item.get("targetUri"))


def semantic_result_inspectable(operation: str, result: Any) -> bool:
    """True when hover/definition is a non-null inspectable LSP payload."""
    if result is None:
        return False
    if operation == "hover":
        if isinstance(result, str):
            return bool(result.strip())
        if isinstance(result, dict):
            if "contents" in result:
                return _hover_contents_inspectable(result.get("contents"))
            if "value" in result:
                return bool(str(result.get("value") or "").strip())
            return bool(result)
        if isinstance(result, list):
            return any(semantic_result_inspectable("hover", item) for item in result)
        return True
    if operation == "definition":
        if isinstance(result, list):
            return bool(result)
        if isinstance(result, dict):
            return bool(result)
        return _location_inspectable(result)
    return True


def _lsp_rpc_is_content_modified(exc: ToolsError) -> bool:
    """True for JSON-RPC ContentModified (-32801 / ``content modified``)."""
    if exc.code != "E_LSP_RPC":
        return False
    payload = exc.details if isinstance(exc.details, Mapping) else None
    if payload is not None:
        if payload.get("code") in _CONTENT_MODIFIED_CODES:
            return True
        message = payload.get("message")
        if isinstance(message, str) and _CONTENT_MODIFIED_MSG in message.lower():
            return True
    blob = str(exc.message or "").lower()
    return _CONTENT_MODIFIED_MSG in blob


def _lsp_timeout_s(transport: LspTransport) -> float:
    timeout = getattr(transport, "timeout_s", None)
    if isinstance(timeout, (int, float)) and timeout > 0:
        return float(timeout)
    return DEFAULT_LSP_TIMEOUT_S


def _arm_lsp_deadline(transport: LspTransport) -> None:
    if getattr(transport, "_omg_lsp_deadline", None) is not None:
        return
    try:
        setattr(
            transport,
            "_omg_lsp_deadline",
            time.monotonic() + _lsp_timeout_s(transport),
        )
    except (AttributeError, TypeError):
        pass


def _lsp_deadline(transport: LspTransport) -> float:
    _arm_lsp_deadline(transport)
    stored = getattr(transport, "_omg_lsp_deadline", None)
    if isinstance(stored, (int, float)):
        return float(stored)
    return time.monotonic() + _lsp_timeout_s(transport)


def _pump_or_sleep(transport: LspTransport, deadline: float) -> None:
    pump = getattr(transport, "pump", None)
    if callable(pump):
        pump(deadline)
        return
    remaining = deadline - time.monotonic()
    if remaining > 0:
        time.sleep(remaining)


def _await_inspectable_semantic(
    transport: LspTransport,
    operation: str,
    method: str,
    params: Mapping[str, Any],
    first: Any,
) -> Any:
    """Retry hover/definition until inspectable or the sidecar deadline.

    JSON ``null`` and LSP ContentModified (``-32801`` / ``content modified``)
    are the same retry family while the language server indexes. Other RPC
    errors still fail closed.
    """
    if semantic_result_inspectable(operation, first):
        return first
    deadline = time.monotonic() + _lsp_timeout_s(transport)
    last = first
    while time.monotonic() < deadline:
        _pump_or_sleep(
            transport, min(deadline, time.monotonic() + LSP_SEMANTIC_RETRY_S)
        )
        if time.monotonic() >= deadline:
            break
        try:
            last = transport.request(method, params)
        except ToolsError as exc:
            if _lsp_rpc_is_content_modified(exc):
                continue
            raise
        if semantic_result_inspectable(operation, last):
            return last
    raise ToolsError(
        "E_LSP_TIMEOUT",
        f"{operation} had no inspectable result before the "
        f"{_lsp_timeout_s(transport):.0f}s sidecar deadline",
        details={"operation": operation, "result": last},
    )


@dataclass
class FakeLspTransport:
    """In-process JSON-RPC stand-in for protocol tests (not a real language server)."""

    document_version: int = 1
    closed: bool = False
    calls: list[tuple[str, Mapping[str, Any]]] = field(default_factory=list)
    crash_on: str | None = None
    hang_on: str | None = None

    def request(self, method: str, params: Mapping[str, Any] | None = None) -> Any:
        body = dict(params or {})
        self.calls.append((method, body))
        if self.closed:
            raise ToolsError("E_LSP_CLOSED", "transport already closed")
        if method == self.crash_on:
            raise ToolsError("E_LSP_CRASH", f"fake server crashed on {method}")
        if method == self.hang_on:
            raise ToolsError("E_LSP_TIMEOUT", f"fake server timed out on {method}")
        if method == "workspace/configuration":
            return _workspace_configuration_result(body)
        if method == "workspace/workspaceFolders":
            folders = getattr(self, "_omg_workspace_folders", None)
            return list(folders) if isinstance(folders, list) else []
        if method == "initialize":
            return {
                "capabilities": {
                    "hoverProvider": True,
                    "definitionProvider": True,
                    "referencesProvider": True,
                    "documentSymbolProvider": True,
                    "workspaceSymbolProvider": True,
                    "diagnosticProvider": True,
                    "renameProvider": {"prepareProvider": True},
                    "codeActionProvider": True,
                }
            }
        if method == "textDocument/hover":
            return {"contents": {"kind": "markdown", "value": "fake hover"}}
        doc_uri = (body.get("textDocument") or {}).get("uri") or "file:///tmp/Fake.py"
        if method == "textDocument/definition":
            return [
                {
                    "uri": doc_uri,
                    "range": dict(_FAKE_LSP_RANGE),
                }
            ]
        if method == "textDocument/references":
            return [
                {
                    "uri": doc_uri,
                    "range": dict(_FAKE_LSP_RANGE),
                },
                {
                    "uri": "file:///tmp/Fake.py",
                    "range": {
                        "start": {"line": 2, "character": 0},
                        "end": {"line": 2, "character": 4},
                    },
                },
            ]
        if method == "textDocument/documentSymbol":
            return [
                {
                    "name": _FAKE_LSP_SYMBOL,
                    "kind": 5,
                    "range": {
                        "start": {"line": 0, "character": 0},
                        "end": {"line": 1, "character": 0},
                    },
                    "selectionRange": dict(_FAKE_LSP_RANGE),
                }
            ]
        if method == "workspace/symbol":
            return [
                {
                    "name": _FAKE_LSP_SYMBOL,
                    "kind": 5,
                    "location": {
                        "uri": "file:///tmp/Fake.py",
                        "range": dict(_FAKE_LSP_RANGE),
                    },
                }
            ]
        if method == "textDocument/diagnostic":
            return {"kind": "full", "items": [], "version": self.document_version}
        if method == "textDocument/prepareRename":
            return {
                "range": {
                    "start": {"line": 0, "character": 0},
                    "end": {"line": 0, "character": 4},
                },
                "placeholder": "Fake",
            }
        if method == "textDocument/rename":
            return {"changes": {}}
        if method == "textDocument/codeAction":
            return []
        if method == "codeAction/resolve":
            return body
        if method in {
            "shutdown",
            "initialized",
            "textDocument/didOpen",
            "textDocument/didChange",
        }:
            return None
        raise ToolsError("E_LSP_METHOD", f"unsupported LSP method {method}")

    def close(self) -> None:
        self.closed = True


class StdioLspTransport:
    """Content-Length JSON-RPC client. Caller must close() to reap the child."""

    def __init__(
        self,
        argv: list[str],
        *,
        cwd: Path,
        timeout_s: float = DEFAULT_LSP_TIMEOUT_S,
    ) -> None:
        if not argv:
            raise ToolsError("E_LSP_COMMAND", "lsp command is empty")
        self.timeout_s = timeout_s
        self._next_id = 1
        self._pending = bytearray()
        self._stderr_buf = bytearray()
        self._stderr_lock = threading.Lock()
        try:
            self._proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(cwd),
                bufsize=0,
            )
        except OSError as exc:
            raise ToolsError(
                "E_LSP_COMMAND",
                f"cannot launch lsp command {argv[0]!r}: {exc}",
            ) from exc
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr, name="omg-lsp-stderr", daemon=True
        )
        self._stderr_thread.start()

    def _drain_stderr(self) -> None:
        stderr = self._proc.stderr
        if stderr is None:
            return
        fd = stderr.fileno()
        while True:
            try:
                chunk = os.read(fd, 512)
            except OSError:
                return
            if not chunk:
                return
            with self._stderr_lock:
                room = _MAX_LSP_STDERR_BYTES - len(self._stderr_buf)
                if room > 0:
                    self._stderr_buf.extend(chunk[:room])

    def _stderr_text(self) -> str:
        thread = getattr(self, "_stderr_thread", None)
        if thread is not None and thread.is_alive() and self._proc.poll() is not None:
            thread.join(timeout=0.4)
        with self._stderr_lock:
            return bytes(self._stderr_buf).decode("utf-8", "replace").strip()

    def _raise_crash(self, reason: str) -> NoReturn:
        stderr = self._stderr_text()
        details: dict[str, Any] = {"returncode": self._proc.returncode}
        if stderr:
            details["stderr"] = stderr
            snippet = stderr if len(stderr) <= 500 else stderr[:500]
            raise ToolsError("E_LSP_CRASH", f"{reason}: {snippet}", details=details)
        raise ToolsError("E_LSP_CRASH", reason, details=details)

    def _ensure_alive(self) -> None:
        if self._proc.poll() is not None:
            self._raise_crash("language server exited")

    def _timeout_error(self) -> ToolsError:
        return ToolsError("E_LSP_TIMEOUT", f"read exceeded {self.timeout_s}s")

    def _write_message(self, payload: Mapping[str, Any]) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        header = f"Content-Length: {len(raw)}\r\n\r\n".encode("ascii")
        assert self._proc.stdin is not None
        try:
            self._proc.stdin.write(header + raw)
            self._proc.stdin.flush()
        except OSError as exc:
            if self._proc.poll() is not None:
                self._raise_crash("language server exited")
            raise ToolsError("E_LSP_CRASH", "language server stdin closed") from exc

    def _read_more(self, deadline: float) -> None:
        stdout = self._proc.stdout
        assert stdout is not None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if self._proc.poll() is not None:
                self._raise_crash("language server exited")
            raise self._timeout_error()
        fd = stdout.fileno()
        if os.name != "nt":
            ready, _, _ = select.select([fd], [], [], remaining)
            if not ready:
                if self._proc.poll() is not None:
                    self._raise_crash("language server exited")
                raise self._timeout_error()
            try:
                chunk = os.read(fd, 4096)
            except OSError as exc:
                if self._proc.poll() is not None:
                    self._raise_crash("language server exited")
                raise ToolsError("E_LSP_CRASH", "language server stdout closed") from exc
            if not chunk:
                self._raise_crash("language server stdout closed")
            self._pending.extend(chunk)
            return
        holder: list[bytes] = []
        error: list[BaseException] = []

        def _read() -> None:
            try:
                holder.append(os.read(fd, 4096))
            except BaseException as exc:  # noqa: BLE001 — surface into timeout loop
                error.append(exc)

        worker = threading.Thread(target=_read, daemon=True)
        worker.start()
        worker.join(remaining)
        if worker.is_alive():
            if self._proc.poll() is not None:
                self._raise_crash("language server exited")
            raise self._timeout_error()
        if error:
            if self._proc.poll() is not None:
                self._raise_crash("language server exited")
            raise ToolsError("E_LSP_CRASH", "language server stdout closed") from error[0]
        chunk = holder[0] if holder else b""
        if not chunk:
            self._raise_crash("language server stdout closed")
        self._pending.extend(chunk)

    def _read_message(self, deadline: float) -> dict[str, Any]:
        while b"\r\n\r\n" not in self._pending:
            self._read_more(deadline)
        head, rest = bytes(self._pending).split(b"\r\n\r\n", 1)
        length = None
        for line in head.decode("ascii", "replace").split("\r\n"):
            if line.lower().startswith("content-length:"):
                length = int(line.split(":", 1)[1].strip())
        if length is None:
            raise ToolsError("E_LSP_PROTOCOL", "missing Content-Length")
        self._pending = bytearray(rest)
        while len(self._pending) < length:
            self._read_more(deadline)
        body = bytes(self._pending[:length])
        del self._pending[:length]
        return json.loads(body.decode("utf-8"))

    def notify(self, method: str, params: Mapping[str, Any] | None = None) -> None:
        self._ensure_alive()
        self._write_message(
            {"jsonrpc": "2.0", "method": method, "params": dict(params or {})}
        )

    def _reply_server_request(self, msg: Mapping[str, Any]) -> None:
        """Answer server→client requests so the language server does not stall.

        ``workspace/configuration`` and ``workspace/workspaceFolders`` keep
        their special-case payloads. Every other JSON-RPC *request* (id present)
        gets ``result: null`` — rust-analyzer's ``client/registerCapability``
        and ``window/workDoneProgress/create`` included. Notifications (no id)
        are not answered.
        """
        req_id = msg.get("id")
        if req_id is None:
            return
        method = msg.get("method")
        if method == "workspace/configuration":
            params = msg.get("params")
            result: Any = _workspace_configuration_result(
                params if isinstance(params, Mapping) else None
            )
        elif method == "workspace/workspaceFolders":
            folders = getattr(self, "_omg_workspace_folders", None)
            result = list(folders) if isinstance(folders, list) else []
        else:
            result = None
        self._write_message({"jsonrpc": "2.0", "id": req_id, "result": result})

    def _handle_incoming(self, msg: dict[str, Any], expected_id: Any | None) -> Any:
        incoming_method = msg.get("method")
        if isinstance(incoming_method, str) and incoming_method:
            if "id" in msg and msg.get("id") is not None:
                self._reply_server_request(msg)
            return _LSP_CONTINUE
        if expected_id is None or msg.get("id") != expected_id:
            return _LSP_CONTINUE
        if msg.get("error") is not None:
            error = msg["error"]
            raise ToolsError("E_LSP_RPC", str(error), details=error)
        return msg.get("result")

    def pump(self, deadline: float) -> None:
        """Read and answer server traffic until *deadline* (no client request)."""
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                self._raise_crash("language server exited")
            try:
                msg = self._read_message(deadline)
            except ToolsError as exc:
                if exc.code == "E_LSP_TIMEOUT":
                    return
                raise
            if isinstance(msg, dict):
                self._handle_incoming(msg, None)

    def _request_deadline(self) -> float:
        # Per-request budget. Do not inherit a session-wide deadline — a
        # long-lived omg tools serve transport would otherwise fail every
        # later hover after DEFAULT_LSP_TIMEOUT_S from initialize.
        return time.monotonic() + float(self.timeout_s)

    def request(self, method: str, params: Mapping[str, Any] | None = None) -> Any:
        self._ensure_alive()
        msg_id = self._next_id
        self._next_id += 1
        self._write_message(
            {
                "jsonrpc": "2.0",
                "id": msg_id,
                "method": method,
                "params": dict(params or {}),
            }
        )
        deadline = self._request_deadline()
        while True:
            msg = self._read_message(deadline)
            if not isinstance(msg, dict):
                continue
            handled = self._handle_incoming(msg, msg_id)
            if handled is _LSP_CONTINUE:
                continue
            return handled

    def close(self) -> None:
        try:
            stdin = self._proc.stdin
            if stdin is not None and not stdin.closed:
                stdin.close()
        except OSError:
            pass
        if self._proc.poll() is None:
            self._proc.kill()
        try:
            self._proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self._proc.kill()
        thread = getattr(self, "_stderr_thread", None)
        if thread is not None:
            thread.join(timeout=0.5)


def inventory_lsp_servers() -> list[dict[str, Any]]:
    rows = []
    for name in COMMON_LSP_SERVERS:
        path = shutil.which(name)
        rows.append(
            {
                "name": name,
                "available": path is not None,
                "path": path,
                "ready": False,
                "note": "detected executable only; not started, not live-verified",
            }
        )
    return rows


_LSP_METHODS = {
    "hover": "textDocument/hover",
    "definition": "textDocument/definition",
    "references": "textDocument/references",
    "document_symbols": "textDocument/documentSymbol",
    "workspace_symbols": "workspace/symbol",
    "diagnostics": "textDocument/diagnostic",
    "prepare_rename": "textDocument/prepareRename",
    "rename": "textDocument/rename",
    "code_action": "textDocument/codeAction",
    "code_action_resolve": "codeAction/resolve",
}


def lsp_range(
    line: Any,
    character: Any,
    end_line: Any = None,
    end_character: Any = None,
    range_arg: Any = None,
) -> dict[str, dict[str, int]]:
    """Build the LSP Range required by textDocument/codeAction."""
    if isinstance(range_arg, Mapping):
        start_raw = range_arg.get("start") or {}
        end_raw = range_arg.get("end") or {}
        if isinstance(start_raw, Mapping) and isinstance(end_raw, Mapping):
            return {
                "start": lsp_position(start_raw.get("line"), start_raw.get("character")),
                "end": lsp_position(end_raw.get("line"), end_raw.get("character")),
            }
        raise ToolsError("E_LSP_RANGE", "range must have start and end positions")
    start = lsp_position(line, character)
    if end_line is None:
        end_line = start["line"]
    if end_character is None:
        end_character = start["character"]
    return {"start": start, "end": lsp_position(end_line, end_character)}


def _language_id_for(path: Path) -> str:
    return {
        ".py": "python",
        ".ts": "typescript",
        ".js": "javascript",
        ".go": "go",
        ".rs": "rust",
        ".c": "c",
        ".h": "c",
        ".cpp": "cpp",
        ".json": "json",
        ".md": "markdown",
    }.get(path.suffix.lower(), "plaintext")


def _lsp_notify_or_request(
    transport: LspTransport, method: str, params: Mapping[str, Any] | None = None
) -> None:
    notify = getattr(transport, "notify", None)
    if callable(notify):
        notify(method, params)
        return
    try:
        transport.request(method, params)
    except ToolsError as exc:
        if exc.code != "E_LSP_METHOD":
            raise


def _read_text_bounded(path: Path) -> tuple[str, bool]:
    """Read up to MAX_RESULT_BYTES. Second value is True when the file is larger."""
    if not path.is_file():
        return "", False
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_RESULT_BYTES + 1)
    except OSError:
        return "", False
    truncated = len(raw) > MAX_RESULT_BYTES
    return raw[:MAX_RESULT_BYTES].decode("utf-8", "replace"), truncated


def _file_fingerprint(path: Path) -> str:
    if not path.is_file():
        return ""
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_RESULT_BYTES)
    except OSError:
        return ""
    return hashlib.sha256(raw).hexdigest()


def _lsp_docs(transport: LspTransport) -> dict[str, dict[str, Any]]:
    docs = getattr(transport, "_omg_lsp_docs", None)
    if not isinstance(docs, dict):
        docs = {}
        try:
            setattr(transport, "_omg_lsp_docs", docs)
        except (AttributeError, TypeError):
            pass
    return docs


def ensure_lsp_session(
    transport: LspTransport, *, root: Path, path: str | None
) -> None:
    """Initialize once; didOpen once per URI; didChange when disk text changes."""
    if not getattr(transport, "_omg_lsp_initialized", False):
        _arm_lsp_deadline(transport)
        root_uri = Path(root).resolve().as_uri()
        transport.request(
            "initialize",
            {
                "processId": os.getpid(),
                "rootUri": root_uri,
                "capabilities": {
                    "workspace": {
                        "configuration": True,
                        "workspaceFolders": True,
                    },
                    "window": {"workDoneProgress": True},
                    "textDocument": {
                        "hover": {"contentFormat": ["markdown", "plaintext"]},
                        "definition": {"linkSupport": True},
                    },
                },
                "workspaceFolders": [{"uri": root_uri, "name": Path(root).name}],
            },
        )
        try:
            setattr(
                transport,
                "_omg_workspace_folders",
                [{"uri": root_uri, "name": Path(root).name}],
            )
        except (AttributeError, TypeError):
            pass
        _lsp_notify_or_request(transport, "initialized", {})
        try:
            setattr(transport, "_omg_lsp_initialized", True)
            setattr(transport, "_omg_lsp_ready", True)
        except (AttributeError, TypeError):
            pass
    if not path:
        return
    confined = confine_path(root, path)
    uri = confined.as_uri()
    opened = getattr(transport, "_omg_lsp_opened", None)
    if not isinstance(opened, set):
        opened = set()
        try:
            setattr(transport, "_omg_lsp_opened", opened)
        except (AttributeError, TypeError):
            pass
    docs = _lsp_docs(transport)
    fingerprint = _file_fingerprint(confined)
    text, truncated = _read_text_bounded(confined)
    current = docs.get(uri) if isinstance(docs.get(uri), dict) else None
    if truncated:
        version = 1
        if current is not None and isinstance(current.get("version"), int):
            version = current["version"]
        docs[uri] = {
            "version": version,
            "fingerprint": fingerprint,
            "truncated": True,
        }
        return
    if current is None or uri not in opened:
        _lsp_notify_or_request(
            transport,
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": uri,
                    "languageId": _language_id_for(confined),
                    "version": 1,
                    "text": text,
                }
            },
        )
        docs[uri] = {"version": 1, "fingerprint": fingerprint, "truncated": False}
        opened.add(uri)
        return
    if current.get("fingerprint") == fingerprint:
        current["truncated"] = False
        return
    version = int(current.get("version") or 1) + 1
    _lsp_notify_or_request(
        transport,
        "textDocument/didChange",
        {
            "textDocument": {"uri": uri, "version": version},
            "contentChanges": [{"text": text}],
        },
    )
    current["version"] = version
    current["fingerprint"] = fingerprint
    current["truncated"] = False


def _lsp_doc_version(transport: LspTransport, uri: str) -> int:
    docs = getattr(transport, "_omg_lsp_docs", None)
    if isinstance(docs, dict):
        row = docs.get(uri)
        if isinstance(row, dict) and isinstance(row.get("version"), int):
            return row["version"]
    return 1


def lsp_operation(
    operation: str,
    *,
    root: Path,
    path: str | None = None,
    capability_mode: str = READ_ONLY,
    apply: bool = False,
    transport: LspTransport | None = None,
    query: str | None = None,
    new_name: str | None = None,
    line: Any = None,
    character: Any = None,
    end_line: Any = None,
    end_character: Any = None,
    range_arg: Any = None,
) -> dict[str, Any]:
    if operation == "servers":
        return {
            "ok": True,
            "verified": False,
            "observed": False,
            "healthy": False,
            "servers": inventory_lsp_servers(),
            "note": "inventory only; not Grok-native LSP; not live AG",
        }
    if operation not in _LSP_METHODS:
        raise ToolsError("E_LSP_OP", f"unknown lsp operation {operation}")
    if transport is None:
        raise ToolsError(
            "E_LSP_NO_SERVER",
            "no language server transport; pass a sidecar transport or --lsp-command "
            "(omg lsp remains host-owned status/validate only)",
        )
    mode = capability_mode_of(capability_mode)
    if operation in {"rename", "code_action"} and apply:
        require_read_write(mode, f"lsp.{operation}")
        raise ToolsError(
            "E_LSP_APPLY_UNSUPPORTED",
            "sidecar --apply does not write WorkspaceEdit; omit --apply for preview",
        )
    elif operation in {"rename", "code_action"} and not apply:
        # Preview is allowed read-only.
        pass
    if operation in LSP_SEMANTIC_DOC_OPS:
        if not path:
            raise ToolsError("E_PATH", "path is required")
        truncated = _read_text_bounded(confine_path(root, path))[1]
        if truncated:
            raise ToolsError(
                "E_LSP_TRUNCATED",
                "document exceeds sidecar size bound; refusing semantic analysis "
                "of a truncated prefix",
                details={
                    "truncated": True,
                    "max_bytes": MAX_RESULT_BYTES,
                    "path": str(path),
                },
            )
    ensure_lsp_session(transport, root=root, path=path)
    params: dict[str, Any]
    if operation == "workspace_symbols":
        params = {"query": query or ""}
    elif operation == "code_action_resolve":
        params = {"title": query or "fake"}
    else:
        if not path:
            raise ToolsError("E_PATH", "path is required")
        confined = confine_path(root, path)
        uri = confined.as_uri()
        version = _lsp_doc_version(transport, uri)
        if operation == "code_action":
            params = {
                "textDocument": {"uri": uri},
                "range": lsp_range(
                    line, character, end_line, end_character, range_arg
                ),
                "context": {"diagnostics": []},
            }
        elif operation == "diagnostics":
            params = {"textDocument": {"uri": uri}}
        else:
            params = {
                "textDocument": {"uri": uri, "version": version},
                "position": lsp_position(line, character),
            }
            if operation == "rename":
                params["newName"] = new_name or "Renamed"
            if operation == "references":
                params["context"] = {"includeDeclaration": True}
    method = _LSP_METHODS[operation]
    try:
        result = transport.request(method, params)
    except ToolsError as exc:
        if not (
            operation in {"hover", "definition"}
            and _lsp_rpc_is_content_modified(exc)
        ):
            raise
        result = None
    if operation in {"hover", "definition"}:
        result = _await_inspectable_semantic(
            transport, operation, method, params, result
        )
    truncated_flag = False
    if path:
        row = _lsp_docs(transport).get(confine_path(root, path).as_uri())
        if isinstance(row, dict):
            truncated_flag = bool(row.get("truncated"))
    return _bounded(
        {
            "ok": True,
            "verified": False,
            "operation": operation,
            "apply": bool(apply) if operation in {"rename", "code_action"} else False,
            "capability_mode": mode,
            "session_ready": bool(getattr(transport, "_omg_lsp_ready", False)),
            "truncated": truncated_flag,
            "result": result,
            "note": "sidecar protocol result; not Grok-native; not live AG evidence",
        }
    )


def _astgrep_identity_ok(path: str) -> bool:
    """``sg`` on Linux is often the shadow-utils group tool, not ast-grep."""
    try:
        proc = subprocess.run(
            [path, "--help"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    blob = f"{proc.stdout or ''}{proc.stderr or ''}".lower()
    return "ast-grep" in blob or "--pattern" in blob


def _astgrep_candidates() -> list[str]:
    """Prefer ``ast-grep`` (PATH then cargo bin) over identity-checked ``sg``."""
    found: list[str] = []
    seen: set[str] = set()

    def _add(path: str | None) -> None:
        if not path:
            return
        key = os.path.normcase(os.path.normpath(path))
        if key in seen:
            return
        seen.add(key)
        found.append(path)

    _add(shutil.which("ast-grep"))
    cargo_roots: list[Path] = []
    cargo_home = os.environ.get("CARGO_HOME")
    if cargo_home:
        cargo_roots.append(Path(cargo_home))
    cargo_roots.append(Path.home() / ".cargo")
    ast_names = ["ast-grep", "ast-grep.exe"]
    sg_names = ["sg", "sg.exe"]
    if os.name == "nt":
        ast_names.extend(["ast-grep.cmd", "ast-grep.bat"])
        sg_names.extend(["sg.cmd", "sg.bat"])
    for root in cargo_roots:
        for name in ast_names:
            candidate = root / "bin" / name
            try:
                if candidate.is_file():
                    _add(str(candidate))
            except OSError:
                continue
    _add(shutil.which("sg"))
    for root in cargo_roots:
        for name in sg_names:
            candidate = root / "bin" / name
            try:
                if candidate.is_file():
                    _add(str(candidate))
            except OSError:
                continue
    return found


def _astgrep_bin() -> str | None:
    for path in _astgrep_candidates():
        if _astgrep_identity_ok(path):
            return path
    return None


def ast_search(
    *,
    root: Path,
    pattern: str,
    lang: str,
    path: str | None = None,
) -> dict[str, Any]:
    if not pattern or any(token in pattern for token in ("$(", "`", "&&", "|", ";", "\n")):
        raise ToolsError("E_AST_PATTERN", "pattern must be a bounded AST-grep pattern, not a shell snippet")
    binary = _astgrep_bin()
    if not binary:
        raise ToolsError(
            "E_ASTGREP_MISSING",
            "ast-grep not on PATH; install it to enable structural search "
            "(OMG does not fake matches)",
        )
    target = confine_path(root, path or ".")
    argv = [binary, "run", "--pattern", pattern, "--lang", lang, "--json", str(target)]
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=10,
            cwd=str(root),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ToolsError("E_AST_TIMEOUT", "ast-grep timed out") from exc
    if proc.returncode not in {0, 1}:
        raise ToolsError("E_ASTGREP", proc.stderr.strip() or "ast-grep failed")
    try:
        matches = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise ToolsError("E_ASTGREP", "ast-grep returned non-JSON") from exc
    if not isinstance(matches, list):
        matches = []
    matches = matches[:MAX_AST_MATCHES]
    return _bounded(
        {
            "ok": True,
            "verified": False,
            "binary": binary,
            "count": len(matches),
            "matches": matches,
            "dry_run": True,
        }
    )


def ast_replace(
    *,
    root: Path,
    pattern: str,
    rewrite: str,
    lang: str,
    path: str | None = None,
    write: bool = False,
    capability_mode: str = READ_ONLY,
) -> dict[str, Any]:
    if write:
        require_read_write(capability_mode, "ast.replace")
    preview = ast_search(root=root, pattern=pattern, lang=lang, path=path)
    if not write:
        preview["dry_run"] = True
        preview["applied"] = False
        preview["rewrite"] = rewrite
        preview["note"] = "default dry-run; pass write=true with read-write to apply"
        return preview
    binary = _astgrep_bin()
    if not binary:
        raise ToolsError("E_ASTGREP_MISSING", "ast-grep not on PATH")
    target = confine_path(root, path or ".")
    argv = [
        binary,
        "run",
        "--pattern",
        pattern,
        "--rewrite",
        rewrite,
        "--lang",
        lang,
        str(target),
    ]
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=10, cwd=str(root))
    if proc.returncode not in {0, 1}:
        raise ToolsError("E_ASTGREP", proc.stderr.strip() or "ast-grep rewrite failed")
    return _bounded(
        {
            "ok": True,
            "verified": False,
            "dry_run": False,
            "applied": True,
            "preview_count": preview.get("count"),
            "note": "applied after preview; caller must re-search to confirm",
        }
    )


def _git_dirty(root: Path) -> bool | None:
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=str(root),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return bool(proc.stdout.strip())


def _worktree_fingerprint(root: Path) -> str:
    """Content-sensitive dirty fingerprint (HEAD + diff + untracked bytes)."""
    digest = hashlib.sha256()
    digest.update((_git_head(root) or "").encode("utf-8"))
    try:
        diff = subprocess.run(
            ["git", "diff", "HEAD"],
            capture_output=True,
            timeout=15,
            cwd=str(root),
            check=False,
        )
        if diff.returncode == 0:
            digest.update(diff.stdout)
        others = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard", "-z"],
            capture_output=True,
            timeout=15,
            cwd=str(root),
            check=False,
        )
        if others.returncode == 0:
            for rel in others.stdout.split(b"\0"):
                if not rel:
                    continue
                digest.update(rel)
                try:
                    digest.update((Path(root) / rel.decode("utf-8", "surrogateescape")).read_bytes()[:65536])
                except OSError:
                    pass
            return digest.hexdigest()
    except (OSError, subprocess.TimeoutExpired):
        pass
    files, _truncated = _iter_index_files(root)
    base = Path(root).resolve()
    for path in files:
        try:
            digest.update(path.relative_to(base).as_posix().encode("utf-8"))
            digest.update(path.read_bytes()[:65536])
        except (OSError, ValueError):
            continue
    return digest.hexdigest()


def _git_head(root: Path) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=str(root),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    sha = proc.stdout.strip()
    return sha or None


def _effective_codegraph_mode(mode: str, dirty: bool | None) -> str:
    if mode not in CODEGRAPH_MODES:
        raise ToolsError("E_CODEGRAPH_MODE", f"mode must be one of {CODEGRAPH_MODES}")
    if mode == "off":
        return "off"
    if mode == "auto":
        if dirty:
            return "local"
        return "shared"
    return mode


def _codegraph_index_path(root: Path, kind: str) -> Path:
    return Path(root).resolve() / ".omg" / "artifacts" / "codegraph" / f"{kind}-index.json"


def _load_codegraph_index(root: Path, kind: str) -> dict[str, Any] | None:
    path = _codegraph_index_path(root, kind)
    try:
        confined = confine_path(root, path)
    except ToolsError:
        return None
    if not confined.is_file():
        return None
    try:
        payload = json.loads(confined.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("schema") != CODEGRAPH_INDEX_SCHEMA:
        return None
    if payload.get("kind") != kind:
        return None
    if payload.get("indexer") != CODEGRAPH_INDEXER:
        return None
    files = payload.get("files")
    if not isinstance(files, list):
        return None
    return payload


_PY_IMPORT = re.compile(
    r"^(?:from\s+([\w.]+)\s+import|import\s+([\w.]+))",
    re.MULTILINE,
)
_PY_DEF = re.compile(r"^(?:async\s+)?def\s+(\w+)", re.MULTILINE)
_PY_CLASS = re.compile(r"^class\s+(\w+)", re.MULTILINE)
_JS_IMPORT = re.compile(
    r"(?:from\s+['\"]([^'\"]+)['\"]|import\s+['\"]([^'\"]+)['\"]|require\(\s*['\"]([^'\"]+)['\"])"
)
_JS_SYM = re.compile(
    r"^(?:export\s+)?(?:async\s+)?function\s+(\w+)|^(?:export\s+)?class\s+(\w+)",
    re.MULTILINE,
)
_GO_FN = re.compile(r"^func\s+(?:\([^)]+\)\s+)?(\w+)", re.MULTILINE)
_RS_FN = re.compile(r"^(?:pub(?:\([^)]+\))?\s+)?(?:async\s+)?fn\s+(\w+)", re.MULTILINE)
_JAVA_SYM = re.compile(
    r"^(?:public|protected|private|static|\s)+(?:class|interface|enum)\s+(\w+)",
    re.MULTILINE,
)


def _imported_ident(name: str) -> str:
    parts = [part for part in re.split(r"[./:\\]+", name.strip()) if part]
    return parts[-1] if parts else ""


def _extract_import_symbols(
    path: Path, text: str
) -> tuple[list[str], list[dict[str, Any]], list[dict[str, Any]]]:
    suffix = path.suffix.lower()
    imports: list[str] = []
    symbols: list[dict[str, Any]] = []
    import_sites: list[dict[str, Any]] = []

    def _add_imp(name: str | None, match: re.Match[str] | None = None) -> None:
        if not name:
            return
        if name not in imports:
            imports.append(name)
        if match is not None:
            import_sites.append(
                {"name": name, "line": text[: match.start()].count("\n")}
            )

    def _add_sym(name: str | None, kind: str, match: re.Match[str]) -> None:
        if not name or len(symbols) >= MAX_SYMBOLS_PER_FILE:
            return
        line = text[: match.start()].count("\n")
        symbols.append({"name": name, "kind": kind, "line": line})

    if suffix in {".py", ".pyi"}:
        for match in _PY_IMPORT.finditer(text):
            _add_imp(match.group(1) or match.group(2), match)
        for match in _PY_DEF.finditer(text):
            _add_sym(match.group(1), "function", match)
        for match in _PY_CLASS.finditer(text):
            _add_sym(match.group(1), "class", match)
        return imports, symbols, import_sites
    if suffix in {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}:
        for match in _JS_IMPORT.finditer(text):
            _add_imp(next((g for g in match.groups() if g), None), match)
        for match in _JS_SYM.finditer(text):
            _add_sym(match.group(1), "function", match)
            _add_sym(match.group(2), "class", match)
        return imports, symbols, import_sites
    if suffix == ".go":
        for match in re.finditer(
            r'"([^"]+)"', text.split(")", 1)[0] if "import" in text else ""
        ):
            _add_imp(match.group(1), match)
        for match in _GO_FN.finditer(text):
            _add_sym(match.group(1), "function", match)
        return imports, symbols, import_sites
    if suffix == ".rs":
        for match in re.finditer(r"^use\s+([\w:]+)", text, re.MULTILINE):
            _add_imp(match.group(1), match)
        for match in _RS_FN.finditer(text):
            _add_sym(match.group(1), "function", match)
        return imports, symbols, import_sites
    if suffix == ".java":
        for match in re.finditer(r"^import\s+([\w.]+)", text, re.MULTILINE):
            _add_imp(match.group(1), match)
        for match in _JAVA_SYM.finditer(text):
            _add_sym(match.group(1), "type", match)
        return imports, symbols, import_sites
    return imports, symbols, import_sites


def _scip_lite_occurrences(
    rel: str,
    text: str,
    symbols: list[dict[str, Any]],
    import_sites: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Bounded SCIP-inspired JSON occurrences (not SCIP protobuf)."""
    occs: list[dict[str, Any]] = []
    seen: set[tuple[str, int, str]] = set()
    def_line: dict[str, int] = {}

    def _add(name: str, role: str, line: int) -> None:
        if not name or role not in {"definition", "reference"}:
            return
        if len(occs) >= MAX_OCCURRENCES_PER_FILE:
            return
        key = (name, int(line), role)
        if key in seen:
            return
        seen.add(key)
        occs.append(
            {
                "path": rel,
                "name": name,
                "role": role,
                "line": int(line),
                "symbol_id": f"{rel}#{name}",
            }
        )

    for sym in symbols:
        if not isinstance(sym, dict):
            continue
        name = str(sym.get("name") or "")
        if not name:
            continue
        line = int(sym.get("line") or 0)
        def_line[name] = line
        _add(name, "definition", line)

    for site in import_sites:
        if not isinstance(site, dict):
            continue
        ident = _imported_ident(str(site.get("name") or ""))
        if not ident:
            continue
        _add(ident, "reference", int(site.get("line") or 0))

    known: list[str] = []
    seen_names: set[str] = set()
    for name in list(def_line):
        if name not in seen_names and _IDENT.match(name):
            seen_names.add(name)
            known.append(name)
    for site in import_sites:
        if not isinstance(site, dict):
            continue
        ident = _imported_ident(str(site.get("name") or ""))
        if ident not in seen_names and _IDENT.match(ident):
            seen_names.add(ident)
            known.append(ident)
    if not known:
        return occs
    pattern = re.compile(r"\b(?:" + "|".join(re.escape(name) for name in known) + r")\b")
    for match in pattern.finditer(text):
        if len(occs) >= MAX_OCCURRENCES_PER_FILE:
            break
        name = match.group(0)
        line = text[: match.start()].count("\n")
        if def_line.get(name) == line:
            continue
        _add(name, "reference", line)
    return occs


def _iter_index_files(root: Path) -> tuple[list[Path], bool]:
    base = Path(root).resolve()
    files: list[Path] = []
    truncated = False
    stack = [base]
    while stack:
        current = stack.pop()
        try:
            entries = list(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            if len(files) >= MAX_INDEX_FILES:
                truncated = True
                return files, truncated
            name = entry.name
            try:
                if entry.is_symlink():
                    continue
                is_dir = entry.is_dir()
            except OSError:
                continue
            if is_dir:
                if name in INDEX_SKIP_DIRS or name.startswith("."):
                    continue
                stack.append(entry)
                continue
            if entry.suffix.lower() not in INDEX_SUFFIXES:
                continue
            try:
                confined = confine_path(base, entry)
            except ToolsError:
                continue
            files.append(confined)
    return files, truncated


def _scan_workspace(
    root: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
    files, truncated = _iter_index_files(root)
    rows: list[dict[str, Any]] = []
    occurrences: list[dict[str, Any]] = []
    base = Path(root).resolve()
    for path in files:
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size > MAX_INDEX_FILE_BYTES:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if len(text.encode("utf-8")) > MAX_INDEX_FILE_BYTES:
            text = text[:MAX_INDEX_FILE_BYTES]
        imports, symbols, import_sites = _extract_import_symbols(path, text)
        try:
            rel = path.relative_to(base).as_posix()
        except ValueError:
            continue
        rows.append({"path": rel, "imports": imports, "symbols": symbols})
        if len(occurrences) < MAX_OCCURRENCES:
            room = MAX_OCCURRENCES - len(occurrences)
            occs = _scip_lite_occurrences(rel, text, symbols, import_sites)
            occurrences.extend(occs[:room])
    return rows, occurrences, truncated


def _codegraph_notes(effective: str, dirty: bool | None) -> str:
    note = {
        "off": "CodeGraph disabled",
        "shared": (
            "shared/baseline import/symbol index; SCIP-inspired JSON occurrences, "
            "not SCIP protobuf; does not include uncommitted worktree changes"
        ),
        "local": (
            "worktree-local import/symbol index (SCIP-inspired JSON, not SCIP protobuf); "
            "branch-accurate only when built from this tree and not stale"
        ),
    }[effective]
    if effective == "shared" and dirty:
        note += "; worktree is dirty — do not treat shared hits as this branch"
    return note


def codegraph_status(*, root: Path, mode: str = "auto") -> dict[str, Any]:
    dirty = _git_dirty(root)
    head = _git_head(root)
    effective = _effective_codegraph_mode(mode, dirty)
    index = None if effective == "off" else _load_codegraph_index(root, effective)
    present = index is not None
    stale = False
    if present and index is not None:
        fingerprint = _worktree_fingerprint(root)
        stale = (
            index.get("head_sha") != head
            or index.get("worktree_fingerprint") != fingerprint
        )
    branch_accurate = (
        effective == "local"
        and present
        and not stale
        and (index or {}).get("kind") == "local"
    )
    if effective == "shared":
        branch_accurate = False
    note = _codegraph_notes(effective, dirty)
    if effective == "off":
        pass
    elif not present:
        note += ". No index on disk; run omg tools codegraph index."
    elif stale:
        note += ". Index is stale relative to HEAD/dirty; re-run index."
    return {
        "ok": True,
        "verified": False,
        "observed": False,
        "healthy": False,
        "requested_mode": mode,
        "effective_mode": effective,
        "index_kind": None if not present else effective,
        "indexer": CODEGRAPH_INDEXER,
        "not_scip": True,
        "branch_accurate": branch_accurate,
        "worktree_dirty": dirty,
        "head_sha": head,
        "index_present": present,
        "index_stale": stale if present else False,
        "file_count": (index or {}).get("file_count") if present else 0,
        "note": note,
    }


def codegraph_index(*, root: Path, mode: str = "local") -> dict[str, Any]:
    dirty = _git_dirty(root)
    effective = _effective_codegraph_mode(mode, dirty)
    if effective == "off":
        raise ToolsError("E_CODEGRAPH_OFF", "CodeGraph mode is off")
    if effective == "shared" and dirty:
        raise ToolsError(
            "E_CODEGRAPH_DIRTY",
            "shared index refuses dirty worktrees; commit or use --mode local",
        )
    rows, occurrences, truncated = _scan_workspace(root)
    head = _git_head(root)
    fingerprint = _worktree_fingerprint(root)
    payload = {
        "schema": CODEGRAPH_INDEX_SCHEMA,
        "kind": effective,
        "indexer": CODEGRAPH_INDEXER,
        "not_scip": True,
        "verified": False,
        "head_sha": head,
        "worktree_dirty": dirty,
        "worktree_fingerprint": fingerprint,
        "file_count": len(rows),
        "truncated": truncated,
        "files": rows,
        "occurrences": occurrences,
        "note": (
            "toy import/symbol scan with SCIP-inspired JSON occurrences; "
            "not SCIP protobuf / a real SCIP indexer; "
            + (
                "not a shared branch-accurate graph"
                if effective == "shared"
                else "local to this worktree"
            )
        ),
    }
    dest = _codegraph_index_path(root, effective)
    confined = confine_path(root, dest)
    confined.parent.mkdir(parents=True, exist_ok=True)
    tmp = confined.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, confined)
    status = codegraph_status(root=root, mode=effective)
    status["built"] = True
    status["truncated"] = truncated
    status["path"] = ".omg/artifacts/codegraph/" + f"{effective}-index.json"
    return _bounded(status)


def codegraph_query(*, root: Path, mode: str = "auto", query: str = "") -> dict[str, Any]:
    status = codegraph_status(root=root, mode=mode)
    if status["effective_mode"] == "off":
        raise ToolsError("E_CODEGRAPH_OFF", "CodeGraph mode is off", details=status)
    if not status["index_present"]:
        raise ToolsError(
            "E_CODEGRAPH_NO_INDEX",
            "no import/symbol index; run omg tools codegraph index",
            details=status,
        )
    kind = str(status["effective_mode"])
    index = _load_codegraph_index(root, kind)
    if index is None:
        raise ToolsError(
            "E_CODEGRAPH_NO_INDEX",
            "index missing after status check",
            details=status,
        )
    needle = query.strip().lower()
    hits: list[dict[str, Any]] = []
    for row in index.get("files") or []:
        if not isinstance(row, dict) or len(hits) >= MAX_CODEGRAPH_HITS:
            break
        rel = str(row.get("path") or "")
        try:
            confine_path(root, rel)
        except ToolsError:
            continue
        for sym in row.get("symbols") or []:
            if len(hits) >= MAX_CODEGRAPH_HITS:
                break
            if not isinstance(sym, dict):
                continue
            name = str(sym.get("name") or "")
            if needle and needle not in name.lower() and needle not in rel.lower():
                continue
            if not needle and not name:
                continue
            hits.append(
                {
                    "path": rel,
                    "kind": "symbol",
                    "name": name,
                    "symbol_kind": sym.get("kind"),
                    "line": sym.get("line"),
                }
            )
        for imp in row.get("imports") or []:
            if len(hits) >= MAX_CODEGRAPH_HITS:
                break
            name = str(imp)
            if needle and needle not in name.lower() and needle not in rel.lower():
                continue
            if not needle:
                continue
            hits.append({"path": rel, "kind": "import", "name": name})
    for occ in index.get("occurrences") or []:
        if len(hits) >= MAX_CODEGRAPH_HITS:
            break
        if not isinstance(occ, dict):
            continue
        rel = str(occ.get("path") or "")
        try:
            confine_path(root, rel)
        except ToolsError:
            continue
        name = str(occ.get("name") or "")
        symbol_id = str(occ.get("symbol_id") or "")
        if needle:
            if (
                needle not in name.lower()
                and needle not in symbol_id.lower()
                and needle not in rel.lower()
            ):
                continue
        elif not name:
            continue
        role = occ.get("role")
        if role not in {"definition", "reference"}:
            continue
        hits.append(
            {
                "path": rel,
                "kind": "occurrence",
                "name": name,
                "role": role,
                "line": occ.get("line"),
                "symbol_id": symbol_id,
            }
        )
    return _bounded(
        {
            "ok": True,
            "verified": False,
            "answered_by": kind,
            "effective_mode": kind,
            "branch_accurate": bool(status["branch_accurate"]),
            "index_stale": bool(status.get("index_stale")),
            "indexer": CODEGRAPH_INDEXER,
            "not_scip": True,
            "query": query,
            "count": len(hits),
            "hits": hits,
            "note": status["note"],
        }
    )


def research_enabled(env: Mapping[str, str] | None = None) -> bool:
    flag = str(_env(env).get("OMG_TOOLS_NETWORK", "")).strip().lower()
    return flag in {"1", "true", "yes", "on"}


def research_status(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    enabled = research_enabled(env)
    return {
        "ok": True,
        "verified": False,
        "enabled": enabled,
        "provider": None,
        "credentials_bundled": False,
        "note": (
            "network research is opt-in via OMG_TOOLS_NETWORK=1; "
            "no provider is configured in this first cut"
        ),
    }


def research_search(query: str, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    status = research_status(env)
    if not status["enabled"]:
        raise ToolsError(
            "E_NETWORK_DISABLED",
            "network research is opt-in; set OMG_TOOLS_NETWORK=1 after choosing a provider",
            details=status,
        )
    raise ToolsError(
        "E_NETWORK_NO_PROVIDER",
        "no research provider configured (credentials are never bundled)",
        details=status,
    )


def doctor_payload(
    *,
    root: Path,
    env: Mapping[str, str] | None = None,
    strict: bool = False,
) -> dict[str, Any]:
    ast_bin = _astgrep_bin()
    research = research_status(env)
    payload: dict[str, Any] = {
        "schema": SCHEMA,
        "ok": True,
        "strict": strict,
        "configured": True,
        "installed": True,
        "enabled": True,
        "loadable": True,
        "observed": False,
        "healthy": False,
        "verified": False,
        "omg_lsp_host_owned": True,
        "windows_confinement": "resolve+relative_to",
        "dependencies": {
            "ast-grep": {
                "present": ast_bin is not None,
                "path": ast_bin,
                "required": False,
                "remediation": (
                    "install ast-grep on PATH (cargo install ast-grep or npm); "
                    "do not use shadow-utils /usr/bin/sg"
                ),
            },
            "lsp_servers": inventory_lsp_servers(),
            "codegraph": codegraph_status(root=root, mode="auto"),
            "network_research": research,
        },
        "tool_names": list(SIDECAR_TOOL_NAMES),
        "note": (
            "OMG tools sidecar. Not Grok-native LSP. Not live Antigravity evidence. "
            "omg lsp remains host-owned. CodeGraph is a toy import/symbol index with "
            "SCIP-inspired JSON occurrences, not SCIP protobuf. "
            "Detected language servers are not ready until explicitly started."
        ),
    }
    errors: list[str] = []
    if research["enabled"] and research["provider"] is None:
        errors.append("OMG_TOOLS_NETWORK is set but no provider is configured")
    if strict and errors:
        payload["ok"] = False
        payload["errors"] = errors
    elif errors:
        payload["warnings"] = errors
    return payload


def inspect_tools_sidecar(root: Path | None = None) -> dict[str, Any]:
    base = Path(root) if root is not None else Path.cwd()
    payload = doctor_payload(root=base, strict=False)
    payload["observed"] = False
    payload["healthy"] = False
    payload["verified"] = False
    return payload


def dispatch_sidecar_tool(
    name: str,
    arguments: Mapping[str, Any] | None = None,
    *,
    root: Path,
    env: Mapping[str, str] | None = None,
    capability_mode: str = READ_ONLY,
    transport: LspTransport | None = None,
) -> dict[str, Any]:
    args = dict(arguments or {})
    mode = effective_capability_mode(capability_mode, args.get("capability_mode"))
    if name == "omg.tools.doctor":
        return doctor_payload(root=root, env=env, strict=bool(args.get("strict")))
    if name.startswith("omg.tools.lsp."):
        op = name.rsplit(".", 1)[-1]
        return lsp_operation(
            op,
            root=root,
            path=args.get("path"),
            capability_mode=mode,
            apply=bool(args.get("apply")),
            transport=transport,
            query=args.get("query"),
            new_name=args.get("new_name"),
            line=args.get("line"),
            character=args.get("character"),
            end_line=args.get("end_line"),
            end_character=args.get("end_character"),
            range_arg=args.get("range"),
        )
    if name == "omg.tools.ast.search":
        return ast_search(
            root=root,
            pattern=str(args.get("pattern") or ""),
            lang=str(args.get("lang") or "python"),
            path=args.get("path"),
        )
    if name == "omg.tools.ast.replace":
        return ast_replace(
            root=root,
            pattern=str(args.get("pattern") or ""),
            rewrite=str(args.get("rewrite") or ""),
            lang=str(args.get("lang") or "python"),
            path=args.get("path"),
            write=bool(args.get("write")),
            capability_mode=mode,
        )
    if name == "omg.tools.codegraph.status":
        return codegraph_status(root=root, mode=str(args.get("mode") or "auto"))
    if name == "omg.tools.codegraph.query":
        return codegraph_query(
            root=root,
            mode=str(args.get("mode") or "auto"),
            query=str(args.get("query") or ""),
        )
    if name == "omg.tools.codegraph.index":
        require_read_write(mode, "codegraph.index")
        return codegraph_index(root=root, mode=str(args.get("mode") or "local"))
    if name == "omg.tools.research.status":
        return research_status(env)
    if name == "omg.tools.research.search":
        return research_search(str(args.get("query") or ""), env)
    raise ToolsError("E_UNKNOWN_TOOL", f"unknown sidecar tool {name}")


def list_mcp_tools() -> list[dict[str, Any]]:
    return [
        {
            "name": name,
            "description": f"OMG tools sidecar ({name}); not Grok-native; never sets verified",
            "inputSchema": {"type": "object"},
        }
        for name in SIDECAR_TOOL_NAMES
    ]


def handle_mcp_rpc(
    message: Mapping[str, Any],
    *,
    root: Path,
    env: Mapping[str, str] | None = None,
    capability_mode: str = READ_ONLY,
    transport: LspTransport | None = None,
) -> dict[str, Any]:
    msg_id = message.get("id")
    method = message.get("method")
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "serverInfo": {"name": "omg-tools-sidecar", "version": SCHEMA},
                "capabilities": {"tools": {}},
            },
        }
    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {"tools": list_mcp_tools()},
        }
    if method == "tools/call":
        params = message.get("params") or {}
        name = str(params.get("name") or "")
        try:
            result = dispatch_sidecar_tool(
                name,
                params.get("arguments") or {},
                root=root,
                env=env,
                capability_mode=capability_mode,
                transport=transport,
            )
            encoded = json.dumps(result, ensure_ascii=False)
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "content": [{"type": "text", "text": encoded[:MAX_RESULT_BYTES]}],
                    "isError": False,
                    "verified": False,
                },
            }
        except ToolsError as exc:
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(exc.to_dict())}],
                    "isError": True,
                    "verified": False,
                },
            }
    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "error": {"code": -32601, "message": f"unsupported method {method}"},
    }


def run_tools_stdio(
    root: Path,
    *,
    stdin=None,
    stdout=None,
    env: Mapping[str, str] | None = None,
    capability_mode: str = READ_ONLY,
    transport: LspTransport | None = None,
) -> int:
    inn = stdin if stdin is not None else sys.stdin
    out = stdout if stdout is not None else sys.stdout
    for line in inn:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(message, dict):
            continue
        if message.get("method") == "notifications/cancelled":
            continue
        reply = handle_mcp_rpc(
            message,
            root=root,
            env=env,
            capability_mode=capability_mode,
            transport=transport,
        )
        out.write(json.dumps(reply, ensure_ascii=False) + "\n")
        out.flush()
        if message.get("method") == "shutdown":
            return 0
    return 0


__all__ = [
    "CODEGRAPH_MODES",
    "FakeLspTransport",
    "MAX_RESULT_BYTES",
    "SCHEMA",
    "SIDECAR_TOOL_NAMES",
    "StdioLspTransport",
    "ToolsError",
    "ast_replace",
    "ast_search",
    "codegraph_index",
    "codegraph_query",
    "codegraph_status",
    "confine_path",
    "dispatch_sidecar_tool",
    "doctor_payload",
    "handle_mcp_rpc",
    "inspect_tools_sidecar",
    "inventory_lsp_servers",
    "list_mcp_tools",
    "lsp_command_argv",
    "lsp_operation",
    "semantic_result_inspectable",
    "lsp_range",
    "media_descriptor",
    "research_search",
    "research_status",
    "run_tools_stdio",
]
