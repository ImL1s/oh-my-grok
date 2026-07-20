#!/usr/bin/env bash
# Install oh-my-grok as a trusted Grok plugin from this repo checkout.
# Usage: scripts/install-plugin.sh
# Requires: grok on PATH
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if ! command -v grok >/dev/null 2>&1; then
  echo "ERROR: grok not on PATH. Install Grok Build CLI first." >&2
  exit 1
fi

echo "== grok plugin validate =="
grok plugin validate "$ROOT"

echo "== grok plugin install . --trust =="
# SOURCE is this repo root; --trust for non-interactive hook/skill activation.
# "already installed" is OK — still refresh global hooks below.
if ! grok plugin install "$ROOT" --trust; then
  echo "WARN: plugin install returned non-zero (may already be installed); continuing global hook write" >&2
fi

echo "== global PreToolUse soft-gate (~/.grok/hooks) =="
# Live 2026-07-19: plugin-bundled hooks/hooks.json did not appear in session
# hook_execution runs; only global/settings + ~/.grok/hooks fired. Install deny
# as a global hook so soft-gate is effective for leader + subagents.
HOOKS_DIR="${HOME}/.grok/hooks"
mkdir -p "$HOOKS_DIR"
DENY_PY="${ROOT}/hooks/bin/pre_tool_use_deny.py"
cat > "${HOOKS_DIR}/omg-pretool-deny.json" <<EOF
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "run_terminal_command|Bash|Shell|spawn_subagent|Task",
        "hooks": [
          {
            "type": "command",
            "command": "python3 \"${DENY_PY}\"",
            "timeout": 5
          }
        ]
      }
    ]
  }
}
EOF
echo "wrote ${HOOKS_DIR}/omg-pretool-deny.json -> ${DENY_PY}"

echo "== inventory (best-effort) =="
if grok plugin list --json >/dev/null 2>&1; then
  grok plugin list --json | head -c 4000 || true
  echo
else
  grok plugin list 2>/dev/null || true
fi

echo "== omg CLI symlink (best-effort) =="
LOCAL_BIN="${HOME}/.local/bin"
OMG_BIN="${ROOT}/bin/omg"
if [[ -x "$OMG_BIN" ]]; then
  mkdir -p "$LOCAL_BIN" 2>/dev/null || true
  if [[ -d "$LOCAL_BIN" && -w "$LOCAL_BIN" ]]; then
    ln -sfn "$OMG_BIN" "${LOCAL_BIN}/omg"
    echo "linked ${LOCAL_BIN}/omg -> ${OMG_BIN}"
    if ! command -v omg >/dev/null 2>&1; then
      echo "NOTE: add ${LOCAL_BIN} to PATH if 'omg' is not found" >&2
    fi
  else
    echo "WARN: cannot write ${LOCAL_BIN}; symlink manually:" >&2
    echo "  ln -sf \"${OMG_BIN}\" \"\${HOME}/.local/bin/omg\"" >&2
  fi
fi

echo
echo "Next steps:"
echo "  1. Confirm omg on PATH (install tried ~/.local/bin/omg):"
echo "       omg --version"
echo "  2. In your project:"
echo "       omg setup && omg doctor && omg doctor --strict"
echo "  3. Dry smoke from this repo:"
echo "       \"$ROOT/scripts/smoke.sh\""
echo "  4. Optional PreToolUse canary (never runs real claude/codex):"
echo "       python3 \"$ROOT/scripts/canary_pretool.py\" --dry"
echo "       python3 \"$ROOT/scripts/canary_pretool.py\" --live   # needs grok + global hook"
echo "  5. After relocate/upgrade: re-run this script (global hook uses absolute path)"
echo
echo "install-plugin OK"
