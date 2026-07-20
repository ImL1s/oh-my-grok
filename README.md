# oh-my-grok (OMG)

<p align="center">
  <img src="assets/omg-character.png" alt="oh-my-grok character" width="320">
  <br>
  <em>Start Grok stronger — then let OMG own the workflow, evidence, and verified completion.</em>
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-green.svg" alt="License: MIT"></a>
  <img src="https://img.shields.io/badge/python-%3E%3D3.11-blue" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/host-Grok%20Build-black" alt="Grok Build">
  <img src="https://img.shields.io/badge/scope-core%20purpose%20parity-lightgrey" alt="core purpose parity">
</p>

**Multi-agent orchestration for Grok Build.** Sibling of [oh-my-claudecode](https://github.com/Yeachan-Heo/oh-my-claudecode) (OMC), [oh-my-codex](https://github.com/Yeachan-Heo/oh-my-codex) (OMX), and [oh-my-openagent](https://github.com/code-yeongyu/oh-my-openagent) (OmO) — same orchestration *idea*, **Grok-native** runtime.

_Don't learn every Grok flag. Use `omg` + skills for the path that matters: clarify → plan → execute → verify._

**Option B architecture:** a Grok plugin (skills, agents, hooks) paired with the **`omg` CLI**. Workers fan out only via Grok-native **`spawn_subagent`**. No Rust fork of grok-build. **No tmux in v1** (honest scope: not a full OMC surface clone).

| Component | Role |
|-----------|------|
| **Grok plugin** (`plugin.json`, `skills/`, `agents/`, `hooks/`) | In-session playbooks, custom agents, event spool + PreToolUse soft-guard |
| **`omg` CLI** (`bin/omg`, `omg_cli/`) | Hard keywords (`ulw` / `ralph` / `ralplan` / `goal` / `interview` / `autopilot`…), project setup, state single-writer, acceptance, integrate |

Version: **0.2.5** · License: MIT · Parity matrix: [`docs/research/core-parity-matrix-2026-07-20.md`](docs/research/core-parity-matrix-2026-07-20.md)

---

## What it is

Grok Build already ships subagents, worktrees, plugins, and hooks. oh-my-grok adds the **workflow layer**:

- **ulw (ultrawork)** — parallel decompose → `spawn_subagent` → ownership join → integrate → verify
- **ralph** — persistence loop (one story per iteration; outer CLI owns the loop)
- **ralplan** — plan consensus FSM (planner → architect → critic; **no implementation**)
- **interview / goal / autopilot / qa** — requirements gate, hash-chained ledger, strict phase coordinator, UltraQA (QA clean ≠ verified)

Agents may write proposals under `.omg/artifacts/`. Only the **`omg` CLI** is authoritative for `passes` / `verified` under `.omg/state/`.

---

## Isolation stack (honest)

**Canonical layer table:** [`docs/security-model.md`](docs/security-model.md).

Workers must not run external agent CLIs as a **hard** property of tool policy — not of PreToolUse regex alone.

| Purpose | Primary mechanism | Secondary |
|---------|-------------------|-----------|
| Workers cannot shell external CLIs | `capability_mode: read-write` / `read-only` (**no Execute** → no `run_terminal_command`) | agent `disallowedTools` (executor bans shell+spawn); parent `--disallowed-tools` clamp |
| Leader shell still soft-guarded | PreToolUse deny (**fail-open** honest) | skill HARD RULES |
| Acceptance shell only via CLI | `omg accept` + **semantic command policy** (`omg_cli/command_policy.py`); deny `python -c` / `npx` / shells / agent CLIs | strip `OMG_ALLOW_*` from child env |
| Parallel without tmux | `spawn_subagent` (default isolation story) + worktrees | experimental `omg ulw --fanout process` only with `OMG_EXPERIMENTAL_PROCESS_FANOUT=1` |

**PreToolUse:** grok-build source shows subagents **inherit** parent hooks (still fail-open). Canary: `python3 scripts/canary_pretool.py --dry` (PATH shim, never real claude) — see [`docs/research/subagent-pretooluse-spike.md`](docs/research/subagent-pretooluse-spike.md).

**v0.2.1 hardening:** acceptance allowlist + `--review`/`--yes`; `create_run` flock; cancel `pid.json` starttime verify; integrate path whitelist + `base..head` cherry-pick; `scripts/smoke.sh`.

**v0.2.2:** `build_grok_argv(disallow_shell=…)` injects `--disallowed-tools run_terminal_command` for dual-review / ralplan critic+verifier (not ulw/ralph leaders); `OMG_DISALLOW_SHELL=1` opt-in; process fanout skeleton.

**v0.2.3:** semantic acceptance policy (`python -c` denied; `-m pytest|unittest` / project `.py` ok); `omg accept --review` prints manifest sha + cwd + `shlex` argv; TTY y/N; `--no-allowlist` TTY-only break-glass; `scripts/install-plugin.sh` + `canary_pretool.py`; executor disallows shell tools; capability spawn contract injected in prompts; **process fanout is experimental opt-in only** (`OMG_EXPERIMENTAL_PROCESS_FANOUT=1`); cancel kill is **fail-closed** without matching `starttime`.

**v0.2.5:** integrate ancestry / merge reject / empty-`changed_files` anti-forge / `--require-squash`; pipeline stage order **plan → implement → integrate → dual_review → accept → report**; `omg worker prepare|seal`; `omg ask` stdin default; dual-review sequential interim. **Live-gates:** doctor hard-checks global PreToolUse hook; acceptance argv grammar v2 (git list-only, make no `-f`/`-C`, go no `-exec`/`--exec`, cargo no `build`); canary host-signature pass only (`DENIED_CLAIMED_NO_HOOK_ORACLE` on prose); `scripts/live_suite.sh --quick/--full/--quota-heavy` + dated evidence under `docs/research/live/`.

**Post-0.2.5 (2026-07-20, same release line):** multi-advisor OMC-parity council docs; **strict verdict** (`omg_cli/verdict.py`) so dual/ralplan cannot false-green on `Do not APPROVE` / stubs / non-zero stage rc; spawn PreToolUse deny **RETRY IMMEDIATELY** messaging; ULW **auto-integrate** when envelopes present; live **L-DUAL-1** semantic gate; doctor soft **foreign orch** discovery via `grok inspect`; canary pass = parent host deny **+** child capability isolation (no shell); `omg state --human`. Research index: [`docs/research/omc-parity-council/`](docs/research/omc-parity-council/) · status: [`STATUS.md`](docs/research/omc-parity-council/STATUS.md) · verify: [`docs/research/live/verification-2026-07-20.md`](docs/research/live/verification-2026-07-20.md).

**Honest external seats:** Codex free audit **done** (`docs/research/omc-parity-council/08-codex.md`). Claude Fable free audit **BLOCKED** this round (`09-fable.md`) — do not claim dual-external consensus.

---

## Docs map (research)

| Path | Contents |
|------|----------|
| [`docs/security-model.md`](docs/security-model.md) | Isolation layers (capability primary; PreToolUse soft) |
| [`docs/research/README.md`](docs/research/README.md) | Research index |
| [`docs/research/omc-parity-council/`](docs/research/omc-parity-council/) | Multi-Grok + Codex/Fable parity council |
| [`docs/research/omc-parity-council/STATUS.md`](docs/research/omc-parity-council/STATUS.md) | Done / not-done (incl. Fable BLOCKED) |
| [`docs/research/omc-parity-council/OPEN-ITEMS.md`](docs/research/omc-parity-council/OPEN-ITEMS.md) | Remaining backlog |
| [`docs/research/external-advisors.md`](docs/research/external-advisors.md) | Codex + Claude Fable CLI ops (argv, sanitize, PID) |
| [`docs/research/stop-continuation/`](docs/research/stop-continuation/) | Stop pin **DO NOT BUILD** (host non-blocking) |
| [`docs/research/live/`](docs/research/live/) | Dated live suite + canary evidence |

---

## Install

### Prerequisites

- [Grok Build CLI](https://github.com/xai-org/grok-cli) (`grok` on `PATH`)
- Python **3.11+** (`python3`)

### 1. Install the plugin

From a clone of this repo (private install is fine):

```bash
cd /path/to/oh-my-grok
# one-shot: validate → install --trust → next steps
./scripts/install-plugin.sh
# or manually:
grok plugin validate .
grok plugin install . --trust
```

- `SOURCE` may be a local path, git URL, or GitHub `user/repo` (supports `@ref` and `#subdir`).
- `--trust` skips the confirmation prompt (required for non-interactive install).

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
omg doctor --strict   # treat compat / inspect gaps as FAIL
```

`setup` creates `.omg/` directories, merges AGENTS + `.gitignore` fragments, and prints a **compat.claude isolation** banner.

`doctor` checks plugin layout, hooks, skills (`omg-*`), agents, `grok` on `PATH`, plugin trust/inventory (best-effort), and Claude/OMC keyword leakage under `~/.claude` (warn by default; `--strict` fails).

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
5. **Cancel** with `omg cancel` (PID / process-group). **Never** self-matching `pkill -f`.

### Soft-guard limits (defense-in-depth, not a hard guarantee)

`PreToolUse` denies external agent CLIs in **command position** on matching tools. Grok hooks can **fail-open** (timeout / crash / malformed → tool may still run). Source evidence: subagents **inherit** PreToolUse; still not a sandbox. Prefer **capability_mode** as primary (see Isolation stack).

**Known limits:**

- Soft-gate is **not** a sandbox. Interpreter escapes (`python3 -c …`, `node -e`, `npx …`) matter only when shell tool is available.
- Host may fail-open. Compensation: `capability_mode: read-write` (no shell) for implementers / `read-only` for critic/verifier; acceptance shell **only** via `omg accept`. See [`docs/research/subagent-pretooluse-spike.md`](docs/research/subagent-pretooluse-spike.md).

Bypass is **process-env only**:

```bash
export OMG_ALLOW_EXTERNAL_CLI=1   # only in a controlled parent process
```

- Inline `OMG_ALLOW_EXTERNAL_CLI=1 claude …` in the command string does **not** bypass (env is not parsed from the shell command).
- Intended for rare advisor tooling, not default workers.

---

## v0.2 dual-review completion (C1–C9)

Ship bar from dual-review Criticals. Status:

| ID | Contract | Status | Where |
|----|----------|--------|--------|
| **C1** | compat.claude doctor/setup isolation | **Done** | `omg_cli/compat.py`, `omg doctor`, `omg setup` banner |
| **C2** | trusted/active hook inventory | **Done** | `omg doctor` best-effort `grok` inspect; WARN if unavailable; footer soft-gate honesty |
| **C3** | deny residuals documented | **Done** | README soft-guard limits; tests keep deny paths; residual `python -c` / `npx` noted |
| **C4** | frozen acceptance runner (writer stamp) | **Done** | `omg_cli/acceptance.py`, `omg accept`; forged `{passed:true}` cannot `set_verified` |
| **C5** | active-run mutex | **Done** | `create_run` blocks concurrent non-terminal runs; process-group cancel |
| **C6** | ULW integrator | **Done** | `omg_cli/integrate.py`, `omg integrate`; clean-tree preflight + envelope cherry-pick |
| **C7** | ralplan CLI FSM | **Done** | `omg_cli/ralplan.py` draft→critic→revise→verifier; max_rounds; APPROVE gate |
| **C8** | omg-* agents | **Done** | `agents/omg-{orchestrator,executor,critic,verifier}.md` |
| **C9** | scaffold / project setup | **Done** | `omg setup`, templates, skills |

Additional v0.2 items:

| Item | Status | Where |
|------|--------|--------|
| Subagent PreToolUse spike + capability defaults | **Done** (ASSUMPTION if not live-verified) | `docs/research/subagent-pretooluse-spike.md`, skills |
| Headless argv + ralph context pack | **Done** | `build_grok_argv` / `build_prompt`: `--cwd`, `--output-format plain`, timeout default **3600s**, ralph pack |
| `doctor --strict` | **Done** | compat risks → FAIL |

Plan: [`docs/superpowers/plans/2026-07-19-oh-my-grok-v0.2-dual-review-complete.md`](docs/superpowers/plans/2026-07-19-oh-my-grok-v0.2-dual-review-complete.md)

---

## Commands

```text
omg [-h] [--safe] [--yolo] {setup,doctor,state,cancel,interview,goal,accept,integrate,worker,review,qa,autopilot,ulw,ralph,ralplan,ask,pipeline,dual-review} ...
```

| Command | Purpose |
|---------|---------|
| `omg setup` | Ensure `.omg/` dirs; merge AGENTS + gitignore; print compat isolation banner |
| `omg doctor` | Health checks (+ compat scan). `--strict` → FAIL on compat/inspect gaps |
| `omg state` | Print active run JSON (`--run <id>` for a specific run) |
| `omg cancel` | Cancel active run; SIGTERM process group then optional SIGKILL |
| `omg interview …` | Deterministic deep-interview requirements gate (CLI-stamped) |
| `omg goal …` | Durable hash-chained ultragoal ledger + tail repair |
| `omg accept` | Freeze PRD commands + run acceptance (allowlist); set `verified` only with CLI stamp |
| `omg integrate` | ULW: clean-tree + ancestry/merge/`changed_files` checks + cherry-pick (`base..head`); `--require-squash` |
| `omg worker prepare --task ID` | Create `.omg/worktrees/<run>/<task>` (`git worktree add` or clone path) |
| `omg worker seal --task ID` | Commit worktree + write `.omg/artifacts/ulw-results/<run>/<task>.json` |
| `omg worker own --tasks-json …` | CLI ownership manifest (capability + owned_files; join later) |
| `omg worker join` | Join seals vs ownership; missing/failed tasks block complete |
| `omg review` | Hash-bound code-reviewer + architect gate (never self-APPROVE) |
| `omg qa freeze/run/status` | Bounded UltraQA; **QA clean ≠ verified** |
| `omg autopilot start/transition/complete` | Strict v2 phase coordinator; verified only same-process accept |
| `omg pipeline "goal"` | plan → implement → integrate → dual_review → accept → `report.json` |
| `omg dual-review "…"` | Sequential headless critic→verifier (**interim**; not native spawn) |
| `omg ask <provider> "…"` | Trusted advisor broker (stdin prompt; child-only allow env) |
| `omg ulw "goal"` | Ultrawork — parallel `spawn_subagent` fan-out (records `base_sha` when git available) |
| `omg ulw "goal" --fanout process --workers N` | **Experimental** multi-PID supervisor (requires `OMG_EXPERIMENTAL_PROCESS_FANOUT=1`; not default isolation) |
| `omg ralph "goal"` | Ralph — persistence loop (one story per iteration; context pack each iter) |
| `omg ralplan "goal"` | Ralplan — CLI-owned plan consensus FSM only (no implementation) |

**Honest scope:** core Grok-native purpose parity (lifecycle, evidence, lease, ralplan, interview, goal ledger, ULW ownership, review, UltraQA, autopilot). **Not** full OMC surface (HUD/wiki/tmux team/Stop hard-pin). Matrix: [`docs/research/core-parity-matrix-2026-07-20.md`](docs/research/core-parity-matrix-2026-07-20.md).

### Shared flags

| Flag | Meaning |
|------|---------|
| `--dry-run` | Create run state + write `last_argv.json` / prompt; **do not** exec `grok` (mode subcommands) |
| `--yolo` | Elevated permissions for mode launchers (maps to Grok `--permission-mode bypassPermissions` + `--always-approve`; off by default) |
| `--safe` | Prefer non-elevated defaults (`--permission-mode plan`); if both `--yolo` and `--safe`, **safe wins** (no elevation). dual-review / ralplan critic+verifier always force safe (ignore parent `--yolo`) |
| `--max-iter N` | Max iterations (`ralph` default **3**; `ulw` default **1**; `ralplan` = max_rounds default **3**) |
| `--timeout SEC` | Per-launch grok timeout (default **3600**); `0` = unlimited |

### Examples

```bash
omg doctor
omg doctor --strict
omg setup

omg ulw "parallelize the flaky test fix" --dry-run
omg ralph "ship the auth migration" --max-iter 5 --timeout 7200
omg ralplan "consensus plan for Option B state layout" --safe

omg state
omg state --run 20260719T094708Z-7048b749
omg cancel

# Acceptance (writer stamp + semantic policy required for verified)
omg accept --yes                    # non-tty / CI: always pass --yes
omg accept --review --yes           # print sha/cwd/commands; --yes skips TTY prompt
omg accept --run <id> --dry-run
omg accept --allow-cmd mytool --yes # extend basename allowlist (floors remain)
# omg accept --no-allowlist        # DANGEROUS TTY-only break-glass

# ULW convergence: prepare/seal (no-shell workers) → integrate
omg worker prepare --task t1
# … worker edits under .omg/worktrees/<run>/t1 …
omg worker seal --task t1
omg integrate --dry-run
omg integrate --run <run-id>
omg integrate --require-squash   # reject multi-commit ranges
```

### Acceptance runner (writer stamp + semantic policy)

PRD / acceptance manifest schema (argv arrays only — no bare shell strings by default):

```json
{
  "version": 1,
  "goal": "...",
  "stories": [
    {"id": "s1", "title": "...", "commands": [["pytest", "tests/test_foo.py", "-q"]]}
  ],
  "global_commands": [["python3", "-m", "pytest", "tests/", "-q"]]
}
```

**Default families:** `true` / `false` / `pytest` (+args) / `python*` with only `-m pytest|unittest` or a project `.py` path / `npm test` or `npm run test|pytest` / `make` / `cargo` / `go` / `dart` / `flutter` / linters / `git`.

**Always denied (floors):** `claude`, `codex`, `omx`, `agy`, `cursor-agent`, `kimi`, `rm`, `sudo`, shells, **`npx`**, **`python -c` / `-e`**, other `python -m` modules. See [`docs/security-model.md`](docs/security-model.md).

`--no-allowlist` is **TTY-only** break-glass; floors still apply. `--yes` never bypasses policy.

Flow:

1. `freeze_acceptance` → policy check → `acceptance.manifest.json` + `acceptance.sha256` (includes `policy_version` / `allow_cmd`)
2. `run_acceptance` re-checks policy → `acceptance.result.json` with `"writer": "omg-cli"`
3. `set_verified` requires CLI stamp + matching manifest sha + in-process token — **agent-forged `{passed: true}` is rejected**

CLI gates: prints run_id / cwd / manifest sha / numbered `shlex` commands; non-tty requires `--yes`; TTY + `--review` prompts `[y/N]` unless `--yes`.

Ralph after each iteration: if PRD has valid commands → freeze → run → maybe verify. Without acceptance commands → never verified; ralph defaults to non-zero exit (`require_acceptance`).

### ULW integrate

**ULW envelopes** (under `.omg/artifacts/ulw-results/`): `task_id`, `base_sha`, `head_sha`, `worktree_path`, `changed_files`, `status` (`ok`|`failed`).

`omg integrate` sorts by `task_id`, requires clean git tree (no auto-stash), matches run `base_sha`, requires `worktree_path` under **project root or `.omg/worktrees`**, cherry-picks `base_sha..head_sha` when they differ (else single `head_sha`), stops on conflict, writes `integrate.result.json`. Does **not** set `verified` alone.

### Ralplan FSM (CLI-owned)

```text
draft → critic → revise → verifier → (accept | revise)* → accepted | failed
max_rounds default 3
```

State: `.omg/state/runs/<id>/ralplan.json` + `stages/`. Terminal **accepted** only if verifier artifact contains whole-word **APPROVE**. Never starts product implementation.

### Headless launch details

Modes load the matching skill body, inject HARD RULES, create a run under `.omg/state/runs/`, and launch `grok -p …` (unless `--dry-run`) with:

- `--cwd <project>` when path known
- `--output-format plain` (headless default)
- timeout default **3600s** (`--timeout` to override; `0` = unlimited)
- **ralph context pack** each iteration: `run_id`, `iteration`, `story`, `frozen_commands_summary`, path to `acceptance.result.json`

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
│  · ralplan FSM            │   │  (OMG_ALLOW_EXTERNAL_CLI)   │
│  · verified / passes only │   │                             │
└───────────────┬───────────┘   └─────────────────────────────┘
                │
                ▼
        .omg/state/runs/<run-id>/   status.json, acceptance.*, ralplan.json, integrate.result.json
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
| **compat.claude** | Doctor/setup scan for OMC/Claude hooks & magic keywords; isolation advice |
| **Acceptance** | Frozen `acceptance.manifest.json` + CLI-stamped `acceptance.result.json` (`writer=omg-cli`); forged `{passed:true}` cannot `set_verified` |
| **ULW integrate** | Clean-tree preflight + envelope cherry-pick (`omg_cli/integrate.py`); does not set `verified` alone |
| **Ralplan FSM** | CLI-owned stages + max_rounds; critic/verifier read-only capability defaults |
| **Soft-guard limits** | Defense-in-depth, not a sandbox. Still may miss interpreter escapes; subagent hook coverage ASSUMPTION — see research spike |

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
./bin/omg doctor --strict || true
./bin/omg ulw "noop" --dry-run
./scripts/smoke.sh
```

- **Runtime:** Python 3.11+, stdlib only for `omg_cli` and hooks.
- **Dev dependency:** `pytest>=8.0` (`requirements-dev.txt`).
- Always set **`PYTHONPATH=.`** when running pytest or importing `omg_cli` outside `bin/omg`.

## Testing

| Layer | Command |
|-------|---------|
| Unit | `PYTHONPATH=. python3 -m pytest -q -m "not live"` |
| Hermetic e2e | `OMG_E2E=1 ./scripts/smoke.sh` (default; set `OMG_E2E=0` to skip) |
| Live quick | `./scripts/live_suite.sh --quick` |
| Live full | `./scripts/live_suite.sh --full` |
| Live heavy | `./scripts/live_suite.sh --quota-heavy` |

Do not claim production isolation from unit green alone. See [`docs/research/test-matrix.md`](docs/research/test-matrix.md).

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
| `omg-executor` | Implement (`capability_mode` prefer read-write, no shell) |
| `omg-critic` | Challenge plans/code (read-only) |
| `omg-verifier` | Check evidence (read-only; does not own `verified` flag) |

**Capability defaults:** implementers → `read-write` (prefer no unrestricted shell); critic/verifier/explore → `read-only`; acceptance shell → **`omg` CLI only**.

---

## Research & plan docs

In-repo:

- [`docs/superpowers/plans/2026-07-19-oh-my-grok.md`](docs/superpowers/plans/2026-07-19-oh-my-grok.md) — MVP implementation plan
- [`docs/superpowers/plans/2026-07-19-oh-my-grok-v0.2-dual-review-complete.md`](docs/superpowers/plans/2026-07-19-oh-my-grok-v0.2-dual-review-complete.md) — v0.2 dual-review completion plan
- [`docs/research/subagent-pretooluse-spike.md`](docs/research/subagent-pretooluse-spike.md) — PreToolUse child coverage spike + ASSUMPTION/compensation

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
