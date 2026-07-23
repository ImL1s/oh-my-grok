"""Evidence-gated adapter boundary for preview Grok workflow authoring.

Product workflow execution is owned by :mod:`omg_cli.workflows.runner`.  Grok
``/create-workflow`` and Rhai projection remain ``optional_unclaimed`` until a
stable public schema and a fresh, successful slash invocation are both supplied
as evidence.  Merely finding a ``.rhai`` file or help text never promotes it.
"""
from __future__ import annotations

import shutil
import subprocess
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and _SHA256_RE.fullmatch(value) is not None


class NativeWorkflowUnsupported(RuntimeError):
    code = "E_WORKFLOW_NATIVE_UNSUPPORTED"


def assess_native_capability(
    root: Path | str,
    *,
    public_schema_evidence: Mapping[str, Any] | None = None,
    fresh_invocation_evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    base = Path(root).resolve()
    rhai_files = sorted(
        path.relative_to(base).as_posix()
        for path in (base / ".grok" / "workflows").glob("*.rhai")
        if path.is_file() and not path.is_symlink()
    ) if (base / ".grok" / "workflows").is_dir() else []
    schema_stable = bool(
        public_schema_evidence
        and public_schema_evidence.get("stable") is True
        and public_schema_evidence.get("public") is True
        and _is_sha256(public_schema_evidence.get("schema_digest"))
    )
    fresh_success = bool(
        fresh_invocation_evidence
        and fresh_invocation_evidence.get("fresh") is True
        and fresh_invocation_evidence.get("slash_command") == "/create-workflow"
        and fresh_invocation_evidence.get("success") is True
        and _is_sha256(fresh_invocation_evidence.get("output_digest"))
    )
    claimed = schema_stable and fresh_success
    return {
        "provider": "grok",
        "status": "claimed" if claimed else "optional_unclaimed",
        "slash_command": "/create-workflow",
        "rhai_files": rhai_files,
        "local_bundle_observed": bool(rhai_files),
        "public_schema_stable": schema_stable,
        "fresh_invocation_observed": fresh_success,
        "semantic_claim": claimed,
        "reason": None if claimed else "stable public schema plus fresh slash proof not both present",
    }


def safe_headless_probe(*, grok_binary: str = "grok", timeout_seconds: float = 5.0) -> dict[str, Any]:
    """Probe only version/help; never invoke an interactive slash command."""
    binary = shutil.which(grok_binary)
    if binary is None:
        return {
            "binary_found": False,
            "help_observed": False,
            "create_workflow_mentioned": False,
            "status": "optional_unclaimed",
        }
    try:
        process = subprocess.run(
            [binary, "--help"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=min(max(float(timeout_seconds), 0.1), 10.0),
            check=False,
            env={"PATH": str(Path(binary).parent)},
        )
        text = (process.stdout or "")[:131_072]
        return {
            "binary_found": True,
            "help_observed": process.returncode == 0,
            "returncode": process.returncode,
            "create_workflow_mentioned": "create-workflow" in text,
            "status": "optional_unclaimed",
            "note": "help text is not stable schema or fresh slash invocation proof",
        }
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "binary_found": True,
            "help_observed": False,
            "error": str(exc),
            "create_workflow_mentioned": False,
            "status": "optional_unclaimed",
        }


def project_to_rhai(_definition: Mapping[str, Any]) -> str:
    raise NativeWorkflowUnsupported(
        "E_WORKFLOW_NATIVE_UNSUPPORTED: arbitrary repository-workflow/v1 to Rhai projection is disabled"
    )


__all__ = [
    "NativeWorkflowUnsupported",
    "assess_native_capability",
    "project_to_rhai",
    "safe_headless_probe",
]
