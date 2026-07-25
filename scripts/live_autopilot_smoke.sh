#!/usr/bin/env bash
# Autopilot Stop-pin / outer-run smoke. Opt-in live sections; dry path is hermetic.
#
# Usage:
#   ./scripts/live_autopilot_smoke.sh              # dry-run + hook gate only
#   OMG_LIVE=1 ./scripts/live_autopilot_smoke.sh   # also attempt live grok probes
#   OMG_LIVE_REQUIRE=1 OMG_LIVE=1 ./scripts/live_autopilot_smoke.sh  # fail if no grok
#
# Evidence (live): docs/research/live/autopilot-smoke-<ts>/
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export PYTHONPATH="${ROOT}${PYTHONPATH:+:$PYTHONPATH}"
export PATH="${HOME}/.local/bin:${HOME}/.grok/bin:${PATH}"
OMG=(python3 "${ROOT}/bin/omg")
PY=(python3)
if [[ -x "${ROOT}/.venv/bin/python" ]]; then
  PY=("${ROOT}/.venv/bin/python")
fi

LIVE="${OMG_LIVE:-0}"
KEEP="${OMG_KEEP:-0}"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
EVIDENCE_ROOT="${OMG_LIVE_EVIDENCE_DIR:-$ROOT/docs/research/live}"
EVIDENCE="$EVIDENCE_ROOT/autopilot-smoke-$TS"
mkdir -p "$EVIDENCE"

fail() { echo "FAIL: $*" >&2; exit 1; }
ok() { echo "OK: $*"; }

cleanup_list=()
trap '[[ "$KEEP" == "1" ]] || rm -rf "${cleanup_list[@]:-}"' EXIT

mkproj() {
  local d
  d="$(mktemp -d "${TMPDIR:-/tmp}/omg-ap-smoke.XXXXXX")"
  cleanup_list+=("$d")
  git -C "$d" init -q
  git -C "$d" config user.email "smoke@omg.test"
  git -C "$d" config user.name "omg-smoke"
  git -C "$d" config commit.gpgsign false
  printf 'base\n' >"$d/README.md"
  printf '.omg/\n' >"$d/.gitignore"
  git -C "$d" add README.md .gitignore
  git -C "$d" commit -qm init
  (cd "$d" && "${OMG[@]}" setup >/dev/null)
  echo "$d"
}

echo "== live_autopilot_smoke ts=$TS live=$LIVE =="
echo "evidence: $EVIDENCE"

# --- A) Hermetic dry-run outer driver ---
echo "== A dry-run: omg autopilot run --dry-run =="
PROJ="$(mkproj)"
(
  cd "$PROJ"
  set -euo pipefail
  out="$EVIDENCE/dry-run.out"
  "${OMG[@]}" autopilot run "add pure add(a,b) with unit test" \
    --skip-interview --dry-run --max-phase-cycles 2 >"$out" 2>&1 || true
  # dry-run should create an autopilot run and leave RESUME or status readable
  "${OMG[@]}" state --json >"$EVIDENCE/dry-state.json" 2>/dev/null \
    || "${OMG[@]}" state >"$EVIDENCE/dry-state.txt" 2>/dev/null || true
  if ! ls .omg/state/runs/*/status.json >/dev/null 2>&1; then
    fail "dry-run did not create .omg/state/runs/*/status.json"
  fi
  # argv / dry-run marker if present in run dir
  if ls .omg/state/runs/*/argv.json >/dev/null 2>&1 \
    || ls .omg/state/runs/*/launch*.json >/dev/null 2>&1 \
    || grep -qi 'dry' "$out" 2>/dev/null; then
    ok "dry-run left launch/argv or dry marker"
  else
    # still OK if status exists — some dry paths only stamp status
    ok "dry-run created run state (no argv file — acceptable)"
  fi
  cp -R .omg/state/runs "$EVIDENCE/dry-runs" 2>/dev/null || true
)
ok "section A"

# --- B) Hermetic Stop gate via hook subprocess ---
echo "== B Stop hook blocks incomplete autopilot =="
PROJ2="$(mkproj)"
(
  cd "$PROJ2"
  set -euo pipefail
  "${OMG[@]}" autopilot start "gate probe" --skip-interview >/dev/null
  rid="$("${PY[@]}" - <<'PY'
import json
from pathlib import Path
active = json.loads(Path(".omg/state/active.json").read_text())
print(active["run_id"])
PY
)"
  # force implement phase without completing gates (direct status merge for probe)
  "${PY[@]}" - <<PY
import json
from pathlib import Path
from omg_cli.state import merge_status_fields
rid = "$rid"
merge_status_fields(Path("."), rid, {"autopilot_phase": "implement"})
PY
  export GROK_WORKSPACE_ROOT="$PROJ2"
  export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
  payload='{"reason":"end_turn","stopHookActive":false,"backgroundTasks":[]}'
  out="$EVIDENCE/stop-hook.out"
  echo "$payload" | "${PY[@]}" "$ROOT/hooks/bin/stop.py" >"$out" 2>"$EVIDENCE/stop-hook.err" || true
  "${PY[@]}" - <<PY
import json, sys
from pathlib import Path
raw = Path("$out").read_text().strip()
assert raw, "Stop hook produced empty stdout (expected decision:block)"
data = json.loads(raw)
assert data.get("decision") == "block", data
assert "implement" in (data.get("reason") or "").lower() or "do not ask" in (data.get("reason") or "").lower(), data
print("block_ok", data["decision"])
PY
  # session-end must allow stop
  echo '{"reason":"shutdown"}' | "${PY[@]}" "$ROOT/hooks/bin/stop.py" >"$EVIDENCE/stop-shutdown.out" 2>/dev/null || true
  [[ -z "$(cat "$EVIDENCE/stop-shutdown.out")" ]] || fail "shutdown should not block"
  # verified phase allows stop
  "${PY[@]}" - <<PY
from pathlib import Path
from omg_cli.state import merge_status_fields
merge_status_fields(Path("."), "$rid", {"autopilot_phase": "verified"})
PY
  echo '{"reason":"end_turn"}' | "${PY[@]}" "$ROOT/hooks/bin/stop.py" >"$EVIDENCE/stop-verified.out" 2>/dev/null || true
  [[ -z "$(cat "$EVIDENCE/stop-verified.out")" ]] || fail "verified should not block"
)
ok "section B"

# --- C) set-host handoff ---
echo "== C omg goal set-host handoff =="
PROJ3="$(mkproj)"
(
  cd "$PROJ3"
  set -euo pipefail
  stories='[{"id":"s1","title":"T","acceptance":"unit test passes","depends_on":[]}]'
  "${OMG[@]}" goal init --goal g-smoke --stories-json "$stories" >/dev/null
  "${OMG[@]}" goal set-host --goal g-smoke >"$EVIDENCE/set-host.md"
  grep -q '/goal' "$EVIDENCE/set-host.md" || fail "set-host missing /goal"
  grep -q 'snapshot.json' "$EVIDENCE/set-host.md" || fail "set-host missing snapshot pointer"
  grep -qi 'replace' "$EVIDENCE/set-host.md" || fail "set-host missing replace warning"
)
ok "section C"

# --- D) analyze-only refuse (hermetic) ---
echo "== D analyze-only refuse for autopilot =="
"${PY[@]}" - <<'PY'
from omg_cli.command_policy import is_analyze_only
assert is_analyze_only([["flutter", "analyze", "lib"], ["flutter", "test", "test/a_test.dart"]]) is True
assert is_analyze_only([["python3", "-m", "pytest", "-q"]]) is False
print("analyze_only_ok")
PY
ok "section D"

# --- E) Optional live grok probe ---
if [[ "$LIVE" != "1" ]]; then
  echo "== E live skipped (set OMG_LIVE=1 to enable) =="
  echo "ALL_AUTOPILOT_SMOKE_DRY_OK"
  exit 0
fi

need_grok() {
  if ! command -v grok >/dev/null 2>&1; then
    if [[ "${OMG_LIVE_REQUIRE:-0}" == "1" ]]; then
      fail "grok not on PATH"
    fi
    echo "SKIP live: grok not on PATH"
    echo "ALL_AUTOPILOT_SMOKE_DRY_OK"
    exit 0
  fi
}
need_grok

echo "== E live: headless grok autopilot (happy path) =="
if [[ "${OMG_LIVE_SKIP_HAPPY:-0}" == "1" ]]; then
  echo "SKIP happy path (OMG_LIVE_SKIP_HAPPY=1)"
else
PROJ4="$(mkproj)"
(
  cd "$PROJ4"
  set -euo pipefail
  "${OMG[@]}" autopilot start \
    "Add a pure Python function add(a,b) and a pytest. Keep working; do not ask questions." \
    --skip-interview >"$EVIDENCE/live-start.json"
  rid="$("${PY[@]}" - <<'PY'
import json
from pathlib import Path
print(json.loads(Path(".omg/state/active.json").read_text())["run_id"])
PY
)"
  timeout_s="${OMG_LIVE_TIMEOUT:-180}"
  set +e
  timeout "$timeout_s" grok -p \
    "You are in an oh-my-grok autopilot run $rid. Drive phases to verified: implement add(a,b)+pytest, structured review, ultraqa, omg autopilot complete. Do not ask the user. Always set capability_mode on spawn_subagent." \
    >"$EVIDENCE/live-grok.out" 2>"$EVIDENCE/live-grok.err"
  rc=$?
  set -e
  echo "live_grok_rc=$rc" | tee "$EVIDENCE/live-rc.txt"
  cp -R .omg/state/runs "$EVIDENCE/live-runs" 2>/dev/null || true
  "${OMG[@]}" state --json >"$EVIDENCE/live-state.json" 2>/dev/null || true
  # Record verified + non-analyze-only accept when present
  "${PY[@]}" - <<PY
import json
from pathlib import Path
from omg_cli.command_policy import is_analyze_only
rid = "$rid"
st = json.loads(Path(f".omg/state/runs/{rid}/status.json").read_text())
verified = bool(st.get("verified")) and st.get("autopilot_phase") == "verified"
Path("$EVIDENCE/live-verified.txt").write_text(
    f"LIVE_AUTOPILOT_VERIFIED={'1' if verified else '0'}\n", encoding="utf-8"
)
print("LIVE_AUTOPILOT_VERIFIED", int(verified))
man_path = Path(f".omg/state/runs/{rid}/acceptance.manifest.json")
if man_path.is_file():
    man = json.loads(man_path.read_text())
    cmds = man.get("commands") or []
    if cmds and isinstance(cmds[0], str):
        cmds = [c.split() for c in cmds]
    soft = is_analyze_only(cmds) if cmds else False
    Path("$EVIDENCE/live-accept-analyze-only.txt").write_text(
        f"ANALYZE_ONLY={'1' if soft else '0'}\ncommands={cmds!r}\n",
        encoding="utf-8",
    )
    print("ANALYZE_ONLY", int(soft))
    if verified and soft:
        raise SystemExit("verified via analyze-only acceptance — soft false-green")
PY
)
fi

echo "== E2 live: force incomplete implement + chatty stop (Stop pin) =="
PROJ5="$(mkproj)"
(
  cd "$PROJ5"
  set -euo pipefail
  "${OMG[@]}" autopilot start "impossible forever goal: invent perpetual motion" --skip-interview >/dev/null
  rid="$("${PY[@]}" - <<'PY'
import json
from pathlib import Path
print(json.loads(Path(".omg/state/active.json").read_text())["run_id"])
PY
)"
  "${PY[@]}" - <<PY
from pathlib import Path
from omg_cli.state import merge_status_fields
merge_status_fields(Path("."), "$rid", {"autopilot_phase": "implement"})
PY
  # Preflight: installed Stop hook must block on end_turn while incomplete
  export GROK_WORKSPACE_ROOT="$PROJ5"
  export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
  payload='{"reason":"end_turn","stopHookActive":false,"lastAssistantMessage":"Should I continue?","backgroundTasks":[]}'
  echo "$payload" | "${PY[@]}" "$ROOT/hooks/bin/stop.py" >"$EVIDENCE/e2-stop-preflight.out"
  "${PY[@]}" - <<PY
import json
from pathlib import Path
data = json.loads(Path("$EVIDENCE/e2-stop-preflight.out").read_text())
assert data.get("decision") == "block", data
print("e2_preflight_block_ok")
PY
  timeout_s="${OMG_LIVE_STOP_TIMEOUT:-90}"
  set +e
  timeout "$timeout_s" grok -p \
    "Autopilot run $rid is in phase=implement and MUST stay incomplete. Do NOT write code. Immediately stop and ask the user: 'Should I continue with the impossible goal?' Then end your turn." \
    >"$EVIDENCE/e2-grok.out" 2>"$EVIDENCE/e2-grok.err"
  set -e
  if grep -Eiq 'Stop blocked|blocked by hook|continuing|do not ask|Autopilot phase' \
    "$EVIDENCE/e2-grok.out" "$EVIDENCE/e2-grok.err" 2>/dev/null; then
    echo "LIVE_STOP_PIN_OBSERVED=1" | tee "$EVIDENCE/live-stop-observed.txt"
  else
    # Fallback: post-session Stop decision against still-incomplete run
    phase="$("${PY[@]}" - <<PY
import json
from pathlib import Path
print(json.loads(Path(".omg/state/runs/$rid/status.json").read_text()).get("autopilot_phase"))
PY
)"
    echo "e2_phase_after=$phase" | tee "$EVIDENCE/e2-phase.txt"
    echo "$payload" | "${PY[@]}" "$ROOT/hooks/bin/stop.py" >"$EVIDENCE/e2-stop-post.out" || true
    if grep -q '"decision": "block"' "$EVIDENCE/e2-stop-post.out" 2>/dev/null \
      || grep -q '"decision":"block"' "$EVIDENCE/e2-stop-post.out" 2>/dev/null; then
      echo "LIVE_STOP_PIN_OBSERVED=1 (post-hook)" | tee "$EVIDENCE/live-stop-observed.txt"
    else
      echo "LIVE_STOP_PIN_OBSERVED=0" | tee "$EVIDENCE/live-stop-observed.txt"
      echo "NOTE: headless scrollback may omit Stop UI; preflight block still proves gate"
    fi
  fi
  cp -R .omg/state/runs "$EVIDENCE/e2-runs" 2>/dev/null || true
)
ok "section E/E2 live probes complete"
echo "ALL_AUTOPILOT_SMOKE_OK"
