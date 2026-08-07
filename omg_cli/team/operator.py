"""Identity-fenced Team pane operator control (#101).

All capture/focus/key/input effects must pass:

  Team identity → receipt chain → worker generation → exact pane proof (#98)
  → authorize → (re-probe) → tmux effect

Commands must not call tmux directly. Plane/topology stay read-only here.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from omg_cli.contracts.path_keys import (
    DATA_FILE_MODE,
    ensure_managed_dir,
    exclusive_lock,
)
from omg_cli.evidence import CLI_WRITER
from omg_cli.redaction import redact_text
from omg_cli.team.plane import (
    TeamError,
    _load_team_identity_chain,
    _worker_pane_liveness,
    load_team_meta,
    team_dir,
)
from omg_cli.team.runtime import resolve_team_ref, worker_pane_descriptors
from omg_cli.team.tmux import (
    ALLOWED_OPERATOR_KEYS,
    MAX_OPERATOR_CAPTURE_BYTES,
    MAX_OPERATOR_INPUT_BYTES,
    TmuxTeamError,
    attach_argv_for_target,
    capture_pane,
    focus_pane,
    send_key,
    send_literal,
    tmux_available,
)

_TMUX_PANE_ID = re.compile(r"^%[0-9]{1,16}$")
_WORKER_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")

DEFAULT_CAPTURE_LINES = 200
MAX_CAPTURE_LINES = 2000
MIN_WATCH_INTERVAL_S = 0.5
MAX_WATCH_INTERVAL_S = 60.0
DEFAULT_WATCH_INTERVAL_S = 1.0
MAX_WATCH_ITERATIONS = 3600

STATUS_LIVE = "live"
STATUS_GONE = "gone"
STATUS_MISMATCH = "identity_mismatch"
STATUS_UNKNOWN = "unknown"

_LIVENESS_TO_STATUS = {
    "alive": STATUS_LIVE,
    "proven_absent": STATUS_GONE,
    "present_foreign": STATUS_MISMATCH,
    "unknown": STATUS_UNKNOWN,
}


class OperatorError(RuntimeError):
    """Policy / identity refusal for operator pane control."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "E_OPERATOR_REFUSED",
        exit_code: int = 2,
        status: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.exit_code = exit_code
        self.status = status
        self.details = dict(details or {})


@dataclass(frozen=True)
class ExactPaneProof:
    """Bound exact-pane authority for one worker attempt."""

    run_id: str
    team_id: str
    worker_id: str
    attempt: int
    generation: int
    session: str
    session_id: str
    launch_nonce: str
    pane_id: str
    window_id: str | None
    expected_pid: int
    expected_pid_start: str | None
    owner_token_sha256: str | None
    leader_pane_id: str | None
    role: str | None
    provider: str | None
    posture: str | None
    worktree: str | None
    state: str | None
    ready: bool | None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _owner_token_sha256(meta: Mapping[str, Any]) -> str | None:
    token = meta.get("owner_token")
    if isinstance(token, str) and token:
        return _sha256_text(token)
    hashed = meta.get("owner_token_sha256")
    if isinstance(hashed, str) and hashed:
        return hashed
    return None


def _require_worker_id(worker_id: str) -> str:
    wid = str(worker_id or "").strip()
    if not wid or _WORKER_ID.fullmatch(wid) is None:
        raise OperatorError(
            f"invalid worker id {worker_id!r}",
            code="E_OPERATOR_WORKER_ID",
            exit_code=2,
        )
    return wid


def _require_key_name(key: str) -> str:
    if not isinstance(key, str) or not key:
        raise OperatorError(
            f"key not allowlisted: {key!r}",
            code="E_OPERATOR_KEY_REFUSED",
            exit_code=2,
        )
    # Reject before strip so whitespace / newline injection cannot normalize
    # into an allowlisted name.
    if key != key.strip() or any(ch.isspace() for ch in key) or ";" in key:
        raise OperatorError(
            f"key injection refused: {key!r}",
            code="E_OPERATOR_KEY_REFUSED",
            exit_code=2,
        )
    if key not in ALLOWED_OPERATOR_KEYS:
        raise OperatorError(
            f"key not allowlisted: {key!r}",
            code="E_OPERATOR_KEY_REFUSED",
            exit_code=2,
        )
    return key


def _validate_literal_input(text: str) -> str:
    if not isinstance(text, str):
        raise OperatorError(
            "input text must be a string",
            code="E_OPERATOR_INPUT_REFUSED",
            exit_code=2,
        )
    if "\0" in text:
        raise OperatorError(
            "input refuses NUL",
            code="E_OPERATOR_INPUT_REFUSED",
            exit_code=2,
        )
    # Reject C0 controls except TAB (\t) and common whitespace CR/LF which
    # literal mode still forbids — operator must use --submit for Enter.
    for ch in text:
        code = ord(ch)
        if code < 0x20 and ch not in ("\t",):
            raise OperatorError(
                "input refuses control characters (use --submit for Enter)",
                code="E_OPERATOR_INPUT_REFUSED",
                exit_code=2,
            )
        if 0x7F <= code <= 0x9F:
            raise OperatorError(
                "input refuses C1 controls",
                code="E_OPERATOR_INPUT_REFUSED",
                exit_code=2,
            )
    raw = text.encode("utf-8")
    if len(raw) > MAX_OPERATOR_INPUT_BYTES:
        raise OperatorError(
            f"input exceeds {MAX_OPERATOR_INPUT_BYTES} UTF-8 bytes",
            code="E_OPERATOR_INPUT_OVERSIZE",
            exit_code=2,
        )
    return text


def _bound_lines(lines: int | None) -> int:
    if lines is None:
        return DEFAULT_CAPTURE_LINES
    if isinstance(lines, bool) or not isinstance(lines, int) or lines < 1:
        raise OperatorError(
            "capture --lines must be a positive integer",
            code="E_OPERATOR_LINES",
            exit_code=2,
        )
    return min(lines, MAX_CAPTURE_LINES)


def _strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", "", text)


def _liveness_status(state: str) -> str:
    return _LIVENESS_TO_STATUS.get(state, STATUS_UNKNOWN)


def resolve_live_worker(
    root: Path | str,
    identity: str | None,
    worker_id: str,
) -> ExactPaneProof:
    """Resolve Team/run + worker to an exact expected pane identity."""
    root_path = Path(root).resolve()
    wid = _require_worker_id(worker_id)
    run_id = resolve_team_ref(root_path, identity)
    meta = load_team_meta(root_path, run_id)
    if meta.get("writer") != CLI_WRITER:
        raise OperatorError(
            "team meta lacks CLI writer authority",
            code="E_OPERATOR_WRITER",
            exit_code=2,
        )
    if bool(meta.get("dry_run")):
        raise OperatorError(
            "dry_run team has no live panes",
            code="E_OPERATOR_DRY_RUN",
            exit_code=2,
            status=STATUS_GONE,
        )

    try:
        chain = _load_team_identity_chain(root_path, run_id, meta)
    except TeamError as exc:
        raise OperatorError(
            f"identity receipt chain refused: {exc}",
            code="E_OPERATOR_RECEIPT",
            exit_code=2,
        ) from exc
    launch = chain[0]
    session = str(meta.get("session") or launch.get("session_name") or "")
    session_id = str(meta.get("session_id") or launch.get("session_id") or "")
    launch_nonce = str(meta.get("launch_nonce") or launch.get("launch_nonce") or "")
    if not session or not session_id or not launch_nonce:
        raise OperatorError(
            "team identity missing session/session_id/launch_nonce",
            code="E_OPERATOR_IDENTITY",
            exit_code=2,
            status=STATUS_UNKNOWN,
        )

    generation = meta.get("identity_generation", 0)
    if (
        isinstance(generation, bool)
        or not isinstance(generation, int)
        or generation < 0
    ):
        raise OperatorError(
            "invalid identity_generation",
            code="E_OPERATOR_GENERATION",
            exit_code=2,
        )

    task: Mapping[str, Any] | None = None
    for raw in meta.get("tasks") or []:
        if not isinstance(raw, Mapping):
            continue
        if str(raw.get("task_id") or "") == wid:
            task = raw
            break
    if task is None:
        raise OperatorError(
            f"worker {wid!r} not found on team {run_id}",
            code="E_OPERATOR_WORKER_MISSING",
            exit_code=2,
        )

    pane_id = task.get("pane_id")
    if not isinstance(pane_id, str) or _TMUX_PANE_ID.fullmatch(pane_id) is None:
        raise OperatorError(
            f"worker {wid!r} has no exact pane id",
            code="E_OPERATOR_PANE_MISSING",
            exit_code=2,
            status=STATUS_GONE,
        )

    leader_pane_id = meta.get("leader_pane_id")
    if isinstance(leader_pane_id, str) and leader_pane_id == pane_id:
        raise OperatorError(
            "refused: target pane is the leader (not a worker)",
            code="E_OPERATOR_LEADER_REFUSED",
            exit_code=2,
            status=STATUS_MISMATCH,
        )

    pid = task.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise OperatorError(
            f"worker {wid!r} missing receipt pane pid",
            code="E_OPERATOR_PID",
            exit_code=2,
            status=STATUS_UNKNOWN,
        )

    attempt_raw = task.get("attempt", 1)
    attempt = int(attempt_raw) if isinstance(attempt_raw, int) and not isinstance(
        attempt_raw, bool
    ) else 1
    window_id = task.get("window_id")
    if window_id is not None and not isinstance(window_id, str):
        window_id = None
    if window_id is None:
        meta_window = meta.get("window_id")
        if isinstance(meta_window, str):
            window_id = meta_window

    pid_start = task.get("pid_start")
    if pid_start is not None and not isinstance(pid_start, str):
        pid_start = None

    team_id = str(meta.get("team_id") or "team")
    return ExactPaneProof(
        run_id=run_id,
        team_id=team_id,
        worker_id=wid,
        attempt=attempt,
        generation=generation,
        session=session,
        session_id=session_id,
        launch_nonce=launch_nonce,
        pane_id=pane_id,
        window_id=window_id,
        expected_pid=pid,
        expected_pid_start=pid_start,
        owner_token_sha256=_owner_token_sha256(meta),
        leader_pane_id=leader_pane_id if isinstance(leader_pane_id, str) else None,
        role=str(task["role"]) if task.get("role") else None,
        provider=str(task["provider"]) if task.get("provider") else None,
        posture=str(task["posture"]) if task.get("posture") else None,
        worktree=str(task["worktree"]) if task.get("worktree") else None,
        state=str(task["status"]) if task.get("status") else None,
        ready=None,
    )


def probe_exact_worker(proof: ExactPaneProof) -> str:
    """Run #98 exact-pane liveness; return live|gone|identity_mismatch|unknown."""
    if not tmux_available():
        return STATUS_UNKNOWN
    state = _worker_pane_liveness(
        pane_id=proof.pane_id,
        session=proof.session,
        expected_session_id=proof.session_id,
        launch_nonce=proof.launch_nonce,
        expected_pid_start=proof.expected_pid_start,
        expected_pid=proof.expected_pid,
    )
    return _liveness_status(state)


def authorize_capture(proof: ExactPaneProof, *, status: str | None = None) -> str:
    live = status if status is not None else probe_exact_worker(proof)
    if live != STATUS_LIVE:
        raise OperatorError(
            f"capture refused: pane status={live}",
            code="E_OPERATOR_CAPTURE_REFUSED",
            exit_code=2 if live == STATUS_MISMATCH else 1,
            status=live,
        )
    return live


def authorize_focus(proof: ExactPaneProof, *, status: str | None = None) -> str:
    live = status if status is not None else probe_exact_worker(proof)
    if live != STATUS_LIVE:
        raise OperatorError(
            f"focus refused: pane status={live}",
            code="E_OPERATOR_FOCUS_REFUSED",
            exit_code=2 if live == STATUS_MISMATCH else 1,
            status=live,
        )
    return live


def authorize_key(proof: ExactPaneProof, key: str, *, status: str | None = None) -> str:
    _require_key_name(key)
    live = status if status is not None else probe_exact_worker(proof)
    if live != STATUS_LIVE:
        raise OperatorError(
            f"key refused: pane status={live}",
            code="E_OPERATOR_KEY_REFUSED",
            exit_code=2 if live == STATUS_MISMATCH else 1,
            status=live,
        )
    return live


def authorize_input(
    proof: ExactPaneProof,
    text: str,
    *,
    status: str | None = None,
    operator_override: bool = False,
) -> str:
    _validate_literal_input(text)
    if not operator_override and not sys.stdin.isatty():
        raise OperatorError(
            "pane input requires a TTY or --operator-override "
            "(prefer omg team api send-message for automation)",
            code="E_OPERATOR_INPUT_POLICY",
            exit_code=2,
        )
    live = status if status is not None else probe_exact_worker(proof)
    if live != STATUS_LIVE:
        raise OperatorError(
            f"input refused: pane status={live}",
            code="E_OPERATOR_INPUT_REFUSED",
            exit_code=2 if live == STATUS_MISMATCH else 1,
            status=live,
        )
    return live


def audit_operator_event(
    root: Path | str,
    proof: ExactPaneProof,
    *,
    action: str,
    ok: bool,
    key_name: str | None = None,
    text_length: int | None = None,
    text_sha256: str | None = None,
    status: str | None = None,
    error_code: str | None = None,
) -> dict[str, Any]:
    """Append a bounded operator audit row (never raw input text)."""
    root_path = Path(root).resolve()
    path = team_dir(root_path, proof.run_id) / "operator-audit.jsonl"
    ensure_managed_dir(path.parent)
    event = {
        "event_id": f"op-{secrets.token_hex(8)}",
        "ts": _utc_now_iso(),
        "actor": "operator_cli",
        "action": action,
        "ok": bool(ok),
        "run_id": proof.run_id,
        "team_id": proof.team_id,
        "worker_id": proof.worker_id,
        "attempt": proof.attempt,
        "generation": proof.generation,
        "pane_id": proof.pane_id,
        "session_id": proof.session_id,
        "identity_sha256": _sha256_text(
            f"{proof.session_id}|{proof.launch_nonce}|{proof.pane_id}|"
            f"{proof.expected_pid}|{proof.expected_pid_start or ''}"
        ),
        "key_name": key_name,
        "text_length": text_length,
        "text_sha256": text_sha256,
        "status": status,
        "error_code": error_code,
    }
    line = json.dumps(event, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    line += "\n"
    with exclusive_lock(path.with_suffix(".lock")):
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()
            try:
                os.fchmod(fh.fileno(), DATA_FILE_MODE)
            except OSError:
                pass
    return event


def list_panes(
    root: Path | str,
    identity: str | None = None,
    *,
    probe: bool = True,
) -> dict[str, Any]:
    """Return bounded worker pane descriptors with authorization flags."""
    root_path = Path(root).resolve()
    run_id = resolve_team_ref(root_path, identity)
    rows = worker_pane_descriptors(root_path, run_id, probe=probe)
    return {
        "run_id": run_id,
        "command": "team.panes",
        "panes": rows,
        "count": len(rows),
    }


def capture_worker(
    root: Path | str,
    identity: str | None,
    worker_id: str,
    *,
    lines: int | None = None,
    raw: bool = False,
) -> dict[str, Any]:
    proof = resolve_live_worker(root, identity, worker_id)
    status = probe_exact_worker(proof)
    if status != STATUS_LIVE:
        return {
            "ok": False,
            "command": "team.capture",
            "status": status,
            "run_id": proof.run_id,
            "worker_id": proof.worker_id,
            "attempt": proof.attempt,
            "generation": proof.generation,
            "pane_id": proof.pane_id,
            "text": None,
            "bytes": 0,
            "lines_requested": _bound_lines(lines),
            "raw": bool(raw),
        }
    authorize_capture(proof, status=status)
    # TOCTOU: re-probe immediately before capture.
    status2 = probe_exact_worker(proof)
    if status2 != STATUS_LIVE:
        raise OperatorError(
            f"capture TOCTOU: pane status={status2}",
            code="E_OPERATOR_TOCTOU",
            exit_code=2 if status2 == STATUS_MISMATCH else 1,
            status=status2,
        )
    bound = _bound_lines(lines)
    try:
        text = capture_pane(proof.pane_id, lines=bound, raw=raw)
    except TmuxTeamError as exc:
        raise OperatorError(
            str(exc),
            code="E_OPERATOR_TMUX",
            exit_code=1,
            status=STATUS_UNKNOWN,
        ) from exc
    if not raw:
        text = _strip_ansi(text)
    text = redact_text(text)
    encoded = text.encode("utf-8")
    if len(encoded) > MAX_OPERATOR_CAPTURE_BYTES:
        text = encoded[:MAX_OPERATOR_CAPTURE_BYTES].decode("utf-8", errors="ignore")
        encoded = text.encode("utf-8")
    return {
        "ok": True,
        "command": "team.capture",
        "status": STATUS_LIVE,
        "run_id": proof.run_id,
        "worker_id": proof.worker_id,
        "attempt": proof.attempt,
        "generation": proof.generation,
        "pane_id": proof.pane_id,
        "text": text,
        "bytes": len(encoded),
        "lines_requested": bound,
        "raw": bool(raw),
    }


def focus_worker(
    root: Path | str,
    identity: str | None,
    worker_id: str,
    *,
    as_json: bool = False,
    execute: bool = False,
    is_tty: bool | None = None,
) -> dict[str, Any]:
    proof = resolve_live_worker(root, identity, worker_id)
    status = probe_exact_worker(proof)
    authorize_focus(proof, status=status)
    tty = sys.stdin.isatty() if is_tty is None else bool(is_tty)
    inside_tmux = bool(os.environ.get("TMUX"))
    attach_argv = attach_argv_for_target(
        session=proof.session,
        pane_id=proof.pane_id,
        window_id=proof.window_id,
    )
    result: dict[str, Any] = {
        "ok": True,
        "command": "team.focus",
        "status": STATUS_LIVE,
        "run_id": proof.run_id,
        "worker_id": proof.worker_id,
        "attempt": proof.attempt,
        "generation": proof.generation,
        "pane_id": proof.pane_id,
        "focused": False,
        "attach_argv": attach_argv,
        "attach_hint": " ".join(attach_argv),
        "team_state_mutated": False,
    }
    if as_json:
        # --json never changes focus.
        audit_operator_event(
            root,
            proof,
            action="focus",
            ok=True,
            status=STATUS_LIVE,
        )
        return result

    status2 = probe_exact_worker(proof)
    if status2 != STATUS_LIVE:
        raise OperatorError(
            f"focus TOCTOU: pane status={status2}",
            code="E_OPERATOR_TOCTOU",
            exit_code=2 if status2 == STATUS_MISMATCH else 1,
            status=status2,
        )

    try:
        if inside_tmux and tty:
            focus_pane(proof.pane_id)
            result["focused"] = True
            result["mode"] = "select-pane"
        elif execute and tty:
            # Explicit attach path outside tmux.
            import subprocess

            proc = subprocess.run(
                attach_argv,
                check=False,
                shell=False,
            )
            result["focused"] = proc.returncode == 0
            result["mode"] = "attach"
            result["attach_exit"] = proc.returncode
            if proc.returncode != 0:
                raise OperatorError(
                    f"attach failed exit={proc.returncode}",
                    code="E_OPERATOR_ATTACH",
                    exit_code=1,
                    status=STATUS_LIVE,
                )
        else:
            result["mode"] = "hint"
            result["note"] = (
                "printed attach command only "
                "(use interactive TTY inside tmux, or --execute to attach)"
            )
    except TmuxTeamError as exc:
        audit_operator_event(
            root,
            proof,
            action="focus",
            ok=False,
            status=status2,
            error_code="E_OPERATOR_TMUX",
        )
        raise OperatorError(
            str(exc),
            code="E_OPERATOR_TMUX",
            exit_code=1,
            status=STATUS_UNKNOWN,
        ) from exc

    audit_operator_event(
        root,
        proof,
        action="focus",
        ok=True,
        status=STATUS_LIVE,
    )
    return result


def key_worker(
    root: Path | str,
    identity: str | None,
    worker_id: str,
    key: str,
    *,
    as_json: bool = False,
) -> dict[str, Any]:
    if as_json:
        raise OperatorError(
            "--json never delivers keys",
            code="E_OPERATOR_JSON_NOOP",
            exit_code=2,
        )
    proof = resolve_live_worker(root, identity, worker_id)
    key_name = _require_key_name(key)
    status = probe_exact_worker(proof)
    authorize_key(proof, key_name, status=status)
    status2 = probe_exact_worker(proof)
    if status2 != STATUS_LIVE:
        audit_operator_event(
            root,
            proof,
            action="key",
            ok=False,
            key_name=key_name,
            status=status2,
            error_code="E_OPERATOR_TOCTOU",
        )
        raise OperatorError(
            f"key TOCTOU: pane status={status2}",
            code="E_OPERATOR_TOCTOU",
            exit_code=2 if status2 == STATUS_MISMATCH else 1,
            status=status2,
        )
    try:
        send_key(proof.pane_id, key_name)
    except TmuxTeamError as exc:
        audit_operator_event(
            root,
            proof,
            action="key",
            ok=False,
            key_name=key_name,
            status=STATUS_LIVE,
            error_code="E_OPERATOR_TMUX",
        )
        raise OperatorError(str(exc), code="E_OPERATOR_TMUX", exit_code=1) from exc
    audit_operator_event(
        root,
        proof,
        action="key",
        ok=True,
        key_name=key_name,
        status=STATUS_LIVE,
    )
    return {
        "ok": True,
        "command": "team.key",
        "status": STATUS_LIVE,
        "run_id": proof.run_id,
        "worker_id": proof.worker_id,
        "attempt": proof.attempt,
        "generation": proof.generation,
        "pane_id": proof.pane_id,
        "key": key_name,
        "delivered": True,
    }


def input_worker(
    root: Path | str,
    identity: str | None,
    worker_id: str,
    text: str,
    *,
    submit: bool = False,
    as_json: bool = False,
    operator_override: bool = False,
    is_tty: bool | None = None,
) -> dict[str, Any]:
    if as_json:
        raise OperatorError(
            "--json never sends input",
            code="E_OPERATOR_JSON_NOOP",
            exit_code=2,
        )
    proof = resolve_live_worker(root, identity, worker_id)
    safe = _validate_literal_input(text)
    tty = sys.stdin.isatty() if is_tty is None else bool(is_tty)
    # Mirror authorize_input TTY policy with injectable is_tty for tests.
    if not operator_override and not tty:
        raise OperatorError(
            "pane input requires a TTY or --operator-override "
            "(prefer omg team api send-message for automation)",
            code="E_OPERATOR_INPUT_POLICY",
            exit_code=2,
        )
    status = probe_exact_worker(proof)
    authorize_input(
        proof,
        safe,
        status=status,
        operator_override=True if tty or operator_override else False,
    )
    status2 = probe_exact_worker(proof)
    text_len = len(safe.encode("utf-8"))
    text_hash = _sha256_text(safe)
    if status2 != STATUS_LIVE:
        audit_operator_event(
            root,
            proof,
            action="input",
            ok=False,
            text_length=text_len,
            text_sha256=text_hash,
            status=status2,
            error_code="E_OPERATOR_TOCTOU",
        )
        raise OperatorError(
            f"input TOCTOU: pane status={status2}",
            code="E_OPERATOR_TOCTOU",
            exit_code=2 if status2 == STATUS_MISMATCH else 1,
            status=status2,
        )
    try:
        send_literal(proof.pane_id, safe)
        if submit:
            send_key(proof.pane_id, "Enter")
    except TmuxTeamError as exc:
        audit_operator_event(
            root,
            proof,
            action="input",
            ok=False,
            text_length=text_len,
            text_sha256=text_hash,
            status=STATUS_LIVE,
            error_code="E_OPERATOR_TMUX",
        )
        raise OperatorError(str(exc), code="E_OPERATOR_TMUX", exit_code=1) from exc
    audit_operator_event(
        root,
        proof,
        action="input",
        ok=True,
        text_length=text_len,
        text_sha256=text_hash,
        status=STATUS_LIVE,
    )
    return {
        "ok": True,
        "command": "team.input",
        "status": STATUS_LIVE,
        "run_id": proof.run_id,
        "worker_id": proof.worker_id,
        "attempt": proof.attempt,
        "generation": proof.generation,
        "pane_id": proof.pane_id,
        "delivered": True,
        "submitted": bool(submit),
        "text_length": text_len,
        "text_sha256": text_hash,
    }


def watch_worker(
    root: Path | str,
    identity: str | None,
    worker_id: str | None = None,
    *,
    interval_s: float = DEFAULT_WATCH_INTERVAL_S,
    lines: int | None = None,
    as_json: bool = False,
    max_iterations: int = MAX_WATCH_ITERATIONS,
    sleep_fn: Callable[[float], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Pure observation loop — never mutates Team state or sends input."""
    if interval_s < MIN_WATCH_INTERVAL_S or interval_s > MAX_WATCH_INTERVAL_S:
        raise OperatorError(
            f"watch --interval must be in "
            f"[{MIN_WATCH_INTERVAL_S}, {MAX_WATCH_INTERVAL_S}] seconds",
            code="E_OPERATOR_INTERVAL",
            exit_code=2,
        )
    if (
        isinstance(max_iterations, bool)
        or not isinstance(max_iterations, int)
        or max_iterations < 1
    ):
        raise OperatorError(
            "watch max_iterations must be a positive int",
            code="E_OPERATOR_WATCH",
            exit_code=2,
        )
    sleeper = sleep_fn or time.sleep
    root_path = Path(root).resolve()
    events: list[dict[str, Any]] = []
    last_text: str | None = None
    last_identity: tuple[str, int, int] | None = None
    iterations = 0
    stop_reason = "max_iterations"

    targets: Sequence[str]
    if worker_id:
        targets = [_require_worker_id(worker_id)]
    else:
        listing = list_panes(root_path, identity, probe=False)
        targets = [
            str(row["worker_id"])
            for row in listing.get("panes") or []
            if isinstance(row, Mapping) and row.get("worker_id")
        ]
        if not targets:
            raise OperatorError(
                "no workers to watch",
                code="E_OPERATOR_WATCH_EMPTY",
                exit_code=2,
            )

    while iterations < max_iterations:
        if should_stop is not None and should_stop():
            stop_reason = "cancelled"
            break
        iterations += 1
        for wid in targets:
            try:
                proof = resolve_live_worker(root_path, identity, wid)
            except OperatorError as exc:
                event = {
                    "ts": _utc_now_iso(),
                    "worker_id": wid,
                    "status": exc.status or STATUS_UNKNOWN,
                    "error": exc.code,
                }
                events.append(event)
                if as_json:
                    print(json.dumps(event, ensure_ascii=False), flush=True)
                stop_reason = "identity_lost"
                return {
                    "ok": False,
                    "command": "team.watch",
                    "iterations": iterations,
                    "stop_reason": stop_reason,
                    "events": events if not as_json else None,
                    "event_count": len(events),
                }
            status = probe_exact_worker(proof)
            identity_key = (proof.pane_id, proof.attempt, proof.generation)
            if last_identity is not None and identity_key != last_identity:
                stop_reason = "identity_changed"
                event = {
                    "ts": _utc_now_iso(),
                    "worker_id": wid,
                    "status": STATUS_MISMATCH,
                    "error": "identity_changed",
                    "pane_id": proof.pane_id,
                    "attempt": proof.attempt,
                    "generation": proof.generation,
                }
                events.append(event)
                if as_json:
                    print(json.dumps(event, ensure_ascii=False), flush=True)
                return {
                    "ok": False,
                    "command": "team.watch",
                    "iterations": iterations,
                    "stop_reason": stop_reason,
                    "events": events if not as_json else None,
                    "event_count": len(events),
                }
            last_identity = identity_key
            if status != STATUS_LIVE:
                event = {
                    "ts": _utc_now_iso(),
                    "worker_id": wid,
                    "status": status,
                    "pane_id": proof.pane_id,
                    "attempt": proof.attempt,
                    "generation": proof.generation,
                }
                events.append(event)
                if as_json:
                    print(json.dumps(event, ensure_ascii=False), flush=True)
                stop_reason = "pane_" + status
                return {
                    "ok": False,
                    "command": "team.watch",
                    "iterations": iterations,
                    "stop_reason": stop_reason,
                    "events": events if not as_json else None,
                    "event_count": len(events),
                }
            snap = capture_worker(
                root_path,
                identity,
                wid,
                lines=lines,
                raw=False,
            )
            text = str(snap.get("text") or "")
            changed = text != last_text
            last_text = text
            event = {
                "ts": _utc_now_iso(),
                "worker_id": wid,
                "status": STATUS_LIVE,
                "pane_id": proof.pane_id,
                "attempt": proof.attempt,
                "generation": proof.generation,
                "changed": changed,
                "bytes": snap.get("bytes"),
            }
            if changed:
                event["text"] = text
            events.append(event)
            if as_json:
                print(json.dumps(event, ensure_ascii=False), flush=True)
            elif changed:
                sys.stdout.write(
                    f"\n--- {wid} gen={proof.generation} attempt={proof.attempt} ---\n"
                )
                sys.stdout.write(text)
                if not text.endswith("\n"):
                    sys.stdout.write("\n")
                sys.stdout.flush()
        sleeper(float(interval_s))

    return {
        "ok": True,
        "command": "team.watch",
        "iterations": iterations,
        "stop_reason": stop_reason,
        "events": events if not as_json else None,
        "event_count": len(events),
    }


__all__ = [
    "ALLOWED_OPERATOR_KEYS",
    "DEFAULT_CAPTURE_LINES",
    "ExactPaneProof",
    "MAX_CAPTURE_LINES",
    "OperatorError",
    "STATUS_GONE",
    "STATUS_LIVE",
    "STATUS_MISMATCH",
    "STATUS_UNKNOWN",
    "audit_operator_event",
    "authorize_capture",
    "authorize_focus",
    "authorize_input",
    "authorize_key",
    "capture_worker",
    "focus_worker",
    "input_worker",
    "key_worker",
    "list_panes",
    "probe_exact_worker",
    "resolve_live_worker",
    "watch_worker",
]
