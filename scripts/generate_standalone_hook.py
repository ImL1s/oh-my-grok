#!/usr/bin/env python3
"""Generate / check hooks/bin/omg_pretool_deny_standalone.py from canonical sources.

WHY this exists
---------------
The global PreToolUse soft-gate must be a SELF-CONTAINED, stdlib-only script
installed under ``$GROK_HOME/hooks/`` (see ``omg_cli.hook_install``). Pointing the
global hook at a script inside a project checkout (e.g. under macOS-TCC-protected
``~/Documents``) that also ``import``s ``omg_cli`` is a latent, catastrophic bug:
a grok session in another workspace / without Documents access cannot even
``open()`` the script, ``python3 <script>`` exits **2**, and — because grok reads
a PreToolUse exit code of 2 as an *explicit deny* — grok blocks EVERY tool call.

This generator produces the standalone deterministically by embedding the
canonical ``omg_cli/deny.py`` decision logic verbatim (validated stdlib-only)
plus ``hooks/bin/_common.hook_disabled``, wrapped in a fail-OPEN ``main()`` that:

- signals deny ONLY via the stdout JSON ``{"decision": "deny"}`` decision
  (grok honors that "regardless of exit code" per its hook contract), and
- ALWAYS exits 0, so a nonzero exit (especially 2) can never come from us and an
  interpreter/startup failure fails OPEN, not closed.

Single source of truth: ``omg_cli/deny.py`` + ``_common.hook_disabled``. A unit
test + CI run ``--check`` so the committed standalone can never drift from them.

Usage:
  python3 scripts/generate_standalone_hook.py           # (re)write committed standalone
  python3 scripts/generate_standalone_hook.py --check    # exit 1 if committed is stale
  python3 scripts/generate_standalone_hook.py --print    # emit to stdout only
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import sys
from pathlib import Path

# deny.py + _common.hook_disabled may only import from these stdlib modules; a
# non-stdlib import would break the "self-contained, runs under ``python3 -I -S``
# with no PYTHONPATH" guarantee. Enforced at generation time (fail-closed).
STDLIB_IMPORT_ALLOWLIST = frozenset({"__future__", "os", "re", "sys", "json", "typing"})
# Runtime names the generated _HEADER binds. deny.py's stripped top-level imports must
# bind ONLY these — otherwise the stripped body references an unbound name (NameError),
# e.g. `from os import environ` binds `environ`, which the header does not provide.
HEADER_PROVIDED_NAMES = frozenset({"json", "os", "re", "sys", "Any", "annotations"})
STANDALONE_BASENAME = "omg_pretool_deny_standalone.py"
INTERFACE_VERSION = "standalone_hook_generator/1"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def committed_path(root: Path | None = None) -> Path:
    return (root or _repo_root()) / "hooks" / "bin" / STANDALONE_BASENAME


def _plugin_version(root: Path) -> str:
    try:
        return str(json.loads((root / "plugin.json").read_text(encoding="utf-8"))["version"])
    except Exception:
        return "0"


def _extract_function_source(src: str, name: str) -> str:
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            seg = ast.get_source_segment(src, node)
            if seg is None:
                raise SystemExit(f"generate_standalone_hook: cannot extract source for {name!r}")
            return seg.strip("\n")
    raise SystemExit(f"generate_standalone_hook: function {name!r} not found")


def _validate_stdlib_only(src: str, label: str) -> None:
    """Fail-closed if ANY import in the tree (nested too) is non-stdlib or relative.

    ``ast.walk`` covers function/class-body imports, not just top-level — a nested
    ``import requests`` or ``from .evil import x`` would otherwise slip into the
    embedded body and break the "runs under python3 -I -S, stdlib-only" guarantee.
    """
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if top not in STDLIB_IMPORT_ALLOWLIST:
                    raise SystemExit(
                        f"generate_standalone_hook: {label} imports non-stdlib {alias.name!r}; "
                        "the standalone must be stdlib-only"
                    )
                if alias.asname is not None:
                    # A stripped `import os as X` would leave X unbound in the body (the
                    # header re-imports plain names only) → NameError. Force plain imports.
                    raise SystemExit(
                        f"generate_standalone_hook: {label} uses an import alias "
                        f"({alias.name} as {alias.asname}); the standalone header re-imports "
                        "plain stdlib names only — use a plain import"
                    )
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                raise SystemExit(
                    f"generate_standalone_hook: {label} has a relative import "
                    f"(level {node.level}); not allowed in the standalone"
                )
            mod = (node.module or "").split(".")[0]
            if mod not in STDLIB_IMPORT_ALLOWLIST:
                raise SystemExit(
                    f"generate_standalone_hook: {label} imports from non-stdlib {node.module!r}; "
                    "the standalone must be stdlib-only"
                )
            for alias in node.names:
                if alias.asname is not None:
                    raise SystemExit(
                        f"generate_standalone_hook: {label} uses a from-import alias "
                        f"({alias.name} as {alias.asname}); use a plain import"
                    )
        elif isinstance(node, ast.Call):
            # Dynamic imports bypass the static import allowlist entirely.
            fn = node.func
            fname = getattr(fn, "id", None) or getattr(fn, "attr", None)
            if fname in ("__import__", "import_module"):
                raise SystemExit(
                    f"generate_standalone_hook: {label} uses a dynamic import ({fname}); "
                    "not allowed in the standalone"
                )
        elif isinstance(node, ast.Name) and node.id == "__import__":
            # `loader = __import__; loader(...)` rebinds the dynamic-import builtin.
            raise SystemExit(
                f"generate_standalone_hook: {label} references __import__ (dynamic import); "
                "not allowed in the standalone"
            )


def _deny_body_after_imports(src: str) -> str:
    """Return deny.py source AFTER a CONTIGUOUS top-level import preamble.

    Fail-closed: every import (nested too) must be stdlib and non-relative, and NO
    top-level import may appear AFTER the first non-import statement — a late import
    would otherwise extend the stripped preamble and silently drop preceding globals.
    """
    _validate_stdlib_only(src, "deny.py")
    tree = ast.parse(src)
    preamble_end = 0
    seen_code = False
    for node in tree.body:
        is_import = isinstance(node, (ast.Import, ast.ImportFrom))
        is_docstring = (
            isinstance(node, ast.Expr)
            and isinstance(getattr(node, "value", None), ast.Constant)
            and isinstance(node.value.value, str)
        )
        if is_import:
            if seen_code:
                raise SystemExit(
                    "generate_standalone_hook: deny.py has a top-level import after code; "
                    "move all imports into the contiguous top preamble"
                )
            # The stripped preamble may bind ONLY names the header re-provides — else the
            # body references an unbound name (e.g. `from os import environ` → `environ`).
            for alias in node.names:
                if isinstance(node, ast.Import):
                    bound = (alias.asname or alias.name).split(".")[0]
                else:  # ImportFrom
                    bound = alias.asname or alias.name
                if bound not in HEADER_PROVIDED_NAMES:
                    raise SystemExit(
                        f"generate_standalone_hook: deny.py top-level import binds {bound!r}, "
                        "which the standalone header does not provide; add it to the header "
                        "(and HEADER_PROVIDED_NAMES) or drop the import"
                    )
            preamble_end = max(preamble_end, node.end_lineno or 0)
        elif is_docstring and not seen_code:
            preamble_end = max(preamble_end, node.end_lineno or 0)
        else:
            seen_code = True
    lines = src.splitlines(keepends=True)
    body = "".join(lines[preamble_end:])
    return body.strip("\n") + "\n"


_HEADER = '''\
#!/usr/bin/env python3
# @generated by scripts/generate_standalone_hook.py — DO NOT EDIT BY HAND.
"""oh-my-grok PreToolUse deny soft-gate — SELF-CONTAINED standalone (GENERATED).

Installed under $GROK_HOME/hooks/ (a globally-readable, non-TCC, non-workspace
location) so EVERY grok session — including sessions whose workspace is another
project — can open and run it. It is stdlib-only and carries ZERO import from any
project checkout, so it is immune to the "checkout under ~/Documents (TCC) → python
exits 2 → grok denies every tool" failure class.

Contract (grok hooks 10-hooks.md): exit 0 = allow, exit 2 = explicit deny, any
other exit = fail-open, and a stdout {{"decision":"deny"}} is honored REGARDLESS of
exit code. Therefore this script signals deny ONLY via stdout JSON and ALWAYS exits
0 — a nonzero exit (esp. 2, which python emits for "can't open file") must never
come from us. The install launcher additionally wraps it as
``python3 -I -S "<abs>" || true`` so an interpreter/startup failure also fails open.

Regenerate with: python3 scripts/generate_standalone_hook.py
Source of truth: omg_cli/deny.py + hooks/bin/_common.hook_disabled
"""
from __future__ import annotations

import json
import os
import re
import sys
from typing import Any

_OMG_STANDALONE_GENERATED = True
_OMG_GENERATED_FROM_SHA = "{source_sha}"
_OMG_PLUGIN_VERSION = "{plugin_version}"
'''

_FOOTER = '''\


def main() -> None:
    # Fail-OPEN everywhere: compute a decision, default to allow on ANY error, and
    # ALWAYS exit 0. Deny is carried solely by the stdout JSON decision (grok honors
    # {"decision":"deny"} regardless of exit code). We must NEVER exit 2 — that is
    # grok's "explicit deny" code and also what python emits for "can't open file",
    # the exact collision this standalone exists to eliminate.
    decision: dict[str, str] = {"decision": "allow", "reason": "omg-hook-default"}
    try:
        if hook_disabled("pre_tool_use"):
            decision = {"decision": "allow", "reason": "OMG hooks disabled"}
        else:
            try:
                raw = sys.stdin.read()
                event = json.loads(raw) if raw.strip() else {}
            except Exception:
                event = {}
            decision = decide_pre_tool_use(event)
    except Exception as e:  # noqa: BLE001 — soft-gate must never hard-block a session
        decision = {"decision": "allow", "reason": f"omg-hook-error:{type(e).__name__}"}
    try:
        sys.stdout.write(json.dumps(decision) + "\\n")
    except Exception:
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
'''


def render(root: Path | None = None) -> str:
    root = (root or _repo_root()).resolve()
    deny_src = (root / "omg_cli" / "deny.py").read_text(encoding="utf-8")
    common_src = (root / "hooks" / "bin" / "_common.py").read_text(encoding="utf-8")

    hook_disabled_src = _extract_function_source(common_src, "hook_disabled")
    _validate_stdlib_only(hook_disabled_src, "_common.hook_disabled")
    deny_body = _deny_body_after_imports(deny_src)

    # Source SHA over the canonical inputs (drift-detectable by doctor).
    source_sha = hashlib.sha256(
        (hook_disabled_src + "\n---\n" + deny_body).encode("utf-8")
    ).hexdigest()

    header = _HEADER.format(source_sha=source_sha, plugin_version=_plugin_version(root))
    kill_switch = (
        "\n\n# ---- kill switch (extracted verbatim from hooks/bin/_common.hook_disabled) ----\n"
        + hook_disabled_src
        + "\n"
    )
    deny_block = (
        "\n\n# ==== embedded omg_cli/deny.py decision logic (GENERATED verbatim) ====\n"
        + deny_body
    )
    return header + kill_switch + deny_block + _FOOTER


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate/check the self-contained PreToolUse standalone hook")
    parser.add_argument("--check", action="store_true", help="exit 1 if the committed standalone is stale")
    parser.add_argument("--print", action="store_true", dest="to_stdout", help="print generated content to stdout")
    parser.add_argument(
        "--interface",
        action="store_true",
        help="print the stable generator interface identifier and exit",
    )
    parser.add_argument("--root", type=Path, default=None, help="repo root (default: repo containing this script)")
    args = parser.parse_args(argv)
    root = (args.root if args.root is not None else _repo_root()).resolve()

    if args.interface:
        print(INTERFACE_VERSION)
        return 0

    content = render(root)
    if args.to_stdout:
        sys.stdout.write(content)
        return 0

    path = committed_path(root)
    if args.check:
        if not path.is_file():
            print(f"missing {path} — run: python3 scripts/generate_standalone_hook.py", file=sys.stderr)
            return 1
        current = path.read_text(encoding="utf-8")
        if current == content:
            print(f"ok: {path.name} matches canonical deny.py+_common (sha in header)")
            return 0
        print(
            f"stale {path} — regenerate: python3 scripts/generate_standalone_hook.py",
            file=sys.stderr,
        )
        return 1

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
