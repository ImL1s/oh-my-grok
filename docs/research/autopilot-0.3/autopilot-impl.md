# Autopilot Impl Plan — Spawn fail-closed (Option A)

## Tasks

1. Extend `omg_cli/deny.py` with `decide_spawn_subagent` + wire into `decide_pre_tool_use`
2. Update `hooks/hooks.json` PreToolUse matcher
3. Tests in `tests/test_deny.py`
4. security-model + README note
5. pytest + smoke dry

## Out of scope this run

ULW auto-integrate, pipeline product polish (documented for 0.3.x)
