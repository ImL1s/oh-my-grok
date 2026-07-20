# Plan 004: Strip OMG_ALLOW_* from process-fanout child env

> **Drift check**: `git diff --stat 997bcce..HEAD -- omg_cli/fanout.py omg_cli/evidence.py omg_cli/modes.py omg_cli/qa.py tests/test_fanout.py`

## Status

- **Priority**: P1
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none
- **Category**: security
- **Planned at**: commit `997bcce`, 2026-07-21

## Why this matters

Supervised Grok launches use `safe_supervised_child_env` to strip lifecycle escape vars (`OMG_ALLOW_*`). Process fanout (`OMG_EXPERIMENTAL_PROCESS_FANOUT=1`) copies raw `os.environ`, so a parent with `OMG_ALLOW_EXTERNAL_CLI=1` infects every worker. Isolation story says these allows are child-scoped (ask) only.

## Current state

- `omg_cli/fanout.py` ~163–166:
  ```python
  popen_kwargs: dict[str, Any] = {
      "cwd": str(cwd),
      "env": os.environ.copy(),
  }
  ```
- `omg_cli/evidence.py` ~504–513 — `safe_supervised_child_env` removes keys starting with `OMG_ALLOW_` and `OMG_ALLOW_UNSAFE_SPAWN`.
- `omg_cli/modes.py` ~586–590 — normal launch uses sanitizer.
- `omg_cli/qa.py` ~178 — also `os.environ.copy()` (address in plan 005 if not here; at least fanout in this plan).

## Commands

| Purpose | Command | Expected |
|---------|---------|----------|
| Fanout tests | `python -m pytest -q tests/test_fanout.py --tb=short` | exit 0 |
| Full | `python -m pytest -q -m "not live" --tb=short` | exit 0 |

## Scope

**In scope**:
- `omg_cli/fanout.py`
- `tests/test_fanout.py`

**Out of scope**:
- Making process fanout the default
- Full acceptance env scrub (plan 005)
- Disabling worker shell for fanout (documented residual)

## Steps

### Step 1: Use sanitizer

```python
from omg_cli.evidence import safe_supervised_child_env
...
"env": safe_supervised_child_env(os.environ),
```

### Step 2: Test

Monkeypatch or capture Popen kwargs (existing MagicMock style in `test_fanout.py`): set `os.environ["OMG_ALLOW_EXTERNAL_CLI"]="1"` and `OMG_ALLOW_UNSAFE_SPAWN=1`, run dry_run or mocked spawn, assert child env lacks those keys.

### Step 3: Full hermetic pytest

## Done criteria

- [ ] Fanout child env has no `OMG_ALLOW_*`
- [ ] Test locks it
- [ ] Full hermetic green
- [ ] README DONE

## STOP conditions

- Fanout workers intentionally need an allow var — contradict security-model; STOP.

## Maintenance notes

- Any new subprocess supervisor must use `safe_supervised_child_env`.
