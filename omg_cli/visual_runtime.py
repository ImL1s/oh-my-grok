"""Provider-neutral visual capture / verdict / Ralph runtime (#75).

Honesty constraints:
- Never decode image pixels (no PIL/Pillow, no Playwright import).
- Never emit ``approved`` / ``passes`` / ``verified``.
- Never inline image bytes or base64 in JSON; descriptors only.
- Overlay sidecars are descriptor-only (masks + byte-identity).
- Missing capture is ``blocked``, not a fake pass.
- Visual scores are evidence only; this module does not write ``.omg/state``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

from omg_cli.contracts.visual_contract import (
    COMPARISON_KIND,
    DIMENSION_IDS,
    MAX_IMAGE_BYTES,
    SCHEMA_VERSION as VISUAL_SCHEMA,
    VisualContractError,
    compare,
    validate_image_descriptor,
)

SCHEMA_VERSION = 1
CAPTURE_RESULT_KIND = "omg.visual.capture_result"
VERDICT_RESULT_KIND = "omg.visual.verdict_result"
RALPH_RESULT_KIND = "omg.visual.ralph_result"
RUN_MANIFEST_KIND = "omg.visual.run"
OVERLAY_KIND = "omg.visual.overlay"
REPAIR_KIND = "omg.visual.repair_prompt"
FINDINGS_KIND = "omg.visual.findings"
ENV_CAPTURE = "OMG_VISUAL_CAPTURE"
ENV_OUTPUT = "OMG_VISUAL_OUTPUT"
ENV_FAKE_SOURCE = "OMG_VISUAL_FAKE_SOURCE"
ARTIFACT_DIR = ".omg/artifacts/visual"
MAX_CONFIG_BYTES = 256 * 1024
CAPTURE_TIMEOUT_SEC = 60
RUN_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
FORBIDDEN_KEYS = frozenset({"approved", "passes", "verified"})
SUFFIX_MEDIA = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}
BUILTIN_ROLE_CAPABILITY = {
    "omg-vision": "read-only",
    "omg-critic": "read-only",
    "omg-verifier": "read-only",
    "omg-analyst": "read-only",
    "omg-architect": "read-only",
    "omg-code-reviewer": "read-only",
    "omg-security-reviewer": "read-only",
    "omg-designer": "read-write",
    "omg-executor": "read-write",
    "omg-debugger": "read-write",
    "omg-writer": "read-write",
    "omg-test-engineer": "read-write",
    "omg-qa-tester": "read-write",
    "omg-orchestrator": "read-write",
}
DEFAULT_EDITOR_ROLE = "omg-designer"
DEFAULT_REVIEWER_ROLE = "omg-vision"
OVERLAY_NOTE = (
    "overlay is descriptor-only; pixel diffs are not computed "
    "(no image decoder / no vision model)"
)


class VisualRuntimeError(ValueError):
    """Visual runtime failed before producing a scored/blocked artifact."""

    code = "E_VISUAL_RUNTIME"


class VisualPathError(VisualRuntimeError):
    code = "E_VISUAL_PATH"


class VisualConfigError(VisualRuntimeError):
    code = "E_VISUAL_INPUT"


class VisualReviewerError(VisualRuntimeError):
    code = "E_VISUAL_REVIEWER"


class VisualMetadataError(VisualRuntimeError):
    code = "E_VISUAL_METADATA"


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dt%H%M%Sz")
    return f"vis-{stamp}-{uuid.uuid4().hex[:8]}"


def validate_run_id(value: str) -> str:
    if not isinstance(value, str) or not RUN_ID_RE.fullmatch(value):
        raise VisualPathError("run_id must be a lowercase [a-z0-9-] token")
    if ".." in value:
        raise VisualPathError("run_id must not traverse")
    return value


def confine_workspace_path(root: Path, candidate: str | Path) -> Path:
    """Windows-safe workspace confinement (resolve + relative_to)."""
    if candidate is None or str(candidate) == "":
        raise VisualPathError("path is required")
    text = str(candidate)
    if "\x00" in text:
        raise VisualPathError("path contains NUL")
    lowered = text.replace("\\", "/").split(":", 1)[0].lower()
    if "://" in text.replace("\\", "/") or lowered in {"file", "http", "https"}:
        raise VisualPathError("path must not be a URI")
    base = Path(root).resolve()
    path = Path(text)
    if not path.is_absolute():
        path = base / path
    try:
        resolved = path.resolve()
        resolved.relative_to(base)
    except (OSError, ValueError) as exc:
        raise VisualPathError("path escapes workspace") from exc
    return resolved


def posix_relpath(root: Path, path: Path) -> str:
    rel = path.resolve().relative_to(Path(root).resolve())
    text = rel.as_posix()
    if text.startswith("/") or ".." in PurePosixPath(text).parts:
        raise VisualPathError("path must be workspace-relative")
    return text


def media_type_for(path: Path, declared: str | None = None) -> str:
    if declared:
        return declared
    return SUFFIX_MEDIA.get(path.suffix.lower(), "image/png")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def image_descriptor(
    *,
    root: Path,
    path: Path,
    width: int,
    height: int,
    media_type: str | None = None,
) -> dict[str, Any]:
    if not path.is_file():
        raise VisualPathError("image path is not a regular file")
    size = path.stat().st_size
    if size < 1:
        raise VisualMetadataError("image byte_size must be >= 1")
    if size > MAX_IMAGE_BYTES:
        raise VisualMetadataError("image exceeds Visual Contract V1 size limit")
    descriptor = {
        "path": posix_relpath(root, path),
        "sha256": file_sha256(path),
        "media_type": media_type_for(path, media_type),
        "byte_size": int(size),
        "width": int(width),
        "height": int(height),
    }
    return validate_image_descriptor(descriptor)


def load_role_capabilities(*, plugin_root: Path | None = None) -> dict[str, str]:
    roles = dict(BUILTIN_ROLE_CAPABILITY)
    root = plugin_root
    if root is None:
        root = Path(__file__).resolve().parents[1]
    catalog = root / "agents" / "catalog.json"
    try:
        payload = json.loads(catalog.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        payload = {}
    agents = payload.get("agents") if isinstance(payload, dict) else None
    if isinstance(agents, list):
        for row in agents:
            if not isinstance(row, dict):
                continue
            agent_id = row.get("id")
            mode = row.get("capability_mode")
            if isinstance(agent_id, str) and mode in {"read-only", "read-write"}:
                roles[agent_id] = mode
    roles["omg-vision"] = "read-only"
    return roles


def role_capability(role: str, *, roles: Mapping[str, str] | None = None) -> str:
    table = roles if roles is not None else load_role_capabilities()
    if role not in table:
        raise VisualReviewerError(f"unknown visual role {role!r}")
    return table[role]


def enforce_independent_reviewer(
    *,
    editor_role: str,
    reviewer_role: str,
    reviewer_capability: str | None = None,
    roles: Mapping[str, str] | None = None,
) -> dict[str, str]:
    if not isinstance(editor_role, str) or not editor_role.strip():
        raise VisualReviewerError("editor_role is required")
    if not isinstance(reviewer_role, str) or not reviewer_role.strip():
        raise VisualReviewerError("reviewer_role is required")
    editor = editor_role.strip()
    reviewer = reviewer_role.strip()
    if editor == reviewer:
        raise VisualReviewerError(
            "editor_role and reviewer_role must be independent"
        )
    catalog_capability = role_capability(reviewer, roles=roles)
    if reviewer_capability is not None and reviewer_capability != catalog_capability:
        raise VisualReviewerError(
            "reviewer_capability_mode cannot override the catalog role floor"
        )
    capability = catalog_capability
    if capability != "read-only":
        raise VisualReviewerError(
            "reviewer_role must be read-only (omg-vision or a read-only reviewer)"
        )
    editor_cap = role_capability(editor, roles=roles)
    return {
        "editor_role": editor,
        "editor_capability": editor_cap,
        "reviewer_role": reviewer,
        "reviewer_capability": capability,
    }


def percent_to_score(percent: int) -> int:
    if isinstance(percent, bool) or not isinstance(percent, int):
        raise VisualConfigError("threshold must be an integer percent 0..100")
    if percent < 0 or percent > 100:
        raise VisualConfigError("threshold must be an integer percent 0..100")
    return percent * 100


def resolve_threshold(config: Mapping[str, Any], cli_percent: int | None) -> int:
    if cli_percent is not None:
        return percent_to_score(cli_percent)
    if "threshold_score" in config:
        value = config["threshold_score"]
        if isinstance(value, bool) or not isinstance(value, int):
            raise VisualConfigError("threshold_score must be an integer 0..10000")
        if value < 0 or value > 10000:
            raise VisualConfigError("threshold_score must be an integer 0..10000")
        return value
    if "threshold" in config:
        return percent_to_score(int(config["threshold"]))
    return percent_to_score(90)


def parse_simple_yaml(text: str) -> Any:
    """Restricted YAML subset: mappings, lists, scalars. No tags/anchors."""
    rows: list[tuple[int, int, str]] = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        if "\t" in raw:
            raise VisualConfigError("visual YAML must not contain tabs")
        cut = []
        in_str = False
        quote = ""
        for ch in raw:
            if in_str:
                cut.append(ch)
                if ch == quote:
                    in_str = False
                continue
            if ch in {'"', "'"}:
                in_str = True
                quote = ch
                cut.append(ch)
                continue
            if ch == "#":
                break
            cut.append(ch)
        stripped = "".join(cut).rstrip()
        if not stripped.strip():
            continue
        indent = len(stripped) - len(stripped.lstrip(" "))
        rows.append((lineno, indent, stripped.strip()))
    if not rows:
        raise VisualConfigError("visual config is empty")
    value, index = _yaml_parse(rows, 0, rows[0][1])
    if index != len(rows):
        raise VisualConfigError("visual YAML has unused nested content")
    return value


def _yaml_parse(rows: list[tuple[int, int, str]], index: int, indent: int) -> tuple[Any, int]:
    if index >= len(rows):
        raise VisualConfigError("visual YAML ended early")
    _lineno, row_indent, content = rows[index]
    if row_indent != indent:
        raise VisualConfigError("visual YAML indent mismatch")
    if content.startswith("- "):
        return _yaml_list(rows, index, indent)
    return _yaml_mapping(rows, index, indent)


def _yaml_mapping(
    rows: list[tuple[int, int, str]], index: int, indent: int
) -> tuple[dict[str, Any], int]:
    out: dict[str, Any] = {}
    while index < len(rows):
        _lineno, row_indent, content = rows[index]
        if row_indent < indent:
            break
        if row_indent > indent:
            raise VisualConfigError("visual YAML indent mismatch")
        if content.startswith("- "):
            raise VisualConfigError("visual YAML list item in mapping")
        key, sep, rest = content.partition(":")
        if not sep or not key.strip():
            raise VisualConfigError("visual YAML mapping line must be key: value")
        key = key.strip()
        rest = rest.strip()
        index += 1
        if rest == "":
            if index < len(rows) and rows[index][1] > indent:
                child, index = _yaml_parse(rows, index, rows[index][1])
                out[key] = child
            else:
                out[key] = None
            continue
        out[key] = _yaml_scalar(rest)
    return out, index


def _yaml_list(
    rows: list[tuple[int, int, str]], index: int, indent: int
) -> tuple[list[Any], int]:
    out: list[Any] = []
    while index < len(rows):
        _lineno, row_indent, content = rows[index]
        if row_indent < indent:
            break
        if row_indent > indent or not content.startswith("- "):
            raise VisualConfigError("visual YAML list indent mismatch")
        rest = content[2:].strip()
        index += 1
        if rest == "":
            if index < len(rows) and rows[index][1] > indent:
                child, index = _yaml_parse(rows, index, rows[index][1])
                out.append(child)
            else:
                out.append(None)
            continue
        if ":" in rest and not rest.startswith(("{", "[", '"', "'")):
            key, _sep, value = rest.partition(":")
            item = {key.strip(): _yaml_scalar(value.strip()) if value.strip() else None}
            while index < len(rows) and rows[index][1] > indent:
                nested, index = _yaml_mapping(rows, index, rows[index][1])
                item.update(nested)
            out.append(item)
            continue
        out.append(_yaml_scalar(rest))
    return out, index


def _yaml_scalar(text: str) -> Any:
    if text in {"null", "~", ""}:
        return None
    if text in {"true", "True", "yes"}:
        return True
    if text in {"false", "False", "no"}:
        return False
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        return text[1:-1]
    if text.startswith("[") and text.endswith("]"):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise VisualConfigError("visual YAML flow list is not JSON") from exc
        return parsed
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    return text


def load_visual_config(path: Path) -> dict[str, Any]:
    try:
        body = path.read_bytes()
    except OSError as exc:
        raise VisualConfigError("visual config is not readable") from exc
    if len(body) > MAX_CONFIG_BYTES:
        raise VisualConfigError("visual config exceeds size limit")
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise VisualConfigError("visual config is not UTF-8") from exc
    stripped = text.lstrip()
    try:
        if stripped.startswith("{") or stripped.startswith("["):
            data = json.loads(text)
        else:
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                data = parse_simple_yaml(text)
    except json.JSONDecodeError as exc:
        raise VisualConfigError("visual config is not readable JSON/YAML") from exc
    if not isinstance(data, dict):
        raise VisualConfigError("visual config must be a mapping")
    return data


def argv_from_env(raw: str) -> list[str]:
    text = raw.strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise VisualConfigError("OMG_VISUAL_CAPTURE must be a JSON argv array") from exc
        if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
            raise VisualConfigError("OMG_VISUAL_CAPTURE must be a JSON argv string array")
        return [str(item) for item in parsed]
    return shlex.split(text, posix=os.name != "nt")


def diagnose_capture_source(
    config: Mapping[str, Any] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    config_path: str | Path | None = None,
) -> dict[str, Any]:
    """Precedence: (1) config capture.command (2) OMG_VISUAL_CAPTURE (3) none."""
    environ = env if env is not None else os.environ
    loaded = dict(config or {})
    if config_path is not None and not loaded:
        path = Path(config_path)
        if path.is_file():
            try:
                loaded = load_visual_config(path)
            except VisualRuntimeError as exc:
                return {
                    "source": "none",
                    "command": None,
                    "status": "blocked",
                    "block_code": "capture_config_unreadable",
                    "detail": str(exc),
                    "playwright_required": False,
                }
    command: list[str] | None = None
    source = "none"
    capture = loaded.get("capture") if isinstance(loaded.get("capture"), dict) else {}
    raw_cmd = capture.get("command") if isinstance(capture, dict) else None
    if isinstance(raw_cmd, list) and raw_cmd and all(isinstance(item, str) for item in raw_cmd):
        command = [str(item) for item in raw_cmd]
        source = "config"
    else:
        env_raw = str(environ.get(ENV_CAPTURE, "") or "").strip()
        if env_raw:
            try:
                command = argv_from_env(env_raw)
            except VisualConfigError as exc:
                return {
                    "source": "none",
                    "command": None,
                    "status": "blocked",
                    "block_code": "capture_env_invalid",
                    "detail": str(exc),
                    "playwright_required": False,
                }
            if command:
                source = "env"
    if source == "none" or not command:
        return {
            "source": "none",
            "command": None,
            "status": "blocked",
            "block_code": "capture_unavailable",
            "detail": "none (blocked; not a fake pass); set capture.command or OMG_VISUAL_CAPTURE",
            "playwright_required": False,
        }
    return {
        "source": source,
        "command": command,
        "status": "ready",
        "block_code": None,
        "detail": f"source={source} (playwright not required)",
        "playwright_required": False,
        "tool": Path(command[0]).name,
        "target": capture.get("target") if isinstance(capture, dict) else None,
        "readiness": capture.get("readiness") if isinstance(capture, dict) else loaded.get("readiness"),
    }


def discover_visual_config_path(root: Path | None) -> Path | None:
    names = ("visual.yaml", "visual.yml", "visual.json")
    candidates: list[Path] = []
    if root is not None:
        for name in names:
            candidates.append(Path(root) / name)
            candidates.append(Path(root) / ".omg" / name)
    cwd = Path.cwd()
    for name in names:
        candidates.append(cwd / name)
    seen: set[Path] = set()
    for path in candidates:
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        if path.is_file():
            return path
    return None


def compatibility_from_config(config: Mapping[str, Any]) -> dict[str, Any]:
    block = config.get("compatibility")
    if not isinstance(block, dict):
        block = {}
    keys = (
        "viewport_width",
        "viewport_height",
        "dpr_milli",
        "platform",
        "theme",
        "locale",
    )
    out: dict[str, Any] = {}
    for key in keys:
        if key in block:
            out[key] = block[key]
        elif key in config:
            out[key] = config[key]
        else:
            out[key] = None
    return out


def default_dimensions(score: int = 10000) -> list[dict[str, int]]:
    return [
        {"id": dim_id, "score": score, "weight": 1000} for dim_id in DIMENSION_IDS
    ]


def artifact_dir(root: Path, run_id: str) -> Path:
    run_id = validate_run_id(run_id)
    path = confine_workspace_path(root, f"{ARTIFACT_DIR}/{run_id}")
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_json(path: Path, payload: Mapping[str, Any]) -> None:
    _assert_honest(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(dict(payload), indent=2, ensure_ascii=False, sort_keys=True)
    path.write_text(text + "\n", encoding="utf-8")


def _assert_honest(payload: Any) -> None:
    if isinstance(payload, Mapping):
        keys = set(payload)
        if FORBIDDEN_KEYS.intersection(keys):
            raise VisualRuntimeError("result must not contain approved/passes/verified")
        encoded = json.dumps(payload, ensure_ascii=False)
        if "iVBOR" in encoded or "base64" in encoded:
            raise VisualRuntimeError("result must not inline image bytes")
        for value in payload.values():
            _assert_honest(value)
        return
    if isinstance(payload, (list, tuple)):
        for item in payload:
            _assert_honest(item)


def copy_image(root: Path, source: Path, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, dest)
    return confine_workspace_path(root, dest)


def overlay_sidecar(
    *,
    masks: Sequence[Mapping[str, int]],
    reference: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> dict[str, Any]:
    byte_identity = (
        reference.get("sha256") == candidate.get("sha256")
        and reference.get("byte_size") == candidate.get("byte_size")
    )
    payload = {
        "schema_version": SCHEMA_VERSION,
        "kind": OVERLAY_KIND,
        "mode": "descriptor_only",
        "pixel_decode": False,
        "byte_identity": bool(byte_identity),
        "masks": list(masks),
        "note": OVERLAY_NOTE,
    }
    _assert_honest(payload)
    return payload


def _image_spec(config: Mapping[str, Any], key: str) -> dict[str, Any]:
    raw = config.get(key)
    if isinstance(raw, dict):
        return dict(raw)
    return {}


def _require_hw(spec: Mapping[str, Any], config: Mapping[str, Any], *, width: int | None, height: int | None) -> tuple[int, int]:
    w = width if width is not None else spec.get("width", config.get("width"))
    h = height if height is not None else spec.get("height", config.get("height"))
    if isinstance(w, bool) or not isinstance(w, int) or isinstance(h, bool) or not isinstance(h, int):
        raise VisualMetadataError(
            "width/height must be declared in config or flags (images are not decoded)"
        )
    if w <= 0 or h <= 0:
        raise VisualMetadataError("width/height must be positive integers")
    return w, h


def execute_capture_command(
    argv: Sequence[str],
    *,
    root: Path,
    output: Path,
    env: Mapping[str, str] | None = None,
    extra_env: Mapping[str, str] | None = None,
    timeout: int = CAPTURE_TIMEOUT_SEC,
) -> dict[str, Any]:
    if not argv:
        return {
            "exit_code": None,
            "error": "capture command is empty",
            "status": "blocked",
        }
    merged = dict(env if env is not None else os.environ)
    if extra_env:
        merged.update(extra_env)
    merged[ENV_OUTPUT] = str(output)
    try:
        proc = subprocess.run(
            list(argv),
            cwd=str(root),
            env=merged,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            shell=False,
        )
    except FileNotFoundError as exc:
        return {
            "exit_code": 127,
            "error": f"capture executable not found: {exc}",
            "status": "blocked",
        }
    except subprocess.TimeoutExpired:
        return {
            "exit_code": 124,
            "error": "capture timed out",
            "status": "blocked",
        }
    except OSError as exc:
        return {
            "exit_code": 1,
            "error": f"capture failed to start: {exc}",
            "status": "blocked",
        }
    err = (proc.stderr or "").strip()
    if proc.returncode != 0:
        return {
            "exit_code": int(proc.returncode),
            "error": err or f"capture exited {proc.returncode}",
            "status": "blocked",
        }
    return {"exit_code": 0, "error": None, "status": "captured"}


def run_capture(
    *,
    root: Path,
    config: Mapping[str, Any],
    run_id: str | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    rid = validate_run_id(run_id or new_run_id())
    run_dir = artifact_dir(root, rid)
    diagnosis = diagnose_capture_source(config, env=env)
    compat = compatibility_from_config(config)
    capture_cfg = config.get("capture") if isinstance(config.get("capture"), dict) else {}
    target = capture_cfg.get("target") if isinstance(capture_cfg, dict) else None
    readiness = (
        capture_cfg.get("readiness")
        if isinstance(capture_cfg, dict)
        else config.get("readiness")
    )
    timestamp = utc_now()
    output = run_dir / "current.png"
    if output.exists() or output.is_symlink():
        output.unlink(missing_ok=True)
    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": CAPTURE_RESULT_KIND,
        "run_id": rid,
        "status": "blocked",
        "source": diagnosis["source"],
        "command": diagnosis.get("command"),
        "tool": diagnosis.get("tool"),
        "target": target,
        "readiness": readiness or "unspecified",
        "timestamp": timestamp,
        "pixel_decode": False,
        "playwright_required": False,
        "exit_code": None,
        "error": None,
        "image": None,
        "compatibility": compat,
        "artifact_dir": posix_relpath(root, run_dir),
    }
    if diagnosis["source"] == "none" or not diagnosis.get("command"):
        record["block_code"] = "capture_unavailable"
        record["error"] = diagnosis.get("detail")
        write_json(run_dir / "capture.json", record)
        _write_manifest(root, rid, command="visual.capture", extra={"capture": record})
        return record
    extra_env = {
        "OMG_VISUAL_TARGET": str(target or ""),
        "OMG_VISUAL_RUN_ID": rid,
        "OMG_VISUAL_VIEWPORT_WIDTH": str(compat.get("viewport_width") or ""),
        "OMG_VISUAL_VIEWPORT_HEIGHT": str(compat.get("viewport_height") or ""),
        "OMG_VISUAL_DPR_MILLI": str(compat.get("dpr_milli") or ""),
        "OMG_VISUAL_PLATFORM": str(compat.get("platform") or ""),
        "OMG_VISUAL_THEME": str(compat.get("theme") or ""),
        "OMG_VISUAL_LOCALE": str(compat.get("locale") or ""),
    }
    executed = execute_capture_command(
        diagnosis["command"],
        root=root,
        output=output,
        env=env,
        extra_env=extra_env,
    )
    record["exit_code"] = executed.get("exit_code")
    record["error"] = executed.get("error")
    if executed.get("status") != "captured" or not output.is_file():
        record["status"] = "blocked"
        record["block_code"] = "capture_failed"
        if not record["error"]:
            record["error"] = "capture command did not write an image"
        write_json(run_dir / "capture.json", record)
        _write_manifest(root, rid, command="visual.capture", extra={"capture": record})
        return record
    spec = _image_spec(config, "actual") or _image_spec(config, "reference")
    try:
        width, height = _require_hw(spec, config, width=None, height=None)
        record["image"] = image_descriptor(
            root=root,
            path=output,
            width=width,
            height=height,
            media_type=spec.get("media_type"),
        )
        record["status"] = "captured"
    except VisualRuntimeError as exc:
        record["status"] = "blocked"
        record["block_code"] = "capture_metadata"
        record["error"] = str(exc)
    write_json(run_dir / "capture.json", record)
    _write_manifest(root, rid, command="visual.capture", extra={"capture": record})
    return record


def _resolve_existing_image(
    root: Path,
    spec: Mapping[str, Any],
    fallback: str | None,
) -> Path | None:
    raw = spec.get("path") or fallback
    if not raw:
        return None
    return confine_workspace_path(root, str(raw))


def run_verdict(
    *,
    root: Path,
    config: Mapping[str, Any],
    reference_path: str | None = None,
    actual_path: str | None = None,
    threshold_percent: int | None = None,
    width: int | None = None,
    height: int | None = None,
    run_id: str | None = None,
    editor_role: str | None = None,
    reviewer_role: str | None = None,
    reviewer_capability: str | None = None,
) -> dict[str, Any]:
    rid = validate_run_id(run_id or new_run_id())
    run_dir = artifact_dir(root, rid)
    roles = load_role_capabilities()
    review = enforce_independent_reviewer(
        editor_role=str(editor_role or config.get("editor_role") or DEFAULT_EDITOR_ROLE),
        reviewer_role=str(
            reviewer_role or config.get("reviewer_role") or DEFAULT_REVIEWER_ROLE
        ),
        reviewer_capability=reviewer_capability
        or (
            str(config["reviewer_capability_mode"])
            if config.get("reviewer_capability_mode")
            else None
        ),
        roles=roles,
    )
    threshold = resolve_threshold(config, threshold_percent)
    ref_spec = _image_spec(config, "reference")
    act_spec = _image_spec(config, "actual") or _image_spec(config, "candidate")
    if reference_path:
        ref_src = confine_workspace_path(root, reference_path)
    else:
        ref_src = _resolve_existing_image(root, ref_spec, None)
    if actual_path:
        act_src = confine_workspace_path(root, actual_path)
    else:
        act_src = _resolve_existing_image(root, act_spec, None)
    if ref_src is None or act_src is None:
        raise VisualMetadataError("reference and actual image paths are required")
    ref_w, ref_h = _require_hw(ref_spec, config, width=width, height=height)
    act_w, act_h = _require_hw(act_spec, config, width=width, height=height)
    ref_media = ref_spec.get("media_type") or media_type_for(ref_src)
    act_media = act_spec.get("media_type") or media_type_for(act_src)
    ref_copy = copy_image(
        root, ref_src, run_dir / f"reference{ref_src.suffix or '.png'}"
    )
    act_copy = copy_image(
        root, act_src, run_dir / f"current{act_src.suffix or '.png'}"
    )
    reference = image_descriptor(
        root=root,
        path=ref_copy,
        width=ref_w,
        height=ref_h,
        media_type=ref_media,
    )
    candidate = image_descriptor(
        root=root,
        path=act_copy,
        width=act_w,
        height=act_h,
        media_type=act_media,
    )
    masks = config.get("masks") if isinstance(config.get("masks"), list) else []
    overlay = overlay_sidecar(masks=masks, reference=reference, candidate=candidate)
    write_json(run_dir / "overlay.json", overlay)
    write_json(run_dir / "reference.json", reference)
    write_json(run_dir / "current.json", candidate)
    compat = compatibility_from_config(config)
    dimensions = config.get("dimensions")
    dim_mismatch = (
        reference["width"] != candidate["width"]
        or reference["height"] != candidate["height"]
    )
    if not isinstance(dimensions, list) or not dimensions:
        if overlay["byte_identity"] and not dim_mismatch:
            dimensions = default_dimensions(10000)
        elif dim_mismatch:
            # compare() requires ten rows; blocked mismatch carries no scores.
            dimensions = default_dimensions(0)
        else:
            blocked = {
                "schema_version": SCHEMA_VERSION,
                "kind": VERDICT_RESULT_KIND,
                "run_id": rid,
                "status": "blocked",
                "block_code": "scoring_unavailable",
                "block_field": "dimensions",
                "reviewer_status": "blocked",
                "pixel_decode": False,
                "overlay_mode": "descriptor_only",
                "reference": reference,
                "current": candidate,
                "overlay": overlay,
                "findings": None,
                "score_history": [],
                "artifact_dir": posix_relpath(root, run_dir),
                **review,
                "note": (
                    "bytes differ and no dimension scores were supplied; "
                    "pixel diffs are not computed"
                ),
            }
            write_json(run_dir / "findings.json", {
                "schema_version": SCHEMA_VERSION,
                "kind": FINDINGS_KIND,
                "status": "blocked",
                "block_code": "scoring_unavailable",
                "dimensions": None,
            })
            write_json(run_dir / "score_history.json", {"schema_version": SCHEMA_VERSION, "entries": []})
            _write_manifest(root, rid, command="visual.verdict", extra={"verdict": blocked})
            write_json(run_dir / "verdict.json", blocked)
            return blocked
    document = {
        "schema_version": VISUAL_SCHEMA,
        "kind": COMPARISON_KIND,
        "reference": reference,
        "candidate": candidate,
        "reference_compatibility": compat,
        "candidate_compatibility": dict(compat),
        "dimensions": dimensions,
        "threshold": threshold,
        "masks": masks,
        "task_criteria": str(config.get("task_criteria") or "visual comparison"),
    }
    try:
        comparison = compare(document)
    except VisualContractError as exc:
        raise VisualConfigError("comparison document failed Visual Contract V1 validation") from exc
    reviewer_status = "blocked"
    if comparison.get("status") == "scored":
        aggregate = int(comparison["aggregate"])
        reviewer_status = "threshold_met" if aggregate >= threshold else "below_threshold"
    findings = {
        "schema_version": SCHEMA_VERSION,
        "kind": FINDINGS_KIND,
        "status": comparison.get("status"),
        "dimensions": comparison.get("dimensions"),
        "aggregate": comparison.get("aggregate"),
        "threshold": comparison.get("threshold"),
        "block_code": comparison.get("block_code"),
        "block_field": comparison.get("block_field"),
        "comparison_digest": comparison.get("comparison_digest"),
    }
    history = [
        {
            "iteration": 1,
            "status": comparison.get("status"),
            "aggregate": comparison.get("aggregate"),
            "threshold": comparison.get("threshold"),
            "comparison_digest": comparison.get("comparison_digest"),
            "reviewer_status": reviewer_status,
        }
    ]
    write_json(run_dir / "findings.json", findings)
    write_json(run_dir / "score_history.json", {"schema_version": SCHEMA_VERSION, "entries": history})
    result = {
        "schema_version": SCHEMA_VERSION,
        "kind": VERDICT_RESULT_KIND,
        "run_id": rid,
        "status": comparison.get("status"),
        "reviewer_status": reviewer_status,
        "pixel_decode": False,
        "overlay_mode": "descriptor_only",
        "reference": reference,
        "current": candidate,
        "comparison": comparison,
        "findings": findings,
        "overlay": overlay,
        "score_history": history,
        "artifact_dir": posix_relpath(root, run_dir),
        **review,
    }
    if comparison.get("status") == "blocked":
        result["block_code"] = comparison.get("block_code")
        result["block_field"] = comparison.get("block_field")
    write_json(run_dir / "verdict.json", result)
    _write_manifest(root, rid, command="visual.verdict", extra={"verdict": result})
    return result


def _write_repair_prompt(run_dir: Path, iteration: int, comparison: Mapping[str, Any]) -> dict[str, Any]:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "kind": REPAIR_KIND,
        "iteration": iteration,
        "spawned": False,
        "edited_ui": False,
        "next_action": (
            "delegate a bounded UI fix to a read-write designer/executor, "
            "then recapture; visual CLI does not spawn agents"
        ),
        "comparison_digest": comparison.get("comparison_digest"),
        "aggregate": comparison.get("aggregate"),
        "threshold": comparison.get("threshold"),
    }
    dest = run_dir / "iterations" / str(iteration) / "repair_prompt.json"
    write_json(dest, payload)
    md = run_dir / "iterations" / str(iteration) / "repair_prompt.md"
    md.parent.mkdir(parents=True, exist_ok=True)
    md.write_text(
        "# Visual repair prompt\n\n"
        "CLI did not spawn agents or edit UI.\n\n"
        f"- next_action: {payload['next_action']}\n"
        f"- aggregate: {payload.get('aggregate')}\n"
        f"- threshold: {payload.get('threshold')}\n",
        encoding="utf-8",
    )
    return payload


def run_ralph(
    *,
    root: Path,
    config: Mapping[str, Any],
    max_iter: int | None = None,
    threshold_percent: int | None = None,
    run_id: str | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    rid = validate_run_id(run_id or new_run_id())
    run_dir = artifact_dir(root, rid)
    roles = load_role_capabilities()
    review = enforce_independent_reviewer(
        editor_role=str(config.get("editor_role") or DEFAULT_EDITOR_ROLE),
        reviewer_role=str(config.get("reviewer_role") or DEFAULT_REVIEWER_ROLE),
        reviewer_capability=(
            str(config["reviewer_capability_mode"])
            if config.get("reviewer_capability_mode")
            else None
        ),
        roles=roles,
    )
    raw_max = max_iter if max_iter is not None else config.get("max_iter", 5)
    if isinstance(raw_max, bool) or not isinstance(raw_max, int) or raw_max < 1:
        raise VisualConfigError("max_iter must be an integer >= 1")
    if raw_max > 20:
        raise VisualConfigError("max_iter must be <= 20")
    threshold = resolve_threshold(config, threshold_percent)
    compat = compatibility_from_config(config)
    diagnosis = diagnose_capture_source(config, env=env)
    frozen = {
        "threshold": threshold,
        "max_iter": raw_max,
        "compatibility": compat,
        "capture_source": diagnosis["source"],
        "pixel_decode": False,
    }
    write_json(run_dir / "frozen.json", frozen)
    iterations: list[dict[str, Any]] = []
    stop_reason = "budget_exhausted"
    reviewer_status = "blocked"
    current_path: Path | None = None
    for iteration in range(1, raw_max + 1):
        iter_dir = run_dir / "iterations" / str(iteration)
        iter_dir.mkdir(parents=True, exist_ok=True)
        capture_record = None
        if diagnosis["source"] != "none":
            iter_config = dict(config)
            capture_record = run_capture(
                root=root,
                config=iter_config,
                run_id=f"{rid}-c{iteration}",
                env=env,
            )
            if capture_record.get("status") != "captured" or not capture_record.get("image"):
                stop_reason = "blocked"
                reviewer_status = "blocked"
                iterations.append(
                    {
                        "iteration": iteration,
                        "status": "blocked",
                        "block_code": capture_record.get("block_code") or "capture_failed",
                        "capture": {
                            "source": capture_record.get("source"),
                            "exit_code": capture_record.get("exit_code"),
                            "error": capture_record.get("error"),
                        },
                    }
                )
                break
            current_path = confine_workspace_path(root, capture_record["image"]["path"])
        elif current_path is None:
            actual_spec = _image_spec(config, "actual") or _image_spec(config, "candidate")
            provided = _resolve_existing_image(root, actual_spec, None)
            if provided is None:
                stop_reason = "blocked"
                reviewer_status = "blocked"
                iterations.append(
                    {
                        "iteration": iteration,
                        "status": "blocked",
                        "block_code": "capture_unavailable",
                        "note": "capture required for next iter",
                    }
                )
                break
            current_path = provided
        elif diagnosis["source"] == "none":
            stop_reason = "blocked"
            reviewer_status = "blocked"
            iterations.append(
                {
                    "iteration": iteration,
                    "status": "blocked",
                    "block_code": "capture_unavailable",
                    "note": "capture required for next iter",
                }
            )
            break
        verdict_config = dict(config)
        actual_spec = dict(_image_spec(config, "actual") or _image_spec(config, "candidate"))
        actual_spec["path"] = posix_relpath(root, current_path)
        verdict_config["actual"] = actual_spec
        try:
            verdict = run_verdict(
                root=root,
                config=verdict_config,
                actual_path=str(current_path),
                threshold_percent=threshold_percent,
                run_id=f"{rid}-v{iteration}",
                editor_role=review["editor_role"],
                reviewer_role=review["reviewer_role"],
                reviewer_capability=review["reviewer_capability"],
            )
        except VisualRuntimeError as exc:
            stop_reason = "blocked"
            reviewer_status = "blocked"
            iterations.append(
                {
                    "iteration": iteration,
                    "status": "blocked",
                    "block_code": getattr(exc, "code", "E_VISUAL_RUNTIME"),
                    "error": str(exc),
                }
            )
            break
        entry = {
            "iteration": iteration,
            "status": verdict.get("status"),
            "reviewer_status": verdict.get("reviewer_status"),
            "aggregate": (verdict.get("comparison") or {}).get("aggregate")
            if isinstance(verdict.get("comparison"), dict)
            else verdict.get("findings", {}).get("aggregate")
            if isinstance(verdict.get("findings"), dict)
            else None,
            "threshold": threshold,
            "comparison_digest": (verdict.get("comparison") or {}).get("comparison_digest")
            if isinstance(verdict.get("comparison"), dict)
            else None,
            "reference": verdict.get("reference"),
            "current": verdict.get("current"),
            "findings": verdict.get("findings"),
            "overlay": verdict.get("overlay"),
            "capture_source": diagnosis["source"],
        }
        write_json(iter_dir / "iteration.json", entry)
        iterations.append(entry)
        if verdict.get("status") == "blocked":
            stop_reason = "blocked"
            reviewer_status = "blocked"
            break
        aggregate = entry.get("aggregate")
        if isinstance(aggregate, int) and aggregate >= threshold:
            stop_reason = "threshold_met"
            reviewer_status = "threshold_met"
            break
        reviewer_status = "below_threshold"
        repair = _write_repair_prompt(run_dir, iteration, verdict.get("comparison") or {})
        entry["repair"] = {
            "path": posix_relpath(root, run_dir / "iterations" / str(iteration) / "repair_prompt.json"),
            "next_action": repair["next_action"],
            "spawned": False,
        }
        if iteration == raw_max:
            stop_reason = "budget_exhausted"
            reviewer_status = "below_threshold"
            break
        if diagnosis["source"] == "none":
            stop_reason = "blocked"
            reviewer_status = "blocked"
            iterations.append(
                {
                    "iteration": iteration + 1,
                    "status": "blocked",
                    "block_code": "capture_unavailable",
                    "note": "capture required for next iter",
                }
            )
            break
    history = [
        {
            "iteration": row.get("iteration"),
            "status": row.get("status"),
            "aggregate": row.get("aggregate"),
            "threshold": row.get("threshold"),
            "comparison_digest": row.get("comparison_digest"),
            "reviewer_status": row.get("reviewer_status"),
        }
        for row in iterations
    ]
    write_json(run_dir / "score_history.json", {"schema_version": SCHEMA_VERSION, "entries": history})
    result = {
        "schema_version": SCHEMA_VERSION,
        "kind": RALPH_RESULT_KIND,
        "run_id": rid,
        "status": "blocked" if stop_reason == "blocked" else "scored",
        "stop_reason": stop_reason,
        "reviewer_status": reviewer_status,
        "pixel_decode": False,
        "overlay_mode": "descriptor_only",
        "max_iter": raw_max,
        "threshold": threshold,
        "iterations": iterations,
        "score_history": history,
        "frozen": frozen,
        "next_action": None,
        "artifact_dir": posix_relpath(root, run_dir),
        **review,
    }
    if stop_reason == "blocked":
        last = iterations[-1] if iterations else {}
        result["block_code"] = last.get("block_code") or "blocked"
        if last.get("note"):
            result["note"] = last["note"]
            result["next_action"] = last["note"]
    elif stop_reason == "budget_exhausted":
        result["next_action"] = (
            "iteration budget exhausted; visual evidence only — not an OMG accept stamp"
        )
    elif stop_reason == "threshold_met":
        result["next_action"] = (
            "aggregate met threshold; evidence only — acceptance remains behind omg accept"
        )
    write_json(run_dir / "ralph.json", result)
    _write_manifest(root, rid, command="visual.ralph", extra={"ralph": result})
    return result


def _write_manifest(
    root: Path,
    run_id: str,
    *,
    command: str,
    extra: Mapping[str, Any] | None = None,
) -> None:
    run_dir = artifact_dir(root, run_id)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "kind": RUN_MANIFEST_KIND,
        "run_id": run_id,
        "command": command,
        "pixel_decode": False,
        "overlay_mode": "descriptor_only",
        "timestamp": utc_now(),
        "artifact_dir": posix_relpath(root, run_dir),
    }
    if extra:
        manifest.update(dict(extra))
    write_json(run_dir / "manifest.json", manifest)


__all__ = [
    "ENV_CAPTURE",
    "VisualConfigError",
    "VisualMetadataError",
    "VisualPathError",
    "VisualReviewerError",
    "VisualRuntimeError",
    "confine_workspace_path",
    "diagnose_capture_source",
    "discover_visual_config_path",
    "enforce_independent_reviewer",
    "load_visual_config",
    "new_run_id",
    "percent_to_score",
    "run_capture",
    "run_ralph",
    "run_verdict",
]
