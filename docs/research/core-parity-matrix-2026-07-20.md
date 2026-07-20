# OMG core-purpose parity matrix (post G001–G011 land)

**Disposition:** Grok-native core purpose parity — **not** full OMC surface clone.

| Surface | Status | Evidence |
|---------|--------|----------|
| Evidence / proposal stamp (S1) | HAVE | `omg_cli/evidence.py`, `tests/test_evidence.py` |
| Session lease / cancel (S2) | HAVE | `omg_cli/host_session.py`, `state.py`, `tests/test_host_session.py` |
| Ralplan v2 (S3) | HAVE | `omg_cli/ralplan.py`, `tests/test_v2_regression_locks.py` |
| Deep interview (S4) | HAVE | `omg_cli/interview.py`, `skills/omg-deep-interview` |
| Goal ledger + repair (S5) | HAVE | `omg_cli/goals.py`, `tests/test_goals.py` |
| Ownership + join ULW (S6) | HAVE | `workers.build_ownership_manifest` / `join_worker_results` |
| Structured review (S7) | HAVE | `omg_cli/review.py`, `tests/test_review.py` |
| UltraQA (S7) | HAVE | `omg_cli/qa.py`, `tests/test_qa.py` |
| Autopilot v2 (S8) | HAVE | `omg_cli/autopilot.py`, `tests/test_autopilot.py` |
| HUD / wiki / notifications / tmux team | NEVER (scope) | Non-goal in plan |
| Stop hard-pin | NEVER (host) | Soft PreToolUse only |
| Full OMC skill surface | MISSING | Intentional |

## Claims not made

- Full oh-my-claude-code parity
- Malicious same-user filesystem integrity
- Hard sandbox beyond capability_mode
- Live native 2-worker host proof without fingerprints (see live report)
