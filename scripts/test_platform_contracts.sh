#!/usr/bin/env bash
# Focused platform-contract suite for #25 macOS CI lane (also runnable on Linux).
# Hermetic: no Grok account / network. Optional tmux for marked tests.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export PYTHONPATH="${PYTHONPATH:-.}"
PY="${PYTHON:-python3}"

if [[ -x "$ROOT/.venv/bin/python" ]]; then
  PY="$ROOT/.venv/bin/python"
fi

# Hermetic operator identity (CI sets HOME/GROK_HOME; reinforce locale/git).
# Prefer C.UTF-8 on Linux; fall back for macOS which often lacks C.UTF-8 (#25).
if [[ -z "${LANG:-}" || -z "${LC_ALL:-}" ]]; then
  if locale -a 2>/dev/null | grep -qiE '^(C\.UTF-8|C\.utf8)$'; then
    export LANG=C.UTF-8
    export LC_ALL=C.UTF-8
  elif locale -a 2>/dev/null | grep -qiE 'en_US\.UTF-8'; then
    export LANG=en_US.UTF-8
    export LC_ALL=en_US.UTF-8
  else
    export LANG=C
    export LC_ALL=C
  fi
fi
export GIT_AUTHOR_NAME="${GIT_AUTHOR_NAME:-omg-platform-ci}"
export GIT_AUTHOR_EMAIL="${GIT_AUTHOR_EMAIL:-omg-platform-ci@example.com}"
export GIT_COMMITTER_NAME="${GIT_COMMITTER_NAME:-$GIT_AUTHOR_NAME}"
export GIT_COMMITTER_EMAIL="${GIT_COMMITTER_EMAIL:-$GIT_AUTHOR_EMAIL}"

_platform_diag() {
  echo "== platform diagnostics =="
  echo "uname:      $(uname -a 2>/dev/null || true)"
  echo "python:     $("$PY" -c 'import sys; print(sys.version)' 2>/dev/null || true)"
  echo "shell:      ${SHELL:-?} / \$0=$0"
  echo "HOME:       ${HOME:-}"
  echo "GROK_HOME:  ${GROK_HOME:-}"
  echo "pwd:        $(pwd)"
  echo "git:        $(git --version 2>/dev/null || echo missing)"
  echo "tmux:       $(command -v tmux >/dev/null 2>&1 && tmux -V || echo missing)"
  echo "ps self:    $(ps -p $$ -o pid=,pgid=,lstart= 2>/dev/null || true)"
  echo "which omg:  $(command -v omg 2>/dev/null || echo 'not on PATH')"
}

_platform_diag
echo "== platform contracts ($("$PY" -c 'import sys; print(sys.version.split()[0])') on $(uname -s)) =="

# On any failure, re-print diagnostics for CI logs (#25 failure policy).
trap '_platform_diag; echo "PLATFORM_CONTRACTS_FAILED" >&2' ERR

# Core OS-sensitive surfaces — path by file list + platform-marked host tests.
# Not live / no Grok account.
"$PY" -m pytest -q -m "not live" --tb=short \
  tests/test_path_keys.py \
  tests/test_team_meta_mutate.py \
  tests/test_hook_install.py \
  tests/test_state.py \
  tests/test_platform_host.py \
  tests/test_project_root.py \
  tests/test_install_classifier.py \
  tests/test_update_uninstall.py \
  "$@"

# Hermetic setup/CLI smoke (uses tmp paths; HOME/GROK_HOME already hermetic in CI).
echo "== hermetic setup / CLI router smoke =="
"$PY" -m pytest -q -m "not live" --tb=short \
  tests/test_cli_router.py::test_setup_on_tmp_path \
  tests/test_cli_router.py::test_setup_idempotent_agents_marker \
  tests/test_cli_router.py::test_setup_no_global_rules_skips_install \
  "$@"

# tmux transport smoke only when tmux is present.
# When present, failures must fail the job.
if command -v tmux >/dev/null 2>&1; then
  echo "== tmux-present transport smoke =="
  "$PY" -m pytest -q -m "tmux and not live" --tb=short \
    tests/test_team_tmux_transport.py \
    "$@"
else
  echo "== tmux not installed; skip tmux marker group =="
fi

trap - ERR
echo "ALL_PLATFORM_CONTRACTS_OK"
