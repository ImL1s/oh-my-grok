---
name: omg-using
description: Bootstrap router for oh-my-grok. Use when user says omg, setup omg, how to use oh-my-grok, which skill, ulw vs ralph vs ralplan, or first-time install.
---

# omg-using — Bootstrap

Route users and sessions into the correct oh-my-grok workflow. This skill does **not** implement features; it loads the right playbook and points at install/health tools.

## HARD RULES (non-negotiable)
- Fan-out ONLY via Grok `spawn_subagent` (depth=1; children must NOT spawn).
- NEVER invoke claude/codex/omc team/agy/cursor-agent as default workers.
- Use Grok tool names: read_file, search_replace, run_terminal_command, spawn_subagent, grep, list_dir.
- Write-heavy work: isolation worktree + background true; wait with wait_commands_or_subagents / get_command_or_subagent_output.
- State: only omg CLI is authoritative for passes/verified; you may write proposals under .omg/artifacts/.

## When to load which skill

| Trigger keywords | Load skill | Mode |
|---|---|---|
| `ulw`, `ultrawork`, `parallel`, `fan-out` | `omg-ultrawork` | Parallel decompose → spawn → integrate → verify |
| `ralph`, `don't stop`, `keep going until done`, `persist until verified` | `omg-ralph` | One-story persistence iteration; outer CLI owns loop |
| `ralplan`, `plan consensus`, `critic plan`, `steelman plan` | `omg-ralplan` | Plan → critic → revise → verifier (no implementation) |
| `cancel`, `stop omg`, `abort run`, `kill workers` | `omg-cancel` | Cancel via `omg cancel` + PID files |
| `omg`, `setup omg`, `how to use`, first session | **this skill** | Bootstrap + doctor |

If multiple keywords appear, prefer: **cancel** > **ralplan** (planning not done) > **ralph** (durable) > **ulw** (parallel one-shot).

## Persistence model (not OMC Stop continuation)

**oh-my-grok does not force the chat to keep going via Stop hooks.**  
On Grok Build, **only `PreToolUse` can block**; `Stop` is passive (observe/log only). OMC-style `{decision:"block", reason:…}` on Stop **is not host-feasible** today (see `docs/research/stop-continuation/`).

| Want | Do this |
|------|---------|
| Don’t stop until verified | **`omg ralph "goal"`** (CLI outer loop owns max-iter) |
| Full plan→implement→accept | **`omg pipeline "goal"`** |
| Parallel fan-out | **`omg ulw "goal"`** (or pipeline `--implement ulw`) |
| Stop supervised run | **`omg cancel`** |

In-session skills (ralph/ulw) intentionally stop after **one unit of work**; the **CLI** re-launches. Do not invent infinite self-loops inside one TUI turn.

## Install + health

1. **Plugin install** (Grok Build): ensure this repo/`oh-my-grok` plugin is installed so `skills/omg-*` and `agents/omg-*` are visible.
2. **CLI**: ensure `omg` is on `PATH` (repo `bin/omg` or installed entry point).
3. **Doctor** (always before first real run):

```bash
omg doctor
```

Fix any FAIL before starting ulw/ralph/ralplan.

4. **Workspace setup** (creates `.omg/` dirs, gitignore fragment, AGENTS fragment):

```bash
omg setup
```

5. **State inspection**:

```bash
omg state
```

Never invent pass/verified status in chat — read `omg state` or `.omg/state/`.

## Bootstrap steps

1. Confirm plugin skills exist: `skills/omg-ultrawork`, `skills/omg-ralph`, `skills/omg-ralplan`, `skills/omg-cancel`.
2. Run `omg doctor` if `.omg/` missing or user is first-time.
3. Map user intent → skill table above.
4. Load the matching skill body and follow it.
5. Remind: workers = Grok `spawn_subagent` only; no external agent CLIs.

## Do not

- Start implementing under this skill alone — hand off to ulw/ralph/ralplan.
- Call claude/codex/omc team/agy/cursor-agent as workers.
- Mark runs verified yourself; only `omg` CLI owns that.
- Use self-matching `pkill -f` to stop runs — use `omg cancel` (see `omg-cancel`).
