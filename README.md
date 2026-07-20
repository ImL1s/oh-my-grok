# oh-my-grok (OMG)

<p align="center">
  <img src="assets/omg-character.png" alt="oh-my-grok character" width="300">
  <br>
  <em>Start Grok stronger — then let OMG own the workflow, evidence, and verified completion.</em>
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-green.svg" alt="License: MIT"></a>
  <img src="https://img.shields.io/badge/python-%3E%3D3.11-blue" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/host-Grok%20Build-black" alt="Grok Build">
  <img src="https://img.shields.io/badge/scope-core%20purpose%20parity-lightgrey" alt="core purpose parity">
</p>

**Multi-agent orchestration for [Grok Build](https://github.com/xai-org/grok-cli).**  
Sibling of [oh-my-claudecode](https://github.com/Yeachan-Heo/oh-my-claudecode) (OMC), [oh-my-codex](https://github.com/Yeachan-Heo/oh-my-codex) (OMX), and [oh-my-openagent](https://github.com/code-yeongyu/oh-my-openagent) (OmO) — same *orchestration idea*, **Grok-native** runtime.

_Don't learn every Grok flag. Use `omg` + skills: clarify → plan → execute → verify._

---

## Mental model

OMG does **not** replace Grok Build.

| Layer | Job |
|-------|-----|
| **Grok** | Agent work (`spawn_subagent`, tools, session) |
| **Plugin skills / agents** | Playbooks and role prompts |
| **`omg` CLI** | Run state, evidence stamps, acceptance, integrate, verified |
| **`.omg/`** | Plans, artifacts, run state (CLI is single-writer for `passes` / `verified`) |

Workers fan out only via Grok **`spawn_subagent`** (depth 1). No Rust fork of grok-build. **No tmux in v1.**  
**Scope honesty:** [core purpose parity](docs/research/core-parity-matrix-2026-07-20.md) — not a full OMC surface (HUD / wiki / Stop hard-pin / tmux team).

| Component | Role |
|-----------|------|
| **Grok plugin** | `skills/omg-*`, `agents/omg-*`, hooks (event spool + PreToolUse soft-guard) |
| **`omg` CLI** | `setup` / `doctor` / modes / `accept` / `integrate` / `goal` / `interview` / `autopilot`… |

Version: **0.2.5** · License: MIT

---

## Quick start

**Requirements:** [Grok Build CLI](https://github.com/xai-org/grok-cli) (`grok` on `PATH`) · Python **3.11+**

```bash
# 1) Install plugin (from a clone of this repo)
cd /path/to/oh-my-grok
./scripts/install-plugin.sh
# or: grok plugin validate . && grok plugin install . --trust

# 2) Put omg on PATH
ln -sf "$(pwd)/bin/omg" ~/.local/bin/omg   # ensure ~/.local/bin is on PATH
omg --help

# 3) Wire a project
cd /path/to/your-project
omg setup
omg doctor
```

Smoke:

```bash
omg doctor
omg ulw "noop" --dry-run
```

That’s enough to start. Everything below is the default spine and reference.

---

## Recommended default flow

When the task is non-trivial, prefer this spine (OMX-style, Grok-native):

```text
1. omg interview start "…"     # clarify when vague  (or skill omg-deep-interview)
2. omg ralplan "…"             # plan consensus only — no implementation
3. omg ulw / omg ralph / omg autopilot …   # execute
4. omg accept --yes            # only path that may set verified (CLI stamp + process token)
```

| If you need… | Use |
|--------------|-----|
| Parallel independent slices | `omg ulw "…"` + `omg worker own/seal/join` + `omg integrate` |
| Persist until verified | `omg ralph "…"` |
| Plan consensus only | `omg ralplan "…"` |
| Full phase coordinator | `omg autopilot start "…"` |
| Unclear requirements | `omg interview start "…"` |
| Abort | `omg cancel` |

**QA clean ≠ verified.** UltraQA (`omg qa`) can go green without promoting the run.

---

## CLI vs skills

| Surface | Where | Examples |
|---------|--------|----------|
| **Terminal CLI** | shell | `omg setup`, `omg ulw`, `omg accept`, `omg doctor` |
| **In-session skills** | Grok plugin session | `omg-ultrawork`, `omg-ralph`, `omg-ralplan`, `omg-using` |

Both share HARD RULES: spawn only via Grok; CLI owns `verified`; no external agent CLIs as default workers.

---

## Orchestration modes

| Mode | What it is | Use for |
|------|------------|---------|
| **ulw** | Parallel fan-out → ownership join → integrate | Independent slices |
| **ralph** | One-owner persist loop + acceptance | Must finish / verified |
| **ralplan** | Planner → architect → critic (no code) | Plan consensus |
| **pipeline** | plan → implement → integrate → dual-review → accept | Scripted end-to-end |
| **autopilot** | Strict v2 phase machine | Supervised lifecycle |
| **interview / goal / qa / review** | CLI primitives | Requirements, ledger, UltraQA, hash-bound review |

---

## HARD RULES

1. **Fan-out only via Grok `spawn_subagent`** (depth = 1; children do not spawn).
2. **Never** use `claude` / `codex` / `omc team` / `agy` / `cursor-agent` as **default workers** (advisors are opt-in via `omg ask`).
3. **Grok tool names:** `read_file`, `search_replace`, `run_terminal_command`, `spawn_subagent`, `grep`, `list_dir`, …
4. **Only `omg` CLI** may set `passes` / `verified` under `.omg/state/`. Agents write proposals under `.omg/artifacts/`.
5. **Cancel** with `omg cancel` — never self-matching `pkill -f`.

### Isolation (honest)

Primary isolation is **`capability_mode`** (`read-write` implementers / `read-only` critic-verifier; **no Execute** for workers).  
PreToolUse deny is **fail-open soft-guard** — not a sandbox. Details: [`docs/security-model.md`](docs/security-model.md).

| Purpose | Primary | Secondary |
|---------|---------|-----------|
| No external agent CLIs as workers | `capability_mode` + agent `disallowedTools` | PreToolUse soft deny |
| Acceptance shell | `omg accept` + semantic command policy | floors always deny `python -c` / shells / agent bins |
| Parallel without tmux | `spawn_subagent` + worktrees | process fanout only with `OMG_EXPERIMENTAL_PROCESS_FANOUT=1` |

```bash
export OMG_ALLOW_EXTERNAL_CLI=1   # process-env only; never parse from command text
```

---

## Commands

```text
omg {setup,doctor,state,cancel,interview,goal,accept,integrate,worker,
     review,qa,autopilot,ulw,ralph,ralplan,ask,pipeline,dual-review} ...
```

| Command | Purpose |
|---------|---------|
| `omg setup` / `omg doctor` | Scaffold `.omg/` · health (+ `--strict`) |
| `omg state` / `omg cancel` | Active run · process-group cancel |
| `omg interview …` | Deep-interview requirements gate |
| `omg goal …` | Hash-chained ultragoal ledger + tail repair |
| `omg ulw` / `ralph` / `ralplan` | Parallel / persist / plan-only modes |
| `omg worker own\|prepare\|seal\|join` | ULW ownership + worktree + envelopes |
| `omg integrate` | Cherry-pick ULW envelopes (does **not** set verified alone) |
| `omg review` / `omg qa` | Hash-bound review · UltraQA (**QA clean ≠ verified**) |
| `omg autopilot …` | Strict phases; verified only via same-process accept |
| `omg accept` | Freeze PRD + run; only path that may `verified` |
| `omg ask` | Trusted external advisor broker (not a worker) |
| `omg pipeline` / `dual-review` | Scripted pipeline · interim critic→verifier |

### Shared flags

| Flag | Meaning |
|------|---------|
| `--dry-run` | Write state/argv; do not exec `grok` |
| `--yolo` | Elevated permissions (default **off**) |
| `--safe` | Prefer plan permissions (**wins** over yolo) |
| `--max-iter N` | ralph default 3 · ulw 1 · ralplan max_rounds 3 |
| `--timeout SEC` | Per-launch timeout (default **3600**; `0` = unlimited) |

### Examples

```bash
omg setup && omg doctor

omg ralplan "consensus plan for auth refactor" --safe
omg ulw "parallelize the flaky test fix" --dry-run
omg ralph "ship the auth migration" --max-iter 5

omg worker own --run RUN --tasks-json '[{"task_id":"t1","owned_files":["a.py"]}]'
omg worker prepare --task t1 --run RUN
omg worker seal --task t1 --run RUN
omg worker join --run RUN
omg integrate --run RUN

omg accept --yes
omg accept --review --yes
omg state --human
omg cancel
```

### Acceptance (writer stamp)

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

**Floors always deny:** agent CLIs, shells, `npx`, `python -c`/`-e`, dangerous `git`/`make`/`go` forms.  
`set_verified` needs CLI stamp + matching manifest sha + **in-process** acceptance token — forged `{passed:true}` is rejected.

### ULW integrate

Envelopes under `.omg/artifacts/ulw-results/<run_id>/`. Strict-v2 ULW requires **ownership manifest** + complete **join** before integrate. Does not set `verified`.

### Ralplan FSM

```text
draft → critic → revise → verifier → accepted | failed
```

Accepted only with whole-word **APPROVE** in verifier artifact. Never implements product code.

---

## Architecture (brief)

```text
User / Grok session  →  skills + agents
         │
         ├─► omg CLI (single-writer: status / accept / integrate / verified)
         └─► hooks (fail-open PreToolUse soft-guard + event spool)

.omg/state/runs/<run-id>/     CLI authority
.omg/artifacts/               proposals + ULW envelopes
.omg/ultragoal/               goal ledger (when used)
```

---

## Skills & agents

| Skill | Use when |
|-------|----------|
| `omg-using` | Bootstrap / which mode |
| `omg-ultrawork` | Parallel fan-out |
| `omg-ralph` | Persist until verified |
| `omg-ralplan` | Plan consensus |
| `omg-deep-interview` | Requirements gate |
| `omg-ultragoal` | Durable multi-story ledger |
| `omg-ultraqa` | Bounded QA loop |
| `omg-autopilot` | Strict phase coordinator |
| `omg-pipeline` / `omg-dual-review` / `omg-ask` / `omg-cancel` | Pipeline, review, advisors, abort |

| Agent | Role |
|-------|------|
| `omg-orchestrator` | Decompose + coordinate |
| `omg-executor` | Implement (`read-write`, no shell) |
| `omg-critic` / `omg-verifier` | Challenge / check evidence (read-only) |
| `omg-code-reviewer` / `omg-architect` / `omg-qa-tester` / `omg-analyst` | Structured review · QA · interview |

---

## Development & testing

```bash
cd /path/to/oh-my-grok
python3 -m venv .venv && source .venv/bin/activate
python3 -m pip install -r requirements-dev.txt

PYTHONPATH=. python3 -m pytest tests/ -q
grok plugin validate .
./bin/omg doctor
./scripts/smoke.sh
```

| Layer | Command |
|-------|---------|
| Unit | `PYTHONPATH=. python3 -m pytest -q -m "not live"` |
| Hermetic e2e | `OMG_E2E=1 ./scripts/smoke.sh` |
| Live | `./scripts/live_suite.sh --quick` / `--full` |

Do not claim production isolation from unit green alone. See [`docs/research/test-matrix.md`](docs/research/test-matrix.md).

---

## Docs map

| Path | Contents |
|------|----------|
| [`docs/security-model.md`](docs/security-model.md) | Isolation layers |
| [`docs/research/core-parity-matrix-2026-07-20.md`](docs/research/core-parity-matrix-2026-07-20.md) | HAVE / NEVER scope |
| [`docs/research/omc-parity-council/`](docs/research/omc-parity-council/) | Parity council + STATUS |
| [`docs/research/live/`](docs/research/live/) | Live suite evidence |
| [`docs/superpowers/plans/`](docs/superpowers/plans/) | Implementation plans |

---

## Changelog notes (compressed)

Full dual-review ship bar (C1–C9) is complete. Recent lines:

- **v0.2.x:** acceptance policy, run mutex, ULW integrate, ralplan FSM, worker prepare/seal, pipeline order, live suite.
- **2026-07-20 core-purpose parity:** evidence stamps, session lease, interview, goal ledger + repair, ULW ownership/join, hash-bound review, UltraQA, strict autopilot; destination gates; CLI acceptance authority for `verified`.

Details live in git history and `docs/research/`.

---

## License

MIT

---

<p align="center">
  <em>Inspired by OMC · OMX · OmO — built for Grok.</em><br>
  <strong>Core purpose first. No fake parity.</strong>
</p>
