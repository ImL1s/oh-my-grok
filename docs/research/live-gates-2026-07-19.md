# Live gates evidence — 2026-07-19

Quota-heavy live tests (real `grok -p` sessions). Not dry-run.

## 1. `omg ulw` (real agent)

| Field | Value |
|-------|-------|
| Project | `/var/folders/.../implementer/live/ulw-proj2` |
| Run | `20260719T180630Z-4ccceb23` |
| Exit | **0** |
| File | `live_ulw_ok.txt` → `LIVE-ULW-OK` |
| Argv | `grok … --prompt-file …/last_prompt.md` (YAML skill body safe) |
| Status | `completed`, `verified: false` (no CLI acceptance; expected) |

## 2. `omg ralph` (real agent)

| Field | Value |
|-------|-------|
| Project | `/tmp/omg-live-ralph-vvrbBA` |
| Run | `20260719T180855Z-68136262` |
| Exit | **0** |
| File | `live_ralph_ok.txt` → `LIVE-RALPH-OK` |
| Argv | `grok … --prompt-file …/last_prompt.md` |
| Status | `completed`, `verified: false` |
| Log | `/tmp/omg-live-ralph-run.log` |

## 3. Live PreToolUse canary (`scripts/canary_pretool.py --live`)

### 3a. Plugin-only (failed soft-gate)

- Status: `REAL_CLI_RAN_hook_did_not_block`
- Session `hook_execution` only: `global/settings` (Claude compat) — **plugin** PreToolUse not listed
- Real CLI: Claude Code `2.1.215`

### 3b. After `~/.grok/hooks/omg-pretool-deny.json` (pass)

- Status: **`marker_absent_ok`**, canary exit **0**
- Parent + child denied with: `oh-my-grok: external agent CLI blocked…`
- Session runs include `global/omg-pretool-deny` with status denied
- Evidence: `docs/research/canary-pretool-latest.json`
- Install path: `scripts/install-plugin.sh` now writes the global hook

## Residual (honest)

- PreToolUse remains **fail-open** on timeout/crash
- Primary isolation: `capability_mode` (no Execute for implementers)
- Plugin-bundled hooks alone were insufficient on this host; **global** hooks required for live deny
