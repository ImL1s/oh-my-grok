# oh-my-grok

OMC-style multi-agent orchestration for **Grok Build**.

**Option B architecture:** a Grok plugin (skills, agents, hooks) paired with the **`omg` CLI**. Workers fan out only via Grok-native **`spawn_subagent`**. No Rust fork of grok-build. **No tmux in v1.**

| Component | Role |
|-----------|------|
| **Grok plugin** (`plugin.json`, `skills/`, `agents/`, `hooks/`) | In-session playbooks, custom agents, event spool + PreToolUse soft-guard |
| **`omg` CLI** (`bin/omg`, `omg_cli/`) | Hard keywords (`ulw` / `ralph` / `ralplan`), project setup, state single-writer, outer loops |

Version: **0.1.0** · License: MIT

---

## What it is

Grok Build already ships subagents, worktrees, plugins, and hooks. oh-my-grok adds the **workflow layer**:

- **ulw (ultrawork)** — parallel decompose → `spawn_subagent` → integrate → verify
- **ralph** — persistence loop (one story per iteration; outer CLI owns the loop)
- **ralplan** — plan consensus (plan → critic → revise; **no implementation**)

Agents may write proposals under `.omg/artifacts/`. Only the **`omg` CLI** is authoritative for `passes` / `verified` under `.omg/state/`.

---

## Install

### Prerequisites

- [Grok Build CLI](https://github.com/xai-org/grok-cli) (`grok` on `PATH`)
- Python **3.11+** (`python3`)

### 1. Install the plugin

From a clone of this repo:

```bash
cd /path/to/oh-my-grok
grok plugin install . --trust
```

- `SOURCE` may be a local path, git URL, or GitHub `user/repo` (supports `@ref` and `#subdir`).
- `--trust` skips the confirmation prompt (required for non-interactive install).

Validate the manifest anytime:

```bash
grok plugin validate .
```

### 2. Put `omg` on your PATH

The CLI entrypoint is `bin/omg` (stdlib Python; no install package required for the CLI itself).

**Option A — symlink (recommended):**

```bash
ln -sf "$(pwd)/bin/omg" ~/.local/bin/omg
# ensure ~/.local/bin is on PATH
omg --help
```

**Option B — invoke from the repo:**

```bash
./bin/omg --help
# or
PYTHONPATH=. python3 -c 'from omg_cli.main import main; raise SystemExit(main())' --help
```

**Option C — project-local alias** (in a project shell):

```bash
alias omg='/path/to/oh-my-grok/bin/omg'
```

### 3. Set up a project workspace

Inside the project you want to orchestrate:

```bash
omg setup
omg doctor
```

`setup` creates `.omg/` directories and merges:

- `AGENTS.md` fragment (`<!-- OMG:START -->` … `<!-- OMG:END -->`)
- `.gitignore` fragment for runtime state / artifacts

`doctor` checks plugin layout, hooks, skills (`omg-*`), agents, and that `grok` is on `PATH`.

---

## HARD RULES

These are non-negotiable in skills, agent prompts, and CLI-injected reminders:

1. **Fan-out only via Grok `spawn_subagent`**
   - Depth = 1; children must **not** spawn further subagents.
2. **Never** invoke `claude` / `codex` / `omc team` / `agy` / `cursor-agent` / `kimi` as **default workers**.
   - Advisors (if any) are opt-in and outside the default worker path.
3. **Use Grok tool names:** `read_file`, `search_replace`, `run_terminal_command`, `spawn_subagent`, `grep`, `list_dir`, …
4. **State ownership:** only the **`omg` CLI** mutates `passes` / `verified` under `.omg/state/runs/<run-id>/`.
   - Agents/hooks write proposals under `.omg/artifacts/` and event spools — never mark verified themselves.
5. **Cancel** with `omg cancel` (PID files). **Never** self-matching `pkill -f`.

### Soft-guard (defense-in-depth, not a hard guarantee)

`PreToolUse` denies external agent CLIs in command position. Grok hooks can **fail-open** (timeout / crash / malformed → tool may still run), so skills + CLI HARD RULES remain the primary contract.

Bypass is **process-env only**:

```bash
export OMG_ALLOW_EXTERNAL_CLI=1   # only in a controlled parent process
```

- Inline `OMG_ALLOW_EXTERNAL_CLI=1 claude …` in the command string does **not** bypass (env is not parsed from the shell command).
- Intended for rare advisor tooling, not default workers.

---

## Commands

```text
omg [-h] [--safe] [--yolo] {setup,doctor,state,cancel,accept,integrate,ulw,ralph,ralplan} ...
```

| Command | Purpose |
|---------|---------|
| `omg setup` | Ensure `.omg/` dirs; merge AGENTS + gitignore fragments |
| `omg doctor` | Health checks (plugin, hooks, skills, agents, `grok` on PATH) |
| `omg state` | Print active run JSON (`--run <id>` for a specific run) |
| `omg cancel` | Cancel active run (`--run <id>` optional); uses PID files |
| `omg accept` | Freeze PRD commands + run acceptance; set `verified` only with CLI stamp |
| `omg integrate` | ULW: clean-tree preflight + cherry-pick result envelopes (`--run`, `--dry-run`) |
| `omg ulw "goal"` | Ultrawork — parallel `spawn_subagent` fan-out (records `base_sha` when git available) |
| `omg ralph "goal"` | Ralph — persistence loop (one story per iteration) |
| `omg ralplan "goal"` | Ralplan — plan consensus only (no implementation) |

### Shared flags

| Flag | Meaning |
|------|---------|
| `--dry-run` | Create run state + write `last_argv.json` / prompt; **do not** exec `grok` (mode subcommands) |
| `--yolo` | Elevated permissions for mode launchers (maps to Grok `--permission-mode bypassPermissions` + `--always-approve`; off by default) |
| `--safe` | Prefer non-elevated defaults (`--permission-mode default`); if both `--yolo` and `--safe`, **safe wins** (no elevation) |
| `--max-iter N` | Max iterations (`ralph` default **3**; `ulw` / `ralplan` default **1**) |

### Examples

```bash
omg doctor
omg setup

omg ulw "parallelize the flaky test fix" --dry-run
omg ralph "ship the auth migration" --max-iter 5
omg ralplan "consensus plan for Option B state layout" --safe

omg state
omg state --run 20260719T094708Z-7048b749
omg cancel

# ULW convergence: workers write .omg/artifacts/ulw-results/<task_id>.json
omg integrate --dry-run
omg integrate --run <run-id>
```

**ULW envelopes** (under `.omg/artifacts/ulw-results/`): `task_id`, `base_sha`, `head_sha`, `worktree_path`, `changed_files`, `status` (`ok`|`failed`). `omg integrate` sorts by `task_id`, requires clean git tree (no auto-stash), matches run `base_sha`, cherry-picks each `head_sha`, stops on conflict, writes `integrate.result.json`.

Modes load the matching skill body (`skills/omg-ultrawork`, `skills/omg-ralph`, `skills/omg-ralplan`), inject HARD RULES, create a run under `.omg/state/runs/`, and launch `grok -p …` (unless `--dry-run`).

---

## Architecture (brief)

```text
┌─────────────────────────────────────────────────────────────┐
│  User / Grok session                                         │
│    skills/omg-*  ·  agents/omg-*  ·  AGENTS.md fragment      │
└───────────────┬───────────────────────────┬─────────────────┘
                │                           │
                ▼                           ▼
┌───────────────────────────┐   ┌─────────────────────────────┐
│  omg CLI (single-writer)  │   │  Hooks (fail-open safe)     │
│  · setup / doctor / state │   │  SessionStart / Stop /      │
│  · cancel / accept        │   │  SubagentStop → event spool │
│  · integrate (ULW)        │   │  PreToolUse → soft deny     │
│  · ulw / ralph / ralplan  │   │  (OMG_ALLOW_EXTERNAL_CLI)   │
│  · verified / passes only │   │                             │
└───────────────┬───────────┘   └─────────────────────────────┘
                │
                ▼
        .omg/state/runs/<run-id>/   status.json, acceptance.*, integrate.result.json
        .omg/artifacts/ulw-results/ worker result envelopes
        .omg/artifacts/             other agent proposals only
```

| Layer | Notes |
|-------|--------|
| **Plugin** | `plugin.json`, `skills/omg-*`, `agents/omg-*`, `hooks/hooks.json` |
| **PreToolUse soft-guard** | Matches `run_terminal_command\|Bash\|Shell`; shared logic in `omg_cli/deny.py` |
| **Env bypass** | Process env `OMG_ALLOW_EXTERNAL_CLI=1` only — never parsed from command text |
| **State** | `.omg/state/runs/<run-id>/` atomic JSON via `omg_cli/state.py`; hooks must not write `verified` |
| **Workers** | Grok `spawn_subagent` only (depth 1); custom agents: orchestrator, executor, critic, verifier |
| **Acceptance** | Frozen `acceptance.manifest.json` + CLI-stamped `acceptance.result.json` (`writer=omg-cli`); forged `{passed:true}` cannot `set_verified` |
| **ULW integrate** | Clean-tree preflight + envelope cherry-pick (`omg_cli/integrate.py`); does not set `verified` alone |
| **Soft-guard limits** | Defense-in-depth, not a sandbox. Still may miss interpreter escapes (`python3 -c …`, `npx …`) and some shell constructs; HARD RULES remain primary |

Project layout after `omg setup`:

```text
.omg/
  state/runs/<run-id>/   # CLI single-writer status
  plans/ research/ handoffs/ artifacts/ ultragoal/
```

---

## Development

```bash
cd /path/to/oh-my-grok

# optional venv
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements-dev.txt

# tests (stdlib CLI + pytest)
PYTHONPATH=. python3 -m pytest tests/ -q

# plugin manifest
grok plugin validate .

# CLI smoke
./bin/omg doctor
./bin/omg ulw "noop" --dry-run
```

- **Runtime:** Python 3.11+, stdlib only for `omg_cli` and hooks.
- **Dev dependency:** `pytest>=8.0` (`requirements-dev.txt`).
- Always set **`PYTHONPATH=.`** when running pytest or importing `omg_cli` outside `bin/omg`.

---

## Skills & agents

| Skill | Trigger keywords (approx.) |
|-------|----------------------------|
| `omg-using` | bootstrap, setup omg, which mode |
| `omg-ultrawork` | ulw, ultrawork, parallel fan-out |
| `omg-ralph` | ralph, persist until verified |
| `omg-ralplan` | ralplan, plan consensus |
| `omg-cancel` | cancel, abort run |

| Agent | Role |
|-------|------|
| `omg-orchestrator` | Decompose + coordinate |
| `omg-executor` | Implement |
| `omg-critic` | Challenge plans/code (read-oriented) |
| `omg-verifier` | Check evidence (does not own `verified` flag) |

---

## Research & plan docs

In-repo plan:

- [`docs/superpowers/plans/2026-07-19-oh-my-grok.md`](docs/superpowers/plans/2026-07-19-oh-my-grok.md) — MVP implementation plan (task checklist)

Sibling research (written during design; live next to [grok-build](../grok-build) when that tree is present):

| Path | Content |
|------|---------|
| `../grok-build/.omc/research/oh-my-grok-plan.md` | Consolidated architecture / Option B |
| `../grok-build/.omc/research/dual-review-codex.md` | Codex dual-review |
| `../grok-build/.omc/research/dual-review-fable.md` | Fable dual-review |
| `../grok-build/.omc/research/dual-review-synthesis.md` | Synthesis of dual-review |
| `../grok-build/.omc/research/omc-architecture.md` | OMC reference notes |
| `../grok-build/.omc/research/grok-extension-points.md` | Grok plugin/hook extension points |

---

## License

MIT
