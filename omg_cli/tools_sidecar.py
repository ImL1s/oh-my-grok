"""OMG-owned tools sidecar (#73 first cut).

Semantic LSP / AST-grep / CodeGraph / research live here — never on
``omg lsp`` (host-owned ``.lsp.json`` probe) and never on ``omg mcp-server``
(``lsp.*`` names stay forbidden).

Honesty:
- Not Grok-native LSP.
- Not a live Antigravity MCP install.
- AST-grep missing → blocked, not fake success.
- Shared CodeGraph indexes are not worktree-accurate.
- Network research is opt-in (``OMG_TOOLS_NETWORK=1``).
- Never writes ``passes`` / ``verified``.
"""

from __future__ import annotations

import json
import os
import select
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol
from urllib.parse import unquote, urlparse

SCHEMA = "omg-tools-sidecar/v1"
MAX_RESULT_BYTES = 65_536
MAX_AST_MATCHES = 200
MAX_MEDIA_BYTES = 10 * 1024 * 1024
ALLOWED_MEDIA_MIMES = frozenset(
    {"image/png", "image/jpeg", "image/webp", "image/gif"}
)
CODEGRAPH_MODES = ("off", "auto", "shared", "local")
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
COMMON_LSP_SERVERS = (
    "pylsp",
    "pyright-langserver",
    "typescript-language-server",
    "gopls",
    "rust-analyzer",
    "clangd",
)
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
        if method == "textDocument/definition":
            return [
                {
                    "uri": body.get("textDocument", {}).get("uri", "file:///tmp/fake.py"),
                    "range": {
                        "start": {"line": 0, "character": 0},
                        "end": {"line": 0, "character": 4},
                    },
                }
            ]
        if method == "textDocument/references":
            return []
        if method == "textDocument/documentSymbol":
            return [
                {
                    "name": "Fake",
                    "kind": 5,
                    "range": {
                        "start": {"line": 0, "character": 0},
                        "end": {"line": 1, "character": 0},
                    },
                    "selectionRange": {
                        "start": {"line": 0, "character": 0},
                        "end": {"line": 0, "character": 4},
                    },
                }
            ]
        if method == "workspace/symbol":
            return []
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
        if method in {"shutdown", "initialized", "textDocument/didOpen"}:
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
        timeout_s: float = 5.0,
    ) -> None:
        if not argv:
            raise ToolsError("E_LSP_COMMAND", "lsp command is empty")
        self.timeout_s = timeout_s
        self._next_id = 1
        self._pending = bytearray()
        self._proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(cwd),
            bufsize=0,
        )

    def _write_message(self, payload: Mapping[str, Any]) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        header = f"Content-Length: {len(raw)}\r\n\r\n".encode("ascii")
        assert self._proc.stdin is not None
        try:
            self._proc.stdin.write(header + raw)
            self._proc.stdin.flush()
        except OSError as exc:
            raise ToolsError("E_LSP_CRASH", "language server stdin closed") from exc

    def _read_more(self, deadline: float) -> None:
        stdout = self._proc.stdout
        assert stdout is not None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ToolsError("E_LSP_TIMEOUT", f"read exceeded {self.timeout_s}s")
        fd = stdout.fileno()
        if os.name != "nt":
            ready, _, _ = select.select([fd], [], [], remaining)
            if not ready:
                raise ToolsError("E_LSP_TIMEOUT", f"read exceeded {self.timeout_s}s")
            try:
                chunk = os.read(fd, 4096)
            except OSError as exc:
                raise ToolsError("E_LSP_CRASH", "language server stdout closed") from exc
            if not chunk:
                raise ToolsError("E_LSP_CRASH", "language server stdout closed")
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
            raise ToolsError("E_LSP_TIMEOUT", f"read exceeded {self.timeout_s}s")
        if error:
            raise ToolsError("E_LSP_CRASH", "language server stdout closed") from error[0]
        chunk = holder[0] if holder else b""
        if not chunk:
            raise ToolsError("E_LSP_CRASH", "language server stdout closed")
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
        if self._proc.poll() is not None:
            raise ToolsError("E_LSP_CRASH", "language server exited")
        self._write_message(
            {"jsonrpc": "2.0", "method": method, "params": dict(params or {})}
        )

    def request(self, method: str, params: Mapping[str, Any] | None = None) -> Any:
        if self._proc.poll() is not None:
            raise ToolsError("E_LSP_CRASH", "language server exited")
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
        deadline = time.monotonic() + self.timeout_s
        while True:
            msg = self._read_message(deadline)
            if not isinstance(msg, dict):
                continue
            if msg.get("id") != msg_id:
                continue
            if msg.get("error") is not None:
                raise ToolsError("E_LSP_RPC", str(msg["error"]))
            return msg.get("result")

    def close(self) -> None:
        if self._proc.poll() is None:
            self._proc.kill()
        try:
            self._proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self._proc.kill()


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


def ensure_lsp_session(
    transport: LspTransport, *, root: Path, path: str | None
) -> None:
    """Send initialize/initialized (and didOpen when a path is known)."""
    if getattr(transport, "_omg_lsp_ready", False):
        return
    root_uri = Path(root).resolve().as_uri()
    transport.request(
        "initialize",
        {
            "processId": os.getpid(),
            "rootUri": root_uri,
            "capabilities": {},
            "workspaceFolders": [{"uri": root_uri, "name": Path(root).name}],
        },
    )
    _lsp_notify_or_request(transport, "initialized", {})
    if path:
        confined = confine_path(root, path)
        text = ""
        if confined.is_file():
            try:
                raw = confined.read_bytes()
            except OSError:
                raw = b""
            text = raw[:MAX_RESULT_BYTES].decode("utf-8", "replace")
        _lsp_notify_or_request(
            transport,
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": confined.as_uri(),
                    "languageId": _language_id_for(confined),
                    "version": 1,
                    "text": text,
                }
            },
        )
    try:
        setattr(transport, "_omg_lsp_ready", True)
    except (AttributeError, TypeError):
        pass


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
        params = {
            "textDocument": {"uri": uri, "version": 1},
            "position": {"line": 0, "character": 0},
        }
        if operation == "rename":
            params["newName"] = new_name or "Renamed"
        if operation == "references":
            params["context"] = {"includeDeclaration": True}
        if operation == "code_action":
            params["context"] = {"diagnostics": []}
        if operation == "diagnostics":
            params = {"textDocument": {"uri": uri}}
    result = transport.request(_LSP_METHODS[operation], params)
    return _bounded(
        {
            "ok": True,
            "verified": False,
            "operation": operation,
            "apply": bool(apply) if operation in {"rename", "code_action"} else False,
            "capability_mode": mode,
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


def _astgrep_bin() -> str | None:
    for name in ("ast-grep", "sg"):
        path = shutil.which(name)
        if path and _astgrep_identity_ok(path):
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


def codegraph_status(*, root: Path, mode: str = "auto") -> dict[str, Any]:
    if mode not in CODEGRAPH_MODES:
        raise ToolsError("E_CODEGRAPH_MODE", f"mode must be one of {CODEGRAPH_MODES}")
    dirty = _git_dirty(root)
    if mode == "off":
        effective = "off"
    elif mode == "auto":
        effective = "local" if dirty else "shared"
        if dirty is None:
            effective = "shared"
    else:
        effective = mode
    branch_accurate = effective == "local"
    note = {
        "off": "CodeGraph disabled",
        "shared": "shared/baseline index; does not include uncommitted worktree changes",
        "local": "worktree-local index (branch-accurate when built from this tree)",
    }[effective]
    if effective == "shared" and dirty:
        note += "; worktree is dirty — do not treat shared hits as this branch"
    return {
        "ok": True,
        "verified": False,
        "observed": False,
        "healthy": False,
        "requested_mode": mode,
        "effective_mode": effective,
        "branch_accurate": branch_accurate,
        "worktree_dirty": dirty,
        "index_present": False,
        "note": note + ". First cut has no indexer; queries stay blocked.",
    }


def codegraph_query(*, root: Path, mode: str = "auto", query: str = "") -> dict[str, Any]:
    status = codegraph_status(root=root, mode=mode)
    if status["effective_mode"] == "off":
        raise ToolsError("E_CODEGRAPH_OFF", "CodeGraph mode is off", details=status)
    raise ToolsError(
        "E_CODEGRAPH_NO_INDEX",
        "no branch-accurate index in this first cut",
        details=status,
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
                "remediation": "install ast-grep (sg) on PATH for structural search",
            },
            "lsp_servers": inventory_lsp_servers(),
            "codegraph": codegraph_status(root=root, mode="auto"),
            "network_research": research,
        },
        "tool_names": list(SIDECAR_TOOL_NAMES),
        "note": (
            "OMG tools sidecar first cut. Not Grok-native LSP. "
            "Not live Antigravity evidence. omg lsp remains host-owned."
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
    mode = capability_mode_of(str(args.get("capability_mode") or capability_mode))
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
    "codegraph_query",
    "codegraph_status",
    "confine_path",
    "dispatch_sidecar_tool",
    "doctor_payload",
    "handle_mcp_rpc",
    "inspect_tools_sidecar",
    "inventory_lsp_servers",
    "list_mcp_tools",
    "lsp_operation",
    "media_descriptor",
    "research_search",
    "research_status",
    "run_tools_stdio",
]
