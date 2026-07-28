#!/usr/bin/env bash
# Single entry point for repository static analysis (#24).
# CI and release must call this script (not an enumerated file list).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

export PYTHONPATH="${PYTHONPATH:-.}"
PY="${PYTHON:-python3}"
if [[ -x "$ROOT/.venv/bin/python" ]]; then
  PY="$ROOT/.venv/bin/python"
fi

if [[ -x "$ROOT/.venv/bin/ruff" ]]; then
  RUFF=("$ROOT/.venv/bin/ruff")
elif command -v ruff >/dev/null 2>&1; then
  RUFF=(ruff)
else
  echo "ruff not found (install ruff==0.15.22)" >&2
  exit 1
fi

# Roots auto-included by path discovery (no hand-picked file lists).
RUFF_ROOTS=(omg_cli tests scripts hooks)

echo "== static coverage inventory =="
"$PY" scripts/check_static_coverage.py --check

echo "== ruff ($("${RUFF[@]}" --version 2>/dev/null || echo ruff)) =="
"${RUFF[@]}" check "${RUFF_ROOTS[@]}"

echo "== compileall omg_cli hooks/bin =="
"$PY" -m compileall -q omg_cli hooks/bin

echo "== mypy (staged public surface via pyproject.toml) =="
"$PY" -m mypy

echo "ALL_STATIC_CHECKS_OK"
