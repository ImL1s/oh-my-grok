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
grok plugin install "$ROOT" --trust

echo "== inventory (best-effort) =="
if grok plugin list --json >/dev/null 2>&1; then
  grok plugin list --json | head -c 4000 || true
  echo
else
  grok plugin list 2>/dev/null || true
fi

echo
echo "Next steps:"
echo "  1. Put omg on PATH (symlink recommended):"
echo "       ln -sf \"$ROOT/bin/omg\" \"\${HOME}/.local/bin/omg\""
echo "  2. In your project:"
echo "       omg setup && omg doctor && omg doctor --strict"
echo "  3. Dry smoke from this repo:"
echo "       \"$ROOT/scripts/smoke.sh\""
echo "  4. Optional PreToolUse canary (never runs real claude/codex):"
echo "       python3 \"$ROOT/scripts/canary_pretool.py\" --dry"
echo
echo "install-plugin OK"
