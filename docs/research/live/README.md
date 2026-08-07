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

Proves: split panes, schema-v2 provider-ready supervisor (#99), mailbox ACK
enrichment, stop. Legacy `worker-ready` v1 receipts are not sufficient.

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
| 2026-07-30 | **`LIVE_TEAM_SMOKE_OK`** | historical process-ready gate (pre-#99). |
| 2026-08-07 | **#99 provider-ready** | Supervisor schema-v2 phases; v1 `worker-ready` cannot claim `running`. Re-run smoke after merge. |
