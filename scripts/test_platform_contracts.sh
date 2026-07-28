#!/usr/bin/env bash
# Focused platform-contract suite for the early #25 macOS CI lane.
# Hermetic: no Grok account / network. Optional tmux for marked tests.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export PYTHONPATH="${PYTHONPATH:-.}"
PY="${PYTHON:-python3}"

if [[ -x "$ROOT/.venv/bin/python" ]]; then
  PY="$ROOT/.venv/bin/python"
fi

echo "== platform contracts ($("$PY" -c 'import sys; print(sys.version.split()[0])') on $(uname -s)) =="

# Core FS / lock / mode / symlink / install contracts — always run.
# Selection is by file list (OS-sensitive surfaces), not only the platform marker.
"$PY" -m pytest -q -m "not live" --tb=short \
  tests/test_path_keys.py \
  tests/test_team_meta_mutate.py \
  tests/test_hook_install.py \
  tests/test_state.py \
  "$@"

# tmux transport smoke only when tmux is present.
# When present, failures must fail the job (Codex P2 / macOS CI gate).
if command -v tmux >/dev/null 2>&1; then
  echo "== tmux-present transport smoke =="
  "$PY" -m pytest -q -m "tmux and not live" --tb=short \
    tests/test_team_tmux_transport.py \
    "$@"
else
  echo "== tmux not installed; skip tmux marker group =="
fi

echo "ALL_PLATFORM_CONTRACTS_OK"
