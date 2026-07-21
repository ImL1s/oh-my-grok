# Skills catalog (oh-my-grok)

English | [繁體中文](./skills.zh-Hant.md)

**15 in-session skills** under [`skills/omg-*/SKILL.md`](../skills/).  
Same *idea* as OMC’s skill zoo, **Grok-native** runtime: playbooks + `omg` CLI stamps.

> **Two surfaces (like OMC CLI vs `/skill`)**  
> - **Terminal CLI:** `omg …` in your shell (state, accept, modes).  
> - **In-session skill:** natural language or `/oh-my-grok:<skill>` inside Grok Build after plugin install.  
> OMG difference: many workflows have **both** a skill playbook **and** a real CLI subcommand (`omg autopilot`, `omg ralph`, …).

---

## How to invoke a skill

| Method | Example |
|--------|---------|
| Natural language (preferred) | `autopilot 完成登入重構` · `ulw fix these three packages` · `ralph ship it` |
| Skill id (Grok plugin) | `/oh-my-grok:omg-autopilot` · `/oh-my-grok:omg-ultrawork` |
| Terminal only | `omg ralph "…"` / `omg ulw "…"` (no chat skill required) |

**Router:** if unsure which skill → load **`omg-using`** (or say “how do I use omg”).

**HARD RULES (all skills):**

1. Fan-out only via Grok `spawn_subagent` (depth 1).
2. Always set `capability_mode` (`read-write` implementers / `read-only` review).
3. Only **`omg` CLI** may set `verified` / `passes` under `.omg/state/`.
4. Cancel with `omg cancel` — never self-matching `pkill -f`.
5. No OMC Stop hard-pin — re-invoke skill or say **continue** if the turn ends.

---

## In-session shortcuts (OMC-style table)

| Trigger / phrase | Skill | Terminal CLI | What it does |
|------------------|-------|--------------|--------------|
| `how to use omg`, first session | `omg-using` | `omg doctor` · `omg setup` · `omg resume` | Router + install health |
| `autopilot`, `full auto`, `build me`, `handle it all` | `omg-autopilot` | `omg autopilot *` | interview→…→verified playbook |
| `ulw`, `ultrawork`, parallel | `omg-ultrawork` | `omg ulw` + `worker` + `integrate` | Parallel fan-out |
| `ralph`, don’t stop, keep going | `omg-ralph` | `omg ralph` | One-story outer loop |
| `ralplan`, plan consensus | `omg-ralplan` | `omg ralplan` | Plan → critic → verifier (no code) |
| `deep interview`, clarify | `omg-deep-interview` | `omg interview *` | Requirements gate |
| `ultragoal`, multi-story, goal ledger | `omg-ultragoal` | `omg goal *` | Durable story ledger (no host `/goal`) |
| `ultraqa`, fix tests, retest | `omg-ultraqa` | `omg qa *` | Freeze → run → repair (**≠ verified**) |
| `dual-review`, don’t self-approve | `omg-dual-review` | `omg dual-review` · `omg review` | Critic → verifier |
| `pipeline` | `omg-pipeline` | `omg pipeline` | plan→implement→review→accept FSM |
| `ask codex` / second opinion | `omg-ask` | `omg ask` | Human broker for external CLIs |
| `cancel`, abort, kill workers | `omg-cancel` | `omg cancel` | Safe abort |
| `wiki`, project memory | `omg-wiki` | `omg wiki *` | Local markdown wiki |
| `hud`, statusline | `omg-hud` | `omg hud` | One-line run status |
| `lsp`, symbols | `omg-lsp` | `omg lsp *` | Honest local probe (not full LSP MCP) |

**Priority when several keywords match** (from `omg-using`):  
`cancel` > `ralplan` > `autopilot` > `ultragoal` > `ralph` > `ulw`.

---

## Recommended skill chains

```text
Vague idea
  → omg-using → omg-deep-interview → omg-ralplan → omg-autopilot
     (or: omg-ralph / omg-ultrawork after plan)

Known multi-file refactor, independent slices
  → omg-ultrawork → omg integrate → omg accept

Must finish one story across many iterations
  → omg-ralph  (CLI owns max-iter outer loop)

Full lifecycle in one chat
  → omg-autopilot  (+ continue if turn ends)

Many durable stories across days
  → omg-ultragoal + per-story ralph/ulw/autopilot

Post-implement quality
  → omg-dual-review → omg-ultraqa → omg accept / omg autopilot complete
```

---

## Per-skill reference

Each skill’s **normative** playbook is its `SKILL.md`. Below is the operator summary.

### `omg-using` — bootstrap / router

| | |
|--|--|
| **When** | First use, “which skill?”, mid-session “continue” |
| **Invoke** | `how to use omg` · `/oh-my-grok:omg-using` |
| **CLI** | `omg doctor` · `omg setup` · `omg state` · `omg resume` |
| **SKILL** | [`skills/omg-using/SKILL.md`](../skills/omg-using/SKILL.md) |

```bash
omg doctor
omg setup
# after session restart:
# read .omg/state/RESUME.md then:
omg resume
omg resume --clear   # after successfully continuing
```

---

### `omg-autopilot` — full lifecycle (in-session)

| | |
|--|--|
| **When** | End-to-end: clarify → plan → implement → review → QA → verified |
| **Invoke** | `autopilot …` · `full auto` · `/oh-my-grok:omg-autopilot` |
| **CLI** | `omg autopilot start\|transition\|status\|complete` |
| **Deep guide** | [`autopilot.md`](./autopilot.md) |
| **SKILL** | [`skills/omg-autopilot/SKILL.md`](../skills/omg-autopilot/SKILL.md) |

```bash
omg autopilot start "ship feature X with tests"
# or: omg autopilot start "…" --skip-interview
omg autopilot status --run RUN
omg autopilot complete --run RUN
```

Phases: `interview → ralplan → implement → review → (rework) → qa → acceptance → verified`  
No Stop pin — say **continue** if the chat ends mid-run.

---

### `omg-ultrawork` — parallel fan-out

| | |
|--|--|
| **When** | Independent slices; parallel agents |
| **Invoke** | `ulw` · `ultrawork` · `/oh-my-grok:omg-ultrawork` |
| **CLI** | `omg ulw` · `omg worker own\|prepare\|seal[ --all]\|join` · `omg integrate` |
| **SKILL** | [`skills/omg-ultrawork/SKILL.md`](../skills/omg-ultrawork/SKILL.md) |

```bash
omg ulw "parallelize package A/B/C fixes"
omg worker own --run RUN --tasks-json '[{"task_id":"t1","owned_files":["a.py"]}]'
omg worker prepare-owned --run RUN
# workers implement in worktrees …
omg worker seal --all --run RUN   # leader seals every worktree (real head_sha; --force to re-seal)
omg worker join --run RUN
omg integrate --run RUN
omg accept --yes
```

---

### `omg team` — experimental tmux team plane (D1 zero-config + D3 multi-CLI + D2 staged driver + D4 scale/resume/ralph)

| | |
|--|--|
| **When** | Opt-in multi-pane ULW with real worktrees; hermetic dry-run for tests |
| **Gate** | `OMG_EXPERIMENTAL_TMUX_TEAM=1` (refused otherwise) |
| **CLI** | `omg team start\|run\|scale\|resume\|status\|collect\|stop` |
| **Honesty** | Zero-config = grok panes; `--routing` enables multi-CLI (codex/agy/cursor/gemini) with role floors. **Integration** isolation only (ownership + seal + integrate) — **not** an execution sandbox (see `docs/security-model.md` posture table). `collect` / `run` / `scale` / `resume` never set `verified`. Scaling/resume/ralph are **lifecycle extensions** of the same team plane (no new isolation claims). |

**`omg team run`** is a **staged DRIVER** over the team plane (not a new planner/verifier):

`team-plan → team-prd → team-exec → team-verify → team-fix` (terminal: `complete` / `failed` / `blocked`).

- **team-plan / team-prd** — pass-through markers. Decomposition is the **leader’s / ralplan’s** job; `run` only consumes `--tasks-json` or `--tasks-path`.
- **team-exec** — `start_team` then `collect_team` (dry-run: start only; no tmux/subprocess).
- **team-verify** — gates a durable artifact at `stages/team-verifier.md|json` via POST-A2 `parse_verdict_file`. APPROVE → `complete`; else → `team-fix`. Does **not** author verdicts.
- **team-fix** — bounded by `--max-fix` (default 3); re-enters exec with findings; exceeding budget → `failed`.
- **`--ralph [--max-iter N]`** (D4) — outer **bounded** persistence loop (default max_iter=3 from ralph) around exec→verify→fix; records `linked_ralph` on `team.json` and `linked_team` on `stages/team-ralph.json` so stop/cancel can cancel both; still completes only on real team-verify APPROVE — **never** sets `verified`.
- Stale verify stamps are invalidated on (re)entry to exec/fix (mirror autopilot). `verified` remains behind `omg accept` only.

**Lifecycle (D4):**

- **`omg team scale --run ID --add N|--remove N [--dry-run]`** — dynamic panes under a run-dir scale lock; `--add` respects `max_workers_cap()` and monotonic window indices; `--remove` graceful drain (idle/newest), kills only recorded pgids + windows (**not** the session; **no** `pkill -f`), marks `scaled_down`, preserves worktrees; never below 1 active pane.
- **`omg team resume --run ID`** — re-read `team.json`, reconcile pane liveness after leader restart; idempotent status writes only.

```bash
export OMG_EXPERIMENTAL_TMUX_TEAM=1
omg team start --goal "parallelize A/B" --tasks-json '[{"task_id":"t1","owned_files":["a.py"]},{"task_id":"t2","owned_files":["b.py"]}]' --dry-run
# multi-CLI (role→provider); floors reject cursor-on-reviewer and unknown roles:
omg team start --goal "…" --tasks-json '[{"task_id":"t1","role":"executor","owned_files":["a.py"]}]' \
  --routing '{"executor":{"provider":"codex"}}' --dry-run
# staged pipeline (sequences existing lanes; no new planner):
omg team run --goal "x" --tasks-json '[{"task_id":"t1","owned_files":["a.py"]}]' --dry-run --max-fix 3
# ralph composition (bounded outer loop; never verified):
omg team run --goal "x" --tasks-json '[{"task_id":"t1","owned_files":["a.py"]}]' --ralph --max-iter 2 --dry-run
omg team scale --run RUN --add 2 --dry-run
omg team resume --run RUN
omg team status --run RUN --json
omg team collect --run RUN   # seal_all_tasks + integrate; never verified
omg team stop --run RUN      # kill recorded session + pgids only (no pkill -f)
```

---

### `omg-ralph` — persistence (one story)

| | |
|--|--|
| **When** | Don’t stop until verified; multi-iter one goal |
| **Invoke** | `ralph` · `keep going until done` · `/oh-my-grok:omg-ralph` |
| **CLI** | `omg ralph "goal"` (`--max-iter N`) |
| **SKILL** | [`skills/omg-ralph/SKILL.md`](../skills/omg-ralph/SKILL.md) |

```bash
omg ralph "ship the auth migration" --max-iter 5
```

Skill = **one iteration** playbook; **CLI outer loop** owns max-iter + re-launch.

---

### `omg-ralplan` — plan consensus (no code)

| | |
|--|--|
| **When** | Steelman plan before coding |
| **Invoke** | `ralplan` · `plan consensus` · `/oh-my-grok:omg-ralplan` |
| **CLI** | `omg ralplan "…"` |
| **SKILL** | [`skills/omg-ralplan/SKILL.md`](../skills/omg-ralplan/SKILL.md) |

```bash
omg ralplan "consensus plan for auth refactor" --safe
# FSM: draft → critic → revise → verifier → APPROVE
# then: omg ulw / omg ralph / omg autopilot
```

---

### `omg-deep-interview` — requirements gate

| | |
|--|--|
| **When** | Vague goals, ambiguity, brownfield scope |
| **Invoke** | `deep interview` · `clarify requirements` · `/oh-my-grok:omg-deep-interview` |
| **CLI** | `omg interview start\|answer\|status\|pressure-pass\|close` |
| **SKILL** | [`skills/omg-deep-interview/SKILL.md`](../skills/omg-deep-interview/SKILL.md) |

```bash
omg interview start "rebuild billing" --profile standard
omg interview status --run RUN
omg interview answer --run RUN --question-id Q1 --text "…"
omg interview pressure-pass --run RUN --text "assumptions…"
omg interview close --run RUN
```

---

### `omg-ultragoal` — multi-story ledger

| | |
|--|--|
| **When** | Several durable stories, depends_on, cross-session resume |
| **Invoke** | `ultragoal` · `goal ledger` · `/oh-my-grok:omg-ultragoal` |
| **CLI** | `omg goal init\|status\|link-run\|start-story\|checkpoint\|block-story\|resume-story\|complete-story\|verify\|repair` |
| **SKILL** | [`skills/omg-ultragoal/SKILL.md`](../skills/omg-ultragoal/SKILL.md) |

Grok has **no host `/goal`** — ledger is only under `.omg/ultragoal/`.  
`omg goal verify` needs linked run already **verified** via accept/complete.

---

### `omg-ultraqa` — QA repair loop

| | |
|--|--|
| **When** | Adversarial QA, retest until green, post-review |
| **Invoke** | `ultraqa` · `fix failing tests` · `/oh-my-grok:omg-ultraqa` |
| **CLI** | `omg qa freeze\|run\|status` |
| **SKILL** | [`skills/omg-ultraqa/SKILL.md`](../skills/omg-ultraqa/SKILL.md) |

```bash
omg qa freeze --run RUN --scenarios-json \
  '[{"id":"unit","command":"python3 -m pytest -q -m '"'"'not live'"'"'"}]'
omg qa run --run RUN
omg qa status --run RUN
```

**QA clean ≠ verified.** Then `omg accept` or `omg autopilot complete`.  
Freeze rejects `grep` / `test` / `omg` / `python -c` (v0.3.2+ tips).

---

### `omg-dual-review` — critic → verifier

| | |
|--|--|
| **When** | Don’t self-approve; independent review |
| **Invoke** | `dual-review` · `/oh-my-grok:omg-dual-review` |
| **CLI** | `omg dual-review "…"` · `omg review --run RUN …` |
| **SKILL** | [`skills/omg-dual-review/SKILL.md`](../skills/omg-dual-review/SKILL.md) |

Does **not** set `verified`. CLI path is sequential Grok launches (permanent PARTIAL vs native parallel dual-review).

---

### `omg-pipeline` — scripted plan→accept

| | |
|--|--|
| **When** | CLI-owned composition without full autopilot skill |
| **Invoke** | `pipeline` · `/oh-my-grok:omg-pipeline` |
| **CLI** | `omg pipeline "goal"` |
| **SKILL** | [`skills/omg-pipeline/SKILL.md`](../skills/omg-pipeline/SKILL.md) |

```bash
omg pipeline "goal"
omg pipeline "goal" --plan-only
omg pipeline "goal" --skip-plan --implement ulw
omg pipeline "goal" --dry-run
```

Prefer **`omg-autopilot`** for in-session multi-phase with human-in-the-loop chat.

---

### `omg-ask` — external advisors (human only)

| | |
|--|--|
| **When** | Codex / Claude / Gemini second opinion |
| **Invoke** | `ask codex …` · `/oh-my-grok:omg-ask` |
| **CLI** | `omg ask codex\|claude\|gemini "…"` |
| **SKILL** | [`skills/omg-ask/SKILL.md`](../skills/omg-ask/SKILL.md) |

```bash
omg ask codex "review this patch"
omg ask claude "second opinion on the plan"
```

**Never** a default product worker. Agents must not shell advisors unless the **user** asked.

---

### `omg-cancel` — abort

| | |
|--|--|
| **When** | Stuck run, wrong goal, kill workers |
| **Invoke** | `cancel` · `stop omg` · `/oh-my-grok:omg-cancel` |
| **CLI** | `omg cancel` · `omg cancel --run ID` |
| **SKILL** | [`skills/omg-cancel/SKILL.md`](../skills/omg-cancel/SKILL.md) |

```bash
omg state
omg cancel
omg cancel --run 20260720T…-…
```

---

### `omg-wiki` — local knowledge

| | |
|--|--|
| **When** | Capture decisions, search past notes |
| **Invoke** | `wiki` · `/oh-my-grok:omg-wiki` |
| **CLI** | `omg wiki list\|ingest\|query` |
| **SKILL** | [`skills/omg-wiki/SKILL.md`](../skills/omg-wiki/SKILL.md) |

```bash
omg wiki list
omg wiki ingest --title "Auth decision" --text "…" --tags "arch"
omg wiki query "auth"
```

Not run/`verified` authority.

---

### `omg-hud` — statusline

| | |
|--|--|
| **When** | One-line mode\|status\|stage pack |
| **Invoke** | `hud` · `/oh-my-grok:omg-hud` |
| **CLI** | `omg hud` · `omg hud --run RUN` · `omg hud --json` |
| **SKILL** | [`skills/omg-hud/SKILL.md`](../skills/omg-hud/SKILL.md) |

---

### `omg-lsp` — language probe (honest)

| | |
|--|--|
| **When** | Symbols / check; **not** full LSP MCP |
| **Invoke** | `lsp` · `/oh-my-grok:omg-lsp` |
| **CLI** | `omg lsp status` · `omg lsp check path.py` · `omg lsp symbols path.py` · `omg lsp diagnostics path.py` |
| **SKILL** | [`skills/omg-lsp/SKILL.md`](../skills/omg-lsp/SKILL.md) |

Prefer Grok `read_file` / `grep`. `symbols` / `diagnostics` use stdlib `ast`
(Python only; diagnostics are **syntax-only**, not type-checking). Optional
pyright via `check` if installed.

---

### In-session MCP (`omg mcp-server`) — focused ops surface

A **FOCUSED** in-session read + proposal MCP surface, **NOT** OMC ~54-tool
parity. Exposes reads and non-authoritative proposal writes only;
`passes` / `verified` / accept are **never** MCP tools (CLI-only **and**
structurally refused when `OMG_MCP_SERVER=1`); LSP is a local `ast` probe, not
a semantic bridge; no code-exec / state-mutation / authoritative-write tools.
This is the “different alignment” for in-session **workflow** capability, not
tool-count parity.

```bash
# Register with Grok (stdio; scope user|project):
grok mcp add omg omg -- mcp-server
# or:
omg mcp-install --print-only   # shows the grok command
omg mcp-install                # runs grok mcp add when grok is on PATH
omg mcp-server                 # stdio JSON-RPC (sets OMG_MCP_SERVER=1)
```

| Tool | Kind | Backing |
|------|------|---------|
| `omg_state_status` | read | `hud.hud_pack` / run view |
| `omg_state_read` | read | `state.load_run` / `load_run_view` |
| `omg_state_list_active` | read | active pointer + runs list |
| `omg_note_read` / `omg_note_write` | read / proposal | `.omg/notepad.md` |
| `omg_wiki_query` / `omg_wiki_list` / `omg_wiki_ingest` | read / proposal | `.omg/wiki/` |
| `omg_project_memory_read` / `omg_project_memory_add_note` | read / proposal | `.omg/project-memory.json` |
| `omg_artifact_write` | proposal only | `.omg/artifacts/` |
| `omg_lsp_symbols` / `omg_lsp_diagnostics` | read | `lsp_tools` ast probe |
| `omg_resume_context` | read | resume pack + `RESUME.md` |

**Security (three load-bearing mechanisms):**

1. **Curated allowlist** — only the tools above; registry tests fail-closed.
2. **Structural refusal** — `set_verified` / `register_cli_acceptance_token` raise
   when `OMG_MCP_SERVER=1`.
3. **Path confinement** — every write resolves under
   `.omg/notepad.md` / `.omg/wiki/` / `.omg/artifacts/` / `.omg/project-memory*`;
   rejects `.omg/state/**` and `..` / symlink traversal.

**Deliberately excluded (OMC ships some of these; OMG does not):**
`state_write`, `state_clear` (authoritative), `python_repl` (arbitrary exec),
`ast_grep_replace` (mutates code), semantic LSP
`goto` / `hover` / `rename` / `find_references` (keep the ast probe only),
`shared_memory`, `session_search`, `merge_readiness`, and **any**
accept / verify / `set_verified` / token-registration tool.

---

## Agents (roles used by skills)

| Agent | Typical `capability_mode` | Role |
|-------|---------------------------|------|
| `omg-orchestrator` | leader | Decompose + coordinate |
| `omg-executor` | `read-write` (no shell) | Implement |
| `omg-debugger` | `read-write` (no shell) | Root-cause / regression / build-fix |
| `omg-designer` | `read-write` (no shell) | UI/UX implementation |
| `omg-writer` | `read-write` (no shell) | README / API docs / comments |
| `omg-test-engineer` | `read-write` (no shell) | Test strategy / coverage / flaky hardening |
| `omg-critic` / `omg-verifier` | `read-only` | Challenge / evidence |
| `omg-code-reviewer` / `omg-architect` | `read-only` | Structured review lanes |
| `omg-security-reviewer` | `read-only` | OWASP / secrets / unsafe patterns |
| `omg-qa-tester` / `omg-analyst` | see taxonomy | QA scenarios / interview analysis |

Machine-readable posture / class floors for team routing live in
`omg_cli/team/roles.py` (`role_posture`, `role_class`, `is_reviewer_or_verifier`).
Grok built-ins (`explore`, `plan`, `general-purpose`) still fill ad-hoc gaps.

---

## Skill ↔ CLI matrix

| Skill | Primary CLI | Sets `verified`? |
|-------|-------------|------------------|
| omg-using | doctor / setup / resume | no |
| omg-autopilot | `autopilot *` + accept/complete | via complete/accept only |
| omg-ultrawork | `ulw` / worker / integrate | no (need accept) |
| omg-ralph | `ralph` | via outer accept path |
| omg-ralplan | `ralplan` | no |
| omg-deep-interview | `interview *` | no |
| omg-ultragoal | `goal *` | via linked run accept + `goal verify` |
| omg-ultraqa | `qa *` | **never** |
| omg-dual-review | `dual-review` / `review` | **never** |
| omg-pipeline | `pipeline` | via final accept stage |
| omg-ask | `ask` | no |
| omg-cancel | `cancel` | no |
| omg-wiki / hud / lsp | wiki / hud / lsp | no |
| *(MCP surface)* | `mcp-server` / `mcp-install` | **never** (structurally refused) |

---

## Related docs

- [README.md](../README.md) — install + CLI reference  
- [autopilot.md](./autopilot.md) — autopilot deep dive  
- [security-model.md](./security-model.md) — isolation honesty  
- [research/](./research/) — parity / stop-continuation history (not day-to-day)  
