#!/usr/bin/env bash
# oh-my-grok smoke checks (doctor, dry-run modes, plugin validate).
# Fail-fast by default. Set OMG_SMOKE_STRICT=0 to tolerate doctor hard fails.
# Live grok sessions are never required for dry smoke.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

export PYTHONPATH="${ROOT}${PYTHONPATH:+:${PYTHONPATH}}"
OMG=(python3 "${ROOT}/bin/omg")

# OMG_SMOKE_STRICT=1 → doctor / doctor --strict must exit 0 (release gate).
# Default 0: dry-run matrix + canary still fail-fast; doctor soft.
STRICT="${OMG_SMOKE_STRICT:-0}"

# Default ON for hermetic e2e (no LLM). Set OMG_E2E=0 to skip.
OMG_E2E="${OMG_E2E:-1}"

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

echo "== omg doctor =="
if ! "${OMG[@]}" doctor; then
  if [[ "$STRICT" == "1" ]]; then
    fail "omg doctor failed (OMG_SMOKE_STRICT=1)"
  fi
  echo "WARN: omg doctor failed (set OMG_SMOKE_STRICT=1 for hard gate)" >&2
fi

echo "== omg doctor --strict =="
if ! "${OMG[@]}" doctor --strict; then
  if [[ "$STRICT" == "1" ]]; then
    fail "omg doctor --strict failed (install plugin or unset OMG_SMOKE_STRICT)"
  fi
  echo "WARN: omg doctor --strict failed (optional strict gate off)" >&2
fi

echo "== mode dry-runs =="
tmp="$(mktemp -d "${TMPDIR:-/tmp}/omg-smoke.XXXXXX")"
cleanup() { rm -rf "$tmp"; }
trap cleanup EXIT

mkdir -p "$tmp"
(
  set -euo pipefail
  cd "$tmp"
  "${OMG[@]}" setup >/dev/null
  "${OMG[@]}" ulw "smoke ulw" --dry-run
  "${OMG[@]}" cancel --grace 0 >/dev/null 2>&1 || true
  # ralph without PRD commands exits 1 under require_acceptance; allow that
  set +e
  "${OMG[@]}" ralph "smoke ralph" --dry-run --no-require-acceptance
  ralph_rc=$?
  set -e
  if [[ "$ralph_rc" -ne 0 && "$ralph_rc" -ne 1 ]]; then
    fail "ralph dry-run unexpected exit $ralph_rc"
  fi
  "${OMG[@]}" cancel --grace 0 >/dev/null 2>&1 || true
  # ralplan dry-run without verifier APPROVE may exit non-zero — argv/run is enough
  set +e
  "${OMG[@]}" ralplan "smoke ralplan" --dry-run
  ralplan_rc=$?
  set -e
  if [[ "$ralplan_rc" -gt 2 ]]; then
    fail "ralplan dry-run unexpected exit $ralplan_rc"
  fi
  "${OMG[@]}" cancel --grace 0 >/dev/null 2>&1 || true
) || fail "mode dry-run matrix failed"

echo "== plugin validate =="
if command -v grok >/dev/null 2>&1; then
  grok plugin validate "$ROOT" || fail "grok plugin validate failed"
else
  echo "WARN: grok not on PATH; skip plugin validate" >&2
fi

echo "== accept --help (policy flags) =="
"${OMG[@]}" accept --help | grep -E -- '--review|--yes|--allow-cmd|--no-allowlist' >/dev/null \
  || fail "accept --help missing policy flags"

echo "== standalone release/install surfaces =="
bash "${ROOT}/scripts/install.sh" --help | grep -F 'exact tag' >/dev/null \
  || fail "install.sh --help missing immutable-tag contract"
python3 "${ROOT}/scripts/release_attest.py" --help >/dev/null \
  || fail "release_attest.py --help failed"
[[ "$(python3 "${ROOT}/scripts/generate_standalone_hook.py" --interface)" == "standalone_hook_generator/1" ]] \
  || fail "standalone hook generator interface mismatch"
# W6 owns the generated hook bytes; W1 deliberately does not turn a known
# cross-wave stale output into a local smoke failure before W6 regenerates it.

echo "== canary_pretool --dry =="
python3 "${ROOT}/scripts/canary_pretool.py" --dry >/dev/null \
  || fail "canary_pretool --dry failed"

echo "smoke OK"

# Hermetic real-path e2e (temp git project; no external LLM). Default OMG_E2E=1.
if [[ "${OMG_E2E}" == "1" ]]; then
  echo "== e2e_realpath.py =="
  python3 "$ROOT/scripts/e2e_realpath.py"
fi
