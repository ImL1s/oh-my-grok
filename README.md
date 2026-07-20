# oh-my-grok (OMG)

<p align="center">
  <img src="assets/omg-character.png" alt="oh-my-grok character" width="300">
  <br>
  <em>Start Grok stronger — then let OMG own the workflow, evidence, and verified completion.</em>
  <br>
  <sub>Hero art: original AI-assisted mascot for oh-my-grok (ImL1s, 2026) · MIT with the repo · not affiliated with OMC/OMX/OmO</sub>
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-green.svg" alt="License: MIT"></a>
  <a href="https://github.com/ImL1s/oh-my-grok/actions/workflows/ci.yml"><img src="https://github.com/ImL1s/oh-my-grok/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <img src="https://img.shields.io/badge/python-%3E%3D3.11-blue" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/host-Grok%20Build-black" alt="Grok Build">
  <img src="https://img.shields.io/badge/scope-core%20purpose%20parity-lightgrey" alt="core purpose parity">
</p>

**Multi-agent orchestration for [Grok Build](https://github.com/xai-org/grok-build).**  
Sibling of [oh-my-claudecode](https://github.com/Yeachan-Heo/oh-my-claudecode) (OMC), [oh-my-codex](https://github.com/Yeachan-Heo/oh-my-codex) (OMX), and [oh-my-openagent](https://github.com/code-yeongyu/oh-my-openagent) (OmO) — same *orchestration idea*, **Grok-native** runtime.

_Unofficial community plugin — not affiliated with xAI or the OMC/OMX/OmO maintainers._

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

Workers fan out only via Grok **`spawn_subagent`** (depth 1). No Rust fork of grok-build.  
**tmux:** only as a **host session shell** for `omg --madmax` (OMC-style full-open launch) — **not** an OMC multi-CLI team control plane.  
**Scope honesty:** [core purpose parity](docs/research/core-parity-matrix-2026-07-20.md) — partial lifestyle surfaces (resume/wiki/hud/LSP probe); **not** OMC Stop hard-pin, full LSP MCP, or tmux multi-CLI team plane.

| Component | Role |
|-----------|------|
| **Grok plugin** | `skills/omg-*`, `agents/omg-*`, hooks (event spool + PreToolUse soft-guard + SessionStart RESUME.md) |
| **`omg` CLI** | `setup` / `doctor` / `resume` / modes / `accept` / `integrate` / `goal` / `interview` / `wiki` / `hud` / `lsp` / `autopilot`… |

Version: **0.3.2** · License: MIT

---

## Quick start

**Requirements:** [Grok Build CLI](https://github.com/xai-org/grok-build) (`grok` on `PATH`) · Python **3.11+**

OMG has **two surfaces**: Grok **plugin** (skills/agents/hooks) + **`omg` CLI** (state, accept, verified). You need both for the full product.

### Full install (recommended)

Use a **stable path** so the global soft-gate does not break when you tidy folders:

```bash
# 0) Host
curl -fsSL https://x.ai/cli/install.sh | bash
# docs: https://github.com/xai-org/grok-build · https://x.ai/cli

# 1) Clone to a stable home
git clone https://github.com/ImL1s/oh-my-grok.git ~/.local/share/oh-my-grok
cd ~/.local/share/oh-my-grok
./scripts/install-plugin.sh
# optional pin: git checkout v0.3.2

# 2) omg on PATH (not on PyPI yet; install script also tries this)
ln -sf "$(pwd)/bin/omg" ~/.local/bin/omg   # ensure ~/.local/bin is on PATH
omg --version

# 3) Wire a project
cd /path/to/your-project
omg setup
omg doctor
```

`install-plugin.sh` runs `grok plugin install . --trust` **and** writes  
`~/.grok/hooks/omg-pretool-deny.json` with an **absolute path** into this checkout  
(plugin-bundled PreToolUse alone has been insufficient in live sessions).

### Plugin-only (half surface — not enough alone)

```bash
grok plugin install ImL1s/oh-my-grok --trust
# better pin: grok plugin install ImL1s/oh-my-grok@v0.3.2 --trust
```

This installs skills/agents from GitHub. It does **not** put `omg` on PATH and does **not** guarantee the global soft-gate. Prefer **Full install** unless you only need in-session skills.

### Upgrade / relocate / uninstall

| Action | Commands |
|--------|----------|
| Upgrade | `cd ~/.local/share/oh-my-grok && git pull && ./scripts/install-plugin.sh` |
| Relocate clone | Re-run `./scripts/install-plugin.sh` + refresh `ln -sf …/bin/omg ~/.local/bin/omg` |
| Uninstall plugin | `grok plugin uninstall oh-my-grok` (name from `grok plugin list`) |
| Remove soft-gate | `rm -f ~/.grok/hooks/omg-pretool-deny.json` |
| Remove CLI link | `rm -f ~/.local/bin/omg` |

`omg setup` only scaffolds **project** files (`.omg/`, AGENTS fragment). It does **not** install the plugin.

Smoke after install:

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
   # or: omg autopilot complete --run RUN  (same-process accept+verify)
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

**UltraQA / accept tips (v0.3.2+):**

- Freeze only **allowlisted** commands (`python3 -m pytest …`, project `.py`, `true`/`false`). Illegal basenames (`grep`, `test`, `omg`, `python -c`) fail **at freeze** with a tip — not only at run.
- Quote pytest markers: `python3 -m pytest -q -m 'not live'` (unquoted `-m not live` is auto-coalesced when possible).
- After clean UltraQA, **`prd.json` is optional** — `omg accept` / `omg autopilot complete` materialize it from clean scenarios (never overwrites an existing operator PRD).
- If `omg accept` already set `verified`, `omg autopilot complete` **short-circuits** (syncs `autopilot_phase`, no second full acceptance).

---

## CLI vs skills

| Surface | Where | Examples |
|---------|--------|----------|
| **Terminal CLI** | shell | `omg setup`, `omg ulw`, `omg accept`, `omg doctor` |
| **In-session skills** | Grok plugin session | `omg-autopilot`, `omg-ultrawork`, `omg-ralph`, `omg-ralplan`, `omg-using` |

Both share HARD RULES: spawn only via Grok; CLI owns `verified`; no external agent CLIs as default workers.

**In-session autopilot:** see [Autopilot skill](#autopilot-skill-in-session) below · full guide: [`docs/autopilot.md`](docs/autopilot.md).

---

## Autopilot skill (in-session)

End-to-end work inside a **Grok Build chat**: the model loads skill **`omg-autopilot`**, drives the CLI phase machine, and spawns Grok-native workers.  
This is **not** OMC Stop hard-pin — if the turn ends, say **「繼續」 / `continue`** or re-invoke the skill and run `omg autopilot status`.

### How to invoke

| Way | Example |
|-----|---------|
| Natural language | 「autopilot 幫我做完 X」「full auto」「build me …」「handle it all」 |
| Slash / skill | `/oh-my-grok:omg-autopilot` + goal text（Grok plugin skill name） |
| CLI only (no skill) | Terminal `omg autopilot start "…"` then you (or the agent) run transitions by hand |

Skill playbook lives at [`skills/omg-autopilot/SKILL.md`](skills/omg-autopilot/SKILL.md).

### Skill vs CLI

| Layer | Role |
|-------|------|
| **Skill `omg-autopilot`** | In-session playbook: interview → plan → implement → review → QA → accept; uses `spawn_subagent` |
| **`omg autopilot *` CLI** | Legal phases, destination gates, stamps; **only** path that can end in `verified` via `complete` / `accept` |

Agents **must not** hand-write `verified` under `.omg/state/`.

### Phase machine

```text
interview → ralplan → implement → review → (rework) → qa → acceptance → verified
```

| Enter | Gate (CLI-enforced) |
|-------|---------------------|
| `ralplan` | interview closed (`interview_complete`) or `--skip-interview` |
| `implement` | plan consensus (`consensus: true` / ralplan APPROVE) |
| `qa` | structured review clean |
| `acceptance` | UltraQA clean |
| `verified` | `omg autopilot complete` (or prior `omg accept` + complete short-circuit) |

### Minimal user flow (skill session)

```text
You:  /oh-my-grok:omg-autopilot 實作功能 X（含測試與验收）
Grok: omg doctor → omg autopilot start "…"
      … interview / ralplan / implement / review / qa …
      omg autopilot complete --run RUN
You:  （若中斷）continue
      omg autopilot status --run RUN
```

### CLI cheat sheet

```bash
omg autopilot start "GOAL TEXT"
omg autopilot start "GOAL" --skip-interview   # requirements already closed
omg autopilot status --run RUN
omg autopilot transition --run RUN --phase PHASE \
  --evidence-json '{…}' --reason "…"
omg autopilot complete --run RUN              # terminal: accept+verify (or short-circuit)
omg cancel                                    # abort active run
```

### Companion skills (same run)

| Skill | Phase |
|-------|--------|
| `omg-deep-interview` | interview |
| `omg-ralplan` | plan consensus |
| `omg-ultrawork` / `omg-ralph` | implement (parallel / persist) |
| `omg-dual-review` | review |
| `omg-ultraqa` | qa |
| `omg-ultragoal` | multi-story ledger (optional) |
| `omg-cancel` | abort |

Full detail, anti-patterns, and resume rules: **[`docs/autopilot.md`](docs/autopilot.md)**.

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
# Escape hatch (default OFF). Set only for trusted local experiments.
# Prefer: omg ask …  (sets allow only in the advisor child env)
# Never put this in your shell profile / project .env for day-to-day use.
export OMG_ALLOW_EXTERNAL_CLI=1   # process-env only; never parse from command text
```

---

## Commands

```text
omg {setup,doctor,state,cancel,resume,wiki,hud,lsp,interview,goal,accept,
     integrate,worker,review,qa,autopilot,ulw,ralph,ralplan,ask,pipeline,
     dual-review} ...
```

| Command | Purpose |
|---------|---------|
| `omg setup` / `omg doctor` | Scaffold `.omg/` · health (+ `--strict`) |
| `omg state` / `omg cancel` | Active run · process-group cancel |
| `omg resume` | Smart resume routing + `.omg/state/RESUME.md` (SessionStart inject) |
| `omg wiki` / `hud` / `lsp` | Local markdown wiki · statusline pack · optional language-tool probe |
| `omg interview …` | Deep-interview requirements gate |
| `omg goal …` | Hash-chained ultragoal ledger + tail repair (**no host `/goal`** on Grok — repo ledger only) |
| `omg ulw` / `ralph` / `ralplan` | Parallel / persist / plan-only modes |
| `omg worker own\|prepare\|seal\|join` | ULW ownership + worktree + envelopes |
| `omg integrate` | Cherry-pick ULW envelopes (does **not** set verified alone) |
| `omg review` / `omg qa` | Hash-bound review · UltraQA (**QA clean ≠ verified**) |
| `omg autopilot …` | Strict phases; `start` / `transition` / `status` / `complete` — see [Autopilot skill](#autopilot-skill-in-session) |
| `omg accept` | Freeze PRD + run; only path that may `verified` (or materialize PRD from clean UltraQA) |
| `omg ask` | Trusted external advisor broker (not a worker) |
| `omg pipeline` / `dual-review` | Scripted pipeline · interim critic→verifier |
| `omg --madmax` | **Host launcher** (not a mode FSM): full-open Grok in a **new tmux session** each launch |

### Host launcher: `omg --madmax`

Break-glass interactive Grok with `--always-approve` + `--permission-mode bypassPermissions`.  
Requires `grok` + **tmux** (hard fail if missing outside tmux). Continuity: `grok --continue` / `--resume`, not attach-old-session.

```bash
omg --madmax                         # new tmux session + attach
omg --madmax "fix the failing tests"
# already inside $TMUX → grok in current pane
# headless (-p / --single / --prompt-file) → no tmux (stdout preserved)
# Root --yolo is mode elevation only — not a madmax alias
```

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

# Autopilot CLI (skill omg-autopilot drives the same machine in-session)
omg autopilot start "ship feature X with tests"
omg autopilot start "ship feature X" --skip-interview
omg autopilot status --run RUN
omg autopilot complete --run RUN

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
.omg/wiki/                    project markdown wiki
.omg/state/RESUME.md          one-shot continuity pack (SessionStart / omg resume)
```

---

## Skills & agents

| Skill | Use when |
|-------|----------|
| `omg-using` | Bootstrap / which mode (+ **read RESUME.md** first) |
| **`omg-autopilot`** | **In-session end-to-end** — [usage](#autopilot-skill-in-session) · [full guide](docs/autopilot.md) · [SKILL.md](skills/omg-autopilot/SKILL.md) |
| `omg-ultrawork` | Parallel fan-out |
| `omg-ralph` | Persist until verified |
| `omg-ralplan` | Plan consensus |
| `omg-deep-interview` | Requirements gate (Socratic CLI) |
| `omg-ultragoal` | **In-session** multi-story goal ledger (`omg goal *`; no host `/goal`) |
| `omg-ultraqa` | Bounded QA repair loop (**QA ≠ verified**) |
| `omg-wiki` / `omg-hud` / `omg-lsp` | Knowledge · statusline · honest LSP probes |
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

PYTHONPATH=. python3 -m pytest -q -m "not live"
grok plugin validate .
./bin/omg doctor
./scripts/smoke.sh
```

| Layer | Command |
|-------|---------|
| Unit (default gate) | `PYTHONPATH=. python3 -m pytest -q -m "not live"` |
| Hermetic e2e | `OMG_E2E=1 ./scripts/smoke.sh` |
| Live (local evidence) | `./scripts/live_suite.sh --quick` / `--full` → `docs/research/live/` (gitignored) |

Do not claim production isolation from unit green alone. See [`docs/research/test-matrix.md`](docs/research/test-matrix.md).

---

## Docs map

| Path | Contents |
|------|----------|
| [`docs/security-model.md`](docs/security-model.md) | Isolation layers |
| [`docs/research/core-parity-matrix-2026-07-20.md`](docs/research/core-parity-matrix-2026-07-20.md) | HAVE / NEVER scope |
| [`docs/research/omc-parity-council/`](docs/research/omc-parity-council/) | Parity council + STATUS |
| [`docs/research/live/`](docs/research/live/) | How to regenerate live suite evidence (logs gitignored) |
| [`docs/superpowers/plans/`](docs/superpowers/plans/) | Implementation plans |

---

## Changelog notes (compressed)

Full dual-review ship bar (C1–C9) is complete. Recent lines:

- **v0.2.x:** acceptance policy, run mutex, ULW integrate, ralplan FSM, worker prepare/seal, pipeline order, live suite.
- **2026-07-20 core-purpose parity:** evidence stamps, session lease, interview, goal ledger + repair, ULW ownership/join, hash-bound review, UltraQA, strict autopilot; destination gates; CLI acceptance authority for `verified`.
- **v0.2.6:** `omg --madmax` full-open host launch in tmux; OSS install dual-track + release protocol; CI smoke.
- **v0.3.2:** QA freeze allowlist UX + pytest marker coalesce; auto PRD from clean UltraQA; autopilot complete short-circuit; `autopilot_phase` sync on verified.
- **v0.3.1:** strict-v2 accept lease; residual verdict false-green; integrate/fanout/env isolation hygiene (improve-deep).
- **v0.3.0:** `omg resume` + RESUME.md; verdict fence/negation/schema-v2; ultragoal/autopilot/interview/ultraqa skills; `omg wiki` / `hud` / `lsp`.

Details: [`CHANGELOG.md`](CHANGELOG.md) · `docs/research/`.

---

## License

[MIT](LICENSE) · Copyright (c) 2026 ImL1s

See also: [CONTRIBUTING.md](CONTRIBUTING.md) · [SECURITY.md](SECURITY.md)

---

<p align="center">
  <em>Inspired by OMC · OMX · OmO — built for Grok. Unofficial; not affiliated.</em><br>
  <strong>Core purpose first. No fake parity.</strong>
</p>
