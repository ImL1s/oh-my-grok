#!/usr/bin/env bash
# Developer/source install.  Uses the SAME immutable transaction as release install.
# For the recommended no-checkout release path, use scripts/install.sh (curl-safe).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$ROOT"

command -v python3 >/dev/null 2>&1 \
  || { echo "ERROR: python3 >= 3.11 is required" >&2; exit 1; }
command -v grok >/dev/null 2>&1 \
  || { echo "ERROR: grok not on PATH. Install Grok Build CLI first." >&2; exit 1; }

echo "==> source install through immutable OMG transaction"
echo "    source: $ROOT"
echo "    dirty development bytes are preserved as a distinct immutable digest;"
echo "    release/update paths never overwrite them in place."

PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" \
  python3 -m omg_cli.setup_cmd install-source --source-root "$ROOT"

echo "==> source install exactly verified"
echo "    CLI: ${HOME}/.local/bin/omg"
echo "    next: omg setup && omg doctor"
