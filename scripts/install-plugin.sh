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

# Inventory parse: (1) WARN on different-path duplicates (do NOT auto-remove);
# (2) detect same-path install so we can force-refresh via uninstall+reinstall.
# Reuses one `grok plugin list --json` parse for both.
SAME_PATH_INSTALLED=0
echo "== existing inventory (dedup + same-path check) =="
if LIST_JSON="$(grok plugin list --json 2>/dev/null)"; then
  # Best-effort Python parse (stdlib only); skip quietly if parse fails.
  if command -v python3 >/dev/null 2>&1; then
    export OMG_INSTALL_ROOT="$ROOT_RESOLVED"
    export OMG_INSTALL_LIST_JSON="$LIST_JSON"
    # Multi-candidate same-path classifier (importable helper; unit-tested).
    # Independent realpath of source/path/installPath/install_path vs root —
    # never OR-collapse to a single field (path is dual-meaning: checkout vs snapshot).
    # stdout captured for SAME_PATH_INSTALLED=; stderr WARNs pass through.
    PARSE_OUT="$(
      PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" \
        python3 "$ROOT/scripts/omg_install_classifier.py" || true
    )"
    unset OMG_INSTALL_ROOT OMG_INSTALL_LIST_JSON
    if [[ "$PARSE_OUT" == *"SAME_PATH_INSTALLED=1"* ]]; then
      SAME_PATH_INSTALLED=1
      echo "found existing oh-my-grok install for this checkout (will refresh snapshot)"
    else
      echo "no same-path oh-my-grok install detected (fresh install path)"
    fi
  fi
else
  echo "(grok plugin list --json unavailable; skipping dedup/same-path check)"
fi

echo "== grok plugin validate =="
grok plugin validate "$ROOT"

# Install / force-refresh frozen snapshot.
# grok plugin install copies a FROZEN snapshot into ~/.grok/installed-plugins/.
# For an ALREADY-installed local-path plugin, both `install` and `update` are
# no-ops — the only reliable refresh is uninstall-then-reinstall (back-to-back).
echo "== grok plugin install . --trust =="
INSTALLED_OK=0
REFRESHED=0
if [[ "$SAME_PATH_INSTALLED" -eq 1 ]]; then
  # Reinstall = refresh: uninstall FIRST, then install immediately.
  echo "refreshing (uninstall+reinstall)…"
  if grok plugin uninstall oh-my-grok --confirm; then
    echo "uninstall: ok (preparing fresh install)"
  else
    echo "WARN: grok plugin uninstall oh-my-grok --confirm returned non-zero; attempting reinstall anyway" >&2
  fi
  # BACK-TO-BACK: do not leave a gap — if reinstall fails the plugin may be gone.
  if grok plugin install "$ROOT" --trust; then
    INSTALLED_OK=1
    REFRESHED=1
    echo "install: ok (fresh snapshot after uninstall+reinstall)"
  else
    echo "ERROR: ============================================================" >&2
    echo "ERROR: reinstall FAILED after uninstall — plugin may now be REMOVED." >&2
    echo "ERROR: re-run this script, or:" >&2
    echo "ERROR:   grok plugin install \"$ROOT\" --trust" >&2
    echo "ERROR: ============================================================" >&2
    exit 1
  fi
else
  # Not already installed for this path — normal install (no uninstall).
  if grok plugin install "$ROOT" --trust; then
    INSTALLED_OK=1
    echo "install: ok (new install)"
  else
    echo "WARN: plugin install returned non-zero (may already be installed under another key); continuing with update/enable" >&2
  fi
fi

echo "== grok plugin update (best-effort; no-op for local-path) =="
# Local-path installs: `grok plugin update` is a no-op. Snapshot refresh is
# performed above via uninstall+reinstall when same-path was detected. Keep
# update as a harmless best-effort (may still help non-local install sources).
UPDATED_OK=0
if grok plugin update oh-my-grok; then
  UPDATED_OK=1
  echo "update: ok (no-op for local-path; refresh is uninstall+reinstall above)"
else
  echo "WARN: grok plugin update oh-my-grok failed (best-effort; local-path refresh uses uninstall+reinstall)" >&2
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
echo "summary: install=${INSTALLED_OK} refresh=${REFRESHED} update=${UPDATED_OK} enable=${ENABLED_OK}"
echo "  (1=ok/attempted-success, 0=non-zero — re-run or check grok plugin list)"
echo "  refresh=1 means same-path uninstall+reinstall refreshed the frozen snapshot"
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
echo "  5. After relocate/upgrade: re-run this script (same-path reinstall"
echo "     refreshes the frozen snapshot; global hook uses absolute path)"
echo
echo "install-plugin OK"
