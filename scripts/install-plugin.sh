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

# Resolve ROOT for source comparison (realpath if available).
if command -v realpath >/dev/null 2>&1; then
  ROOT_RESOLVED="$(realpath "$ROOT")"
else
  ROOT_RESOLVED="$(cd "$ROOT" && pwd -P)"
fi

# Dedup warn: different source strings (e.g. "." vs abs path) create duplicate
# same-named entries; do NOT auto-uninstall — only warn clearly.
echo "== existing inventory (dedup check) =="
if LIST_JSON="$(grok plugin list --json 2>/dev/null)"; then
  # Best-effort Python parse (stdlib only); skip quietly if parse fails.
  if command -v python3 >/dev/null 2>&1; then
    export OMG_INSTALL_ROOT="$ROOT_RESOLVED"
    export OMG_INSTALL_LIST_JSON="$LIST_JSON"
    python3 - <<'PY' || true
import json, os, sys
root = os.environ.get("OMG_INSTALL_ROOT", "")
raw = os.environ.get("OMG_INSTALL_LIST_JSON", "")
try:
    data = json.loads(raw)
except Exception:
    sys.exit(0)
cands = []
if isinstance(data, list):
    cands = [x for x in data if isinstance(x, dict)]
elif isinstance(data, dict):
    for k in ("plugins", "items", "data", "result"):
        n = data.get(k)
        if isinstance(n, list):
            cands = [x for x in n if isinstance(x, dict)]
            break
    if not cands:
        cands = [data]
stale = []
for item in cands:
    name = str(item.get("name") or item.get("id") or item.get("plugin") or "")
    if "oh-my-grok" not in name:
        continue
    src = str(item.get("source") or item.get("path") or item.get("installPath") or "")
    if not src:
        continue
    # Normalize for comparison
    try:
        src_r = os.path.realpath(src)
    except Exception:
        src_r = src
    if src_r.rstrip("/") != root.rstrip("/") and src.rstrip("/") != root.rstrip("/"):
        key = name
        stale.append(f"  key={key!r} source/path={src!r}")
if stale:
    print("WARN: found oh-my-grok entry(ies) whose source/path differs from this checkout:", file=sys.stderr)
    for line in stale:
        print(line, file=sys.stderr)
    print(f"  this checkout: {root!r}", file=sys.stderr)
    print("  recommend: grok plugin uninstall oh-my-grok  (then re-run this script)", file=sys.stderr)
    print("  (installer will NOT auto-uninstall — remove the stale entry yourself)", file=sys.stderr)
PY
    unset OMG_INSTALL_ROOT OMG_INSTALL_LIST_JSON
  fi
else
  echo "(grok plugin list --json unavailable; skipping dedup check)"
fi

echo "== grok plugin validate =="
grok plugin validate "$ROOT"

echo "== grok plugin install . --trust =="
# SOURCE is this repo root; --trust for non-interactive hook/skill activation.
# "already installed" is OK — still force-refresh via update below.
INSTALLED_OK=0
if grok plugin install "$ROOT" --trust; then
  INSTALLED_OK=1
  echo "install: ok (or already present)"
else
  echo "WARN: plugin install returned non-zero (may already be installed); continuing with update/enable" >&2
fi

echo "== grok plugin update (force-refresh frozen snapshot) =="
# grok plugin install copies a FROZEN snapshot into ~/.grok/installed-plugins/;
# re-running install on an already-installed source no-ops. update force-refreshes.
UPDATED_OK=0
if grok plugin update oh-my-grok; then
  UPDATED_OK=1
  echo "update: refreshed on-disk snapshot for oh-my-grok"
else
  echo "WARN: grok plugin update oh-my-grok failed (best-effort); snapshot may be stale" >&2
fi

echo "== grok plugin enable (plugins disabled by default) =="
ENABLED_OK=0
if grok plugin enable oh-my-grok; then
  ENABLED_OK=1
  echo "enable: oh-my-grok in [plugins].enabled"
else
  echo "WARN: grok plugin enable oh-my-grok failed (best-effort)" >&2
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
echo "summary: install=${INSTALLED_OK} update=${UPDATED_OK} enable=${ENABLED_OK}"
echo "  (1=ok/attempted-success, 0=non-zero — re-run or check grok plugin list)"
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
echo "  5. After relocate/upgrade: re-run this script (update refreshes snapshot;"
echo "     global hook uses absolute path)"
echo
echo "install-plugin OK"
