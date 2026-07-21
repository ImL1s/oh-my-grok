#!/usr/bin/env bash
# Live quota suite for oh-my-grok. Opt-in only. Not default CI.
# Usage:
#   ./scripts/live_suite.sh --quick
#   ./scripts/live_suite.sh --full
#   ./scripts/live_suite.sh --quota-heavy
#   OMG_LIVE_REQUIRE=1 ./scripts/live_suite.sh --quick   # fail if no grok
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export PYTHONPATH="${ROOT}${PYTHONPATH:+:$PYTHONPATH}"
export PATH="${HOME}/.grok/bin:${PATH}"
OMG=(python3 "${ROOT}/bin/omg")

MODE="quick"
KEEP=0
for a in "$@"; do
  case "$a" in
    --quick) MODE=quick ;;
    --full) MODE=full ;;
    --quota-heavy) MODE=quota-heavy ;;
    --keep) KEEP=1 ;;
    -h|--help)
      echo "live_suite.sh --quick|--full|--quota-heavy [--keep]"
      exit 0
      ;;
  esac
done

TS="$(date -u +%Y%m%dT%H%M%SZ)"
EVIDENCE="${OMG_LIVE_EVIDENCE_DIR:-$ROOT/docs/research/live}"
mkdir -p "$EVIDENCE"
LOG="$EVIDENCE/suite-$TS-$MODE.log"
exec > >(tee -a "$LOG") 2>&1

need_grok() {
  if ! command -v grok >/dev/null 2>&1; then
    if [[ "${OMG_LIVE_REQUIRE:-0}" == "1" ]]; then
      echo "FAIL: grok not on PATH" >&2
      exit 1
    fi
    echo "SKIP: grok not on PATH"
    exit 0
  fi
}

mkproj() {
  local d
  d="$(mktemp -d "${TMPDIR:-/tmp}/omg-live-$1.XXXXXX")"
  git -C "$d" init -q
  git -C "$d" config user.email "live@omg.test"
  git -C "$d" config user.name "omg-live"
  git -C "$d" config commit.gpgsign false
  printf 'base\n' >"$d/README.md"
  printf '.omg/\n' >"$d/.gitignore"
  git -C "$d" add README.md .gitignore
  git -C "$d" commit -qm init
  (cd "$d" && "${OMG[@]}" setup >/dev/null)
  echo "$d"
}

cleanup_list=()
trap '[[ ${KEEP:-0} -eq 1 ]] || rm -rf "${cleanup_list[@]:-}"' EXIT

fail() { echo "FAIL: $*" >&2; exit 1; }

need_grok
echo "== live_suite mode=$MODE ts=$TS =="

# Global hook preflight (honor $GROK_HOME)
GROK_HOME_DIR="${GROK_HOME:-$HOME/.grok}"
if [[ ! -f "${GROK_HOME_DIR}/hooks/omg-pretool-deny.json" ]]; then
  echo "WARN: global hook missing; running install-plugin.sh"
  bash "$ROOT/scripts/install-plugin.sh" || true
fi

echo "== L-CANARY =="
python3 "$ROOT/scripts/canary_pretool.py" --live \
  --timeout "${OMG_LIVE_TIMEOUT_CANARY:-180}" \
  -o "$EVIDENCE/canary-$TS.json" \
  || fail "canary live (see $EVIDENCE/canary-$TS.json)"

if [[ "$MODE" == "quick" || "$MODE" == "full" || "$MODE" == "quota-heavy" ]]; then
  ULW="$(mkproj ulw)"; cleanup_list+=("$ULW")
  echo "== L-ULW-1 $ULW =="
  GOAL="$(cat "$ROOT/scripts/fixtures/live/ulw_goal.txt")"
  (
    cd "$ULW"
    "${OMG[@]}" ulw "$GOAL" --max-iter 1 --timeout "${OMG_LIVE_TIMEOUT_ULW:-600}" \
      --no-require-acceptance --yolo
  ) || true
  grep -qx 'LIVE-ULW-OK' "$ULW/live_ulw_ok.txt" 2>/dev/null \
    || fail "L-ULW-1 missing LIVE-ULW-OK"
  cp -R "$ULW/.omg/state/runs" "$EVIDENCE/ulw-runs-$TS" 2>/dev/null || true

  RALPH="$(mkproj ralph)"; cleanup_list+=("$RALPH")
  echo "== L-RALPH-1 $RALPH =="
  GOAL="$(cat "$ROOT/scripts/fixtures/live/ralph_goal.txt")"
  (
    cd "$RALPH"
    "${OMG[@]}" ralph "$GOAL" --max-iter 1 --timeout "${OMG_LIVE_TIMEOUT_RALPH:-600}" \
      --no-require-acceptance --yolo
  ) || true
  grep -qx 'LIVE-RALPH-OK' "$RALPH/live_ralph_ok.txt" 2>/dev/null \
    || fail "L-RALPH-1 missing LIVE-RALPH-OK"

  # L-ACCEPT-1: write hermetic prd.json with [["true"]], then omg accept --yes
  echo "== L-ACCEPT-1 =="
  (
    cd "$RALPH"
    python3 - <<'PY'
import json
from pathlib import Path
from omg_cli.state import load_active_run

root = Path(".").resolve()
active = load_active_run(root)
if not active:
    raise SystemExit("L-ACCEPT-1: no active run after ralph")
run_id = active["run_id"]
prd_path = root / ".omg" / "state" / "runs" / run_id / "prd.json"
prd_path.parent.mkdir(parents=True, exist_ok=True)
prd = {
    "version": 1,
    "goal": "live suite accept gate",
    "stories": [
        {
            "id": "S-accept",
            "title": "hermetic true",
            "commands": [["true"]],
        }
    ],
    "global_commands": [],
}
prd_path.write_text(json.dumps(prd, indent=2) + "\n", encoding="utf-8")
print(f"L-ACCEPT-1 wrote {prd_path}")
PY
    "${OMG[@]}" accept --yes
    python3 - <<'PY'
import json
from pathlib import Path
from omg_cli.state import load_active_run, load_run

root = Path(".").resolve()
active = load_active_run(root)
assert active, "no active run"
data = load_run(root, active["run_id"])
assert data and data.get("verified") is True, f"expected verified true, got {data}"
print("L-ACCEPT-1 verified=true OK")
PY
  ) || fail "L-ACCEPT-1 accept/verified failed"
fi

if [[ "$MODE" == "full" || "$MODE" == "quota-heavy" ]]; then
  DUAL="$(mkproj dual)"; cleanup_list+=("$DUAL")
  echo "== L-DUAL-1 $DUAL =="
  GOAL="$(cat "$ROOT/scripts/fixtures/live/dual_goal.txt")"
  set +e
  (
    cd "$DUAL"
    "${OMG[@]}" dual-review "$GOAL" --timeout "${OMG_LIVE_TIMEOUT_DUAL:-600}" --yolo
  )
  dual_rc=$?
  set -e
  # verified must remain false on any run state (grep for portability; rg may be absent)
  if grep -Rsn '"verified": true' "$DUAL/.omg" 2>/dev/null; then
    fail "L-DUAL-1 must not set verified true"
  fi
  # Semantic gate (Codex P0): dual_review.json must exist; verdict must never be
  # APPROVE when stage exit_code != 0; stub/NEEDS_REVIEW must not be APPROVE.
  python3 - "$DUAL" "$dual_rc" <<'PY' || fail "L-DUAL-1 semantic verdict gate failed"
import json, sys
from pathlib import Path
root = Path(sys.argv[1])
dual_rc = int(sys.argv[2])
states = list(root.glob(".omg/state/runs/*/dual_review.json"))
if not states:
    # dry/missing state is a hard fail for full/quota-heavy suite
    print("L-DUAL-1 FAIL: missing dual_review.json", file=sys.stderr)
    sys.exit(1)
data = json.loads(states[0].read_text(encoding="utf-8"))
verdict = (data.get("verdict") or "UNKNOWN").upper()
history = data.get("history") or []
print(f"L-DUAL-1 dual_rc={dual_rc} verdict={verdict} history_n={len(history)}")
# Never stamp APPROVE if any stage reported non-zero exit
for h in history:
    ec = h.get("exit_code")
    if ec not in (None, 0) and verdict == "APPROVE":
        print(f"L-DUAL-1 FAIL: stage exit {ec} but verdict=APPROVE", file=sys.stderr)
        sys.exit(1)
# dual CLI exit 0 must match APPROVE only
if dual_rc == 0 and verdict != "APPROVE":
    print(f"L-DUAL-1 FAIL: dual-review exit 0 but verdict={verdict}", file=sys.stderr)
    sys.exit(1)
if dual_rc != 0 and verdict == "APPROVE":
    print("L-DUAL-1 FAIL: dual-review non-zero exit but verdict=APPROVE", file=sys.stderr)
    sys.exit(1)
# verifier artifact must not be a dry_run stub while claiming APPROVE
for p in root.glob(".omg/state/runs/*/stages/dual-verifier-*.md"):
    text = p.read_text(encoding="utf-8", errors="replace")
    low = text.lower()
    if ("dry_run stub" in low or "needs_review" in low) and "APPROVE" in text:
        # free-floating APPROVE in instructions OK; terminal APPROVE line is bad with stub
        for line in text.splitlines():
            if line.strip() in ("APPROVE", "## Verdict") or line.strip().endswith("APPROVE"):
                if line.strip() == "APPROVE" or line.strip().endswith(": APPROVE"):
                    print(f"L-DUAL-1 FAIL: stub-like artifact with terminal APPROVE: {p}", file=sys.stderr)
                    sys.exit(1)
print("L-DUAL-1 semantic OK")
PY
  find "$DUAL/.omg" -name '*dual*' 2>/dev/null | head -5 || true
fi

if [[ "$MODE" == "quota-heavy" ]]; then
  CAP="$(mkproj cap)"; cleanup_list+=("$CAP")
  echo "== L-CAP-SPAWN $CAP =="
  GOAL="$(cat "$ROOT/scripts/fixtures/live/cap_spawn_goal.txt")"
  (
    cd "$CAP"
    "${OMG[@]}" ulw "$GOAL" --max-iter 1 --timeout "${OMG_LIVE_TIMEOUT_ULW:-900}" \
      --no-require-acceptance --yolo || true
  )
  test -f "$CAP/live_cap_spawn_report.txt" || fail "L-CAP-SPAWN missing report"
  if grep -qi 'DENIED_OR_RAN=ran' "$CAP/live_cap_spawn_report.txt"; then
    # ran is fail for soft-gate path unless capability blocked before shell
    echo "WARN: child reported ran — check capability_mode; soft-gate may have failed"
    # Hard fail if real version string without deny:
    if grep -qi 'claude code' "$CAP/live_cap_spawn_report.txt"; then
      fail "L-CAP-SPAWN real CLI evidence"
    fi
  fi
  cp "$CAP/live_cap_spawn_report.txt" "$EVIDENCE/cap-spawn-$TS.txt"

  echo "== L-CANCEL (optional long run) =="
  # Start a dry-looking long goal then cancel — best effort
  CANC="$(mkproj canc)"; cleanup_list+=("$CANC")
  (
    cd "$CANC"
    "${OMG[@]}" ralph "Sleep-like long task: do nothing useful for a long time; only read files." \
      --max-iter 1 --timeout 120 --no-require-acceptance --yolo &
    echo $! >"$CANC/suite_parent.pid"
    sleep 8
    "${OMG[@]}" cancel --grace 2 || true
    wait || true
  )
fi

python3 - <<PY
import json
from pathlib import Path
p = Path("$EVIDENCE") / "suite-$TS-$MODE.summary.json"
p.write_text(json.dumps({
  "ts_utc": "$TS",
  "mode": "$MODE",
  "log": "$LOG",
  "status": "ok",
}, indent=2) + "\n", encoding="utf-8")
print("wrote", p)
PY

echo "live_suite OK mode=$MODE evidence=$EVIDENCE"
