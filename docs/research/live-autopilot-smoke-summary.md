# Autopilot live smoke evidence summary (2026-07-26)

Gated script: `scripts/live_autopilot_smoke.sh`  
Raw evidence dirs under `docs/research/live/autopilot-smoke-*` (gitignored).

## Results

| Probe | Result |
|-------|--------|
| Hermetic dry-run `omg autopilot run --dry-run` | **PASS** |
| Stop hook `decision:block` on incomplete autopilot `end_turn` | **PASS** |
| Stop allows `shutdown` / `verified` | **PASS** |
| `omg goal set-host` handoff (`/goal` + snapshot pointer) | **PASS** |
| `is_analyze_only` heuristic | **PASS** |
| Live happy path (headless `grok -p`, ~100s) | **PASS** → `verified:true`, accept cmds `python3 -m pytest -q` (**not** analyze-only) |
| Live E2 Stop pin (incomplete implement + chatty stop) | **PASS** — preflight block + `LIVE_STOP_PIN_OBSERVED=1 (post-hook)` |

## Commands used

```bash
./scripts/install-plugin.sh && omg setup && omg doctor
bash scripts/live_autopilot_smoke.sh                    # dry
OMG_LIVE=1 bash scripts/live_autopilot_smoke.sh         # full live
OMG_LIVE=1 OMG_LIVE_SKIP_HAPPY=1 bash scripts/live_autopilot_smoke.sh  # Stop-pin focus
OMG_E2E=1 bash scripts/smoke.sh
PYTHONPATH=. .venv/bin/python -m pytest -q -m "not live"  # 1356 passed
```

## Honesty

- Headless scrollback often omits host “Stop blocked by hook” UI chrome; gate correctness is proven by hook stdout `decision:block` against active incomplete autopilot.
- Happy-path live run reached `verified` without needing Stop reinjection (model completed gates in one session).
- Cap-8 / interview-pause interactive UX not fully exercised in this pass (deferred finer live).
