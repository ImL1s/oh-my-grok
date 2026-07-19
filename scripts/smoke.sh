#!/usr/bin/env bash
# oh-my-grok smoke checks (doctor, dry-run modes, plugin validate).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

export PYTHONPATH="${ROOT}${PYTHONPATH:+:${PYTHONPATH}}"
OMG=(python3 "${ROOT}/bin/omg")

echo "== omg doctor =="
"${OMG[@]}" doctor || true

echo "== omg doctor --strict (may warn/fail on host gaps) =="
"${OMG[@]}" doctor --strict || true

echo "== mode dry-runs =="
# Each mode creates an active run; cancel between so the mutex does not block.
tmp="$(mktemp -d "${TMPDIR:-/tmp}/omg-smoke.XXXXXX")"
cleanup() { rm -rf "$tmp"; }
trap cleanup EXIT

# Use a throwaway project so we do not clobber the repo's .omg active pointer.
mkdir -p "$tmp"
(
  cd "$tmp"
  "${OMG[@]}" setup >/dev/null
  "${OMG[@]}" ulw "smoke ulw" --dry-run
  "${OMG[@]}" cancel --grace 0 >/dev/null 2>&1 || true
  # ralph without PRD commands exits 1 under require_acceptance; allow that
  "${OMG[@]}" ralph "smoke ralph" --dry-run --no-require-acceptance
  "${OMG[@]}" cancel --grace 0 >/dev/null 2>&1 || true
  # ralplan dry-run without verifier APPROVE exits non-zero — argv/run is enough
  "${OMG[@]}" ralplan "smoke ralplan" --dry-run || true
  "${OMG[@]}" cancel --grace 0 >/dev/null 2>&1 || true
)

echo "== plugin validate =="
if command -v grok >/dev/null 2>&1; then
  grok plugin validate "$ROOT" || {
    echo "WARN: grok plugin validate failed (non-fatal for smoke)" >&2
  }
else
  echo "WARN: grok not on PATH; skip plugin validate" >&2
fi

echo "== accept --help (allowlist flags) =="
"${OMG[@]}" accept --help | grep -E -- '--review|--yes|--allow-cmd|--no-allowlist' >/dev/null

echo "smoke OK"
