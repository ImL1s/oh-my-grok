# Council v0.2.1 → v0.2.2 synthesis (orchestrator)

**Date:** 2026-07-19  
**Inputs:** Fable plan (`.omg/research/council-v021/fable-plan.md`), Grok isolation research, ask-pipeline design, parallel-no-tmux design.  
**Codex:** may still be writing `codex-plan.md` (long max run); synthesis does not wait.

## Fable plan — short summary

Fable (READ-ONLY council) concluded **20 gaps are closable**: 17 via existing host mechanisms, 3 via alternate mechanisms (honest soft-gate for hooks, no-tmux process supervisor instead of panes, persona `[[inputs]]/[[outputs]]` when host supports).

**Three host facts that change residual story:**

1. **Subagents inherit PreToolUse** (`subagent/mod.rs` hook_registry) — still **fail-open**.
2. **`capability_mode: read-write` drops Execute** — workers without shell cannot launch external agent CLIs. Primary isolation, not regex.
3. Hooks deny when healthy; timeout/crash/missing binary are fail-open (documented, never market as hard sandbox).

**Workstreams (Fable order):** P0 flock/allowlist/pid/integrate → P1 doctor/smoke/capability argv clamp/no-tmux → P2 ask/pipeline/dual-review/persona.

## Isolation stack (ship narrative)

| Layer | Role |
|-------|------|
| capability_mode RO/RW | Hard toolset filter (no shell) |
| agent disallowedTools | depth=1 + extra denials |
| parent `--disallowed-tools` | Opt-in argv clamp (`disallow_shell` / dual-review + ralplan RO stages) |
| PreToolUse deny | Soft leader/child gate |
| omg accept allowlist | Only CLI runs tests |
| omg ask | User-only external advisors |

## Parallel without tmux

| Path | When | Mechanism |
|------|------|-----------|
| **skill** (default) | `omg ulw` | One `grok -p` leader + `spawn_subagent` |
| **process** (opt-in) | `omg ulw --fanout process --workers N` | N× independent `grok -p`; PIDs under `workers/*.pid.json`; cancel multi-PID |

## Implemented in tree

### v0.2.1
- P0: allowlist, flock, pid starttime, integrate whitelist + base..head, smoke
- P1: `omg ask`, `omg pipeline`, `omg dual-review`, skills

### v0.2.2
- `build_grok_argv(disallow_shell=…)` + `OMG_DISALLOW_SHELL=1`; dual-review + ralplan critic/verifier inject `--disallowed-tools run_terminal_command` (**not** ulw/ralph leaders)
- `omg ulw --fanout process --workers N` dry_run multi-PID skeleton (`omg_cli/fanout.py`)
- Research docs under `docs/research/` (`.omg/` stays gitignored)

## Honest residuals

- Hooks fail-open → never market as hard sandbox
- Leader with shell still has soft-gate only
- External advisor quality depends on provider flags (codex `-s read-only` etc.)
- Process fanout MVP: shared goal slices + argv/pid skeleton; worktree provisioning / tasks.json auto-decompose remain follow-ups
