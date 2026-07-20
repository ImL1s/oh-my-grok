---
name: omg-using
description: >
  Bootstrap router for oh-my-grok. Use when user says omg, setup omg, how to use
  oh-my-grok, which skill, ulw vs ralph vs ralplan vs autopilot vs ultragoal, or
  first-time install.
---

# omg-using — Bootstrap

Route users and sessions into the correct oh-my-grok workflow. This skill does **not** implement features; it loads the right playbook and points at install/health tools.

**Human catalog (all 15 skills):** `docs/skills.md` · `docs/skills.zh-Hant.md`  
**Docs index:** `docs/README.md` · `docs/README.zh-Hant.md` · user README: `README.zh-TW.md`

## HARD RULES (non-negotiable)
- Fan-out ONLY via Grok `spawn_subagent` (depth=1; children must NOT spawn).
- **Always set `capability_mode` on spawn** (`read-only` for explore/plan/critic/verifier; `read-write` for implementers).
- **If spawn is DENIED for capability_mode: RETRY IMMEDIATELY** same turn with the required mode.
  Do **not** abandon multi-agent; do **not** fall back to solo-only after one deny.
- NEVER invoke claude/codex/omc team/agy/cursor-agent as default workers.
- Use Grok tool names: read_file, search_replace, run_terminal_command, spawn_subagent, grep, list_dir.
- Write-heavy work: isolation worktree + background true; wait with wait_commands_or_subagents / get_command_or_subagent_output.
- State: only omg CLI is authoritative for passes/verified; you may write proposals under .omg/artifacts/.
- **RESUME.md first:** After session start / user says continue, `read_file` `.omg/state/RESUME.md` if present. If `resumable: true`, do **not** invent a new run — follow printed commands or run `omg resume`, then `omg resume --clear` after successfully continuing.

## When to load which skill

| Trigger keywords | Load skill | Mode |
|---|---|---|
| `autopilot`, `auto pilot`, `full auto`, `autonomous`, `build me`, `create me`, `make me`, `handle it all`, end-to-end lifecycle | `omg-autopilot` | Session playbook driving CLI phases interview→…→verified |
| `resume`, `continue run`, `RESUME.md`, mid-session continuity | `omg-using` + **`omg resume`** | Read RESUME.md; CLI smart route |
| `ultragoal`, `goal ledger`, `multi-story durable`, `resume goal`, `omg goal` (multi-story) | `omg-ultragoal` | Durable multi-story ledger; CLI `omg goal *`; no host `/goal` |
| `deep interview`, `clarify requirements`, `ambiguity` | `omg-deep-interview` | Socratic CLI interview gate |
| `ultraqa`, `QA loop`, `fix failing tests`, retest | `omg-ultraqa` | Bounded QA freeze→run→repair |
| `wiki`, project memory, capture decision | `omg-wiki` | `.omg/wiki` markdown knowledge |
| `hud`, statusline | `omg-hud` | One-line `omg hud` |
| `lsp`, go-to-definition, symbols | `omg-lsp` | Honest probes; prefer grep/read_file |
| `ulw`, `ultrawork`, `parallel`, `fan-out` | `omg-ultrawork` | Parallel decompose → spawn → integrate → verify |
| `ralph`, `don't stop`, `keep going until done`, `persist until verified` | `omg-ralph` | One-story persistence iteration; outer CLI owns loop |
| `ralplan`, `plan consensus`, `critic plan`, `steelman plan` | `omg-ralplan` | Plan → critic → revise → verifier (no implementation) |
| `cancel`, `stop omg`, `abort run`, `kill workers` | `omg-cancel` | Cancel via `omg cancel` + PID files |
| `omg`, `setup omg`, `how to use`, first session | **this skill** | Bootstrap + doctor |

If multiple keywords appear, prefer: **cancel** > **ralplan** (planning not done) > **autopilot** (full lifecycle) > **ultragoal** (durable multi-story ledger) > **ralph** (durable one-story) > **ulw** (parallel one-shot).

## Persistence model (not OMC Stop continuation)

**oh-my-grok does not force the chat to keep going via Stop hooks.**  
On Grok Build, **only `PreToolUse` can block**; `Stop` is passive (observe/log only). OMC-style `{decision:"block", reason:…}` on Stop **is not host-feasible** today (see `docs/research/stop-continuation/`).

| Want | Do this |
|------|---------|
| Don’t stop until verified | **`omg ralph "goal"`** (CLI outer loop owns max-iter) |
| Continue after chat ended | **`omg resume`** (+ read `.omg/state/RESUME.md`) then mode CLI |
| Full phase coordinator **in-session** | **`omg-autopilot` skill** + `omg autopilot *` CLI (re-invoke / “continue” if turn ends) |
| Durable multi-story ledger (no host `/goal`) | **`omg-ultragoal` skill** + `omg goal *` (status → next story → link-run → verify) |
| Clarify vague requirements | **`omg-deep-interview`** / `omg interview *` |
| Bounded test-fix loop | **`omg-ultraqa`** / `omg qa *` |
| Full plan→implement→accept (CLI FSM) | **`omg pipeline "goal"`** |
| Parallel fan-out | **`omg ulw "goal"`** (or pipeline `--implement ulw`) |
| Project wiki / HUD / LSP probe | **`omg wiki` / `omg hud` / `omg lsp`** |
| Stop supervised run | **`omg cancel`** |

In-session skills (ralph/ulw) intentionally stop after **one unit of work**; the **CLI** re-launches. Autopilot is multi-phase **within** the session playbook but still cannot Stop-pin the chat — re-load `omg-autopilot` or say “continue” and read `omg autopilot status`. Do not invent infinite self-loops without CLI stamps.

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

1. Confirm plugin skills exist: `skills/omg-autopilot`, `skills/omg-ultragoal`, `skills/omg-ultrawork`, `skills/omg-ralph`, `skills/omg-ralplan`, `skills/omg-cancel`.
2. Run `omg doctor` if `.omg/` missing or user is first-time.
3. Map user intent → skill table above.
4. Load the matching skill body and follow it.
5. Remind: workers = Grok `spawn_subagent` only; no external agent CLIs.

## Do not

- Start implementing under this skill alone — hand off to autopilot/ultragoal/ulw/ralph/ralplan.
- Call claude/codex/omc team/agy/cursor-agent as workers.
- Mark runs verified yourself; only `omg` CLI owns that.
- Use self-matching `pkill -f` to stop runs — use `omg cancel` (see `omg-cancel`).
