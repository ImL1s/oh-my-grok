# Live / fixture team smoke evidence

Machine-local JSON under this directory is **gitignored** (regenerate locally).

## Fixture transport smoke (hermetic, no Grok quota)

```bash
# From repo root; requires tmux
PYTHONPATH=. python3 scripts/live_team_smoke.py --fixture-executor \
  --workers 2 --goal $'1. a\n2. b'
# Expect last line:
# FIXTURE_TEAM_SMOKE_OK
```

Proves: split panes, process-level `worker-ready`, mailbox ACK, stop.

## Live Grok promotion smoke (quota)

```bash
# Requires grok credentials + tmux
PYTHONPATH=. python3 scripts/live_team_smoke.py --live --workers 2 --goal $'1. a\n2. b'
# Expect last line only when all hard assertions pass:
# LIVE_TEAM_SMOKE_OK
```

`LIVE_TEAM_SMOKE_OK` is the Grok-live promotion proof. Fixture OK is transport
only — do not treat it as full multi-CLI or Grok model parity.

### Recorded local success

| When | Result | Notes |
|------|--------|--------|
| 2026-07-30 | **`LIVE_TEAM_SMOKE_OK`** | process-ready gate; `startup_status=running` (process=2); stop identity/session teardown verified. Machine JSON gitignored under this dir. |
