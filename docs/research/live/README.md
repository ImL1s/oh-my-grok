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

## Live Grok promotion smoke (quota; optional)

```bash
# Requires grok credentials + tmux
python3 scripts/live_team_smoke.py --live --workers 2 --goal $'1. a\n2. b'
# Expect last line only when all hard assertions pass:
# LIVE_TEAM_SMOKE_OK
```

`LIVE_TEAM_SMOKE_OK` is the Grok-live promotion proof. Fixture OK is transport
only — do not treat it as full multi-CLI or Grok model parity.
