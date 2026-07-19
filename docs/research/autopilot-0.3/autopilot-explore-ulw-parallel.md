# ULW true-parallel product path — end-to-end map

**Date:** 2026-07-20  
**Repo:** `<repo-root>`  
**Scope:** Trace `omg ulw` → modes → skill → prepare/seal → integrate; gap vs multi-worktree ULW; minimal product change set for multi-worker as **default happy path**.

---

## 1. CLI → modes → prompt → grok (default path)

### Entry

| Step | Path / symbol | Behavior |
|------|----------------|----------|
| Binary | `<repo-root>/bin/omg` | Adds repo root to `sys.path`, calls `omg_cli.main.main` |
| Router | `omg_cli/main.py` → `cmd_mode` | Mode name = subcommand (`ulw` / `ralph` / `ralplan`); goal = remaining argv words |
| Fanout branch | `cmd_mode` ~L88–121 | `fanout` default **`skill`**. `--fanout process` only for `ulw`, and only if `OMG_EXPERIMENTAL_PROCESS_FANOUT=1`; else exit 2 |
| Default launch | `cmd_mode` → `modes.run_mode(...)` | Single-leader path |

### `run_mode` (ULW defaults)

Source: `omg_cli/modes.py` `run_mode` (~L607+).

1. **Defaults:** `max_iter=1` (`DEFAULT_MAX_ITER["ulw"]`); `require_acceptance=False` for ulw (True only for ralph by default).
2. **Run create:** `create_run(mode="ulw", goal=…, extra={max_iter, yolo, safe, require_acceptance, base_sha?})`.
3. **`base_sha` (ULW only):** `integrate.git_rev_parse_head(root)` best-effort into run extra so later envelopes can be base-checked (~L675–687).
4. **Status:** `write_status(..., "running")`.
5. **Argv:** `build_grok_argv(mode="ulw", goal, cwd=root, run_id=…, skill_root=plugin_root())`.
6. **Launch:** `_launch_grok` → materialize prompt file → `subprocess.Popen(argv)`.
7. **Post-launch:** optional acceptance/verify if PRD has commands; else `status=completed` with note *completed without CLI acceptance; verified remains false*. **Does not call `integrate_results`.**

### `build_prompt` / `build_grok_argv` / `--prompt-file`

| Piece | Source | Content |
|-------|--------|---------|
| Skill body | `MODE_SKILL_REL["ulw"]` = `skills/omg-ultrawork/SKILL.md` via `load_skill_body` | Full skill + frontmatter |
| HARD RULES | `HARD_RULES_REMINDER` | spawn-only fan-out, no external agent CLIs, capability_mode contract, CLI owns verified |
| Mode / run | `## Active mode: ulw`, `## Run id: …` | Identity |
| Capability contract | Injected block after skill | Implementers **must** `read-write` (no Execute); critic/explore `read-only`; children no spawn |
| Goal | `## Goal` | User text |
| Soft nudge | final line | *Prefer spawn_subagent for parallel work* (not hard-enforced by CLI) |

Argv shape (`build_grok_argv`):

```text
grok --cwd <root> --output-format plain
     [--permission-mode plan | bypassPermissions + --always-approve]
     [-p <prompt>]   # then rewritten
```

**Critical rewrite** (`_materialize_prompt_file`): skill YAML starts with `---` which Grok CLI rejects as `-p` value → write `.omg/state/runs/<run_id>/last_prompt.md` and replace with:

```text
--prompt-file <run_dir>/last_prompt.md
```

Also writes `last_argv.json`, `pid` / `pid.json` (with starttime/pgid for cancel). Default timeout **3600s**.

**Shell clamp:** `disallow_shell` **not** applied to ulw/ralph leaders (workers rely on spawn `capability_mode`).

### Alternate path: process fanout (not default)

`omg_cli/fanout.py` `run_process_fanout`: N× independent `grok -p` under `workers/wNN.*`; shared goal “claim one slice”; multi-PID cancel; **no** auto-decompose, **no** auto worktree prepare, **no** auto integrate. Env-gated; product isolation story remains **spawn_subagent**.

```text
omg ulw "goal"
  → main.cmd_mode (fanout=skill)
  → modes.run_mode(mode=ulw)
  → create_run + base_sha
  → build_prompt(skill + HARD RULES + goal)
  → build_grok_argv → --prompt-file last_prompt.md
  → grok (single leader process)
  → status completed|failed|verified?
  → [NO auto integrate]
```

---

## 2. `skills/omg-ultrawork/SKILL.md`: spawn required or leader solo?

**Path:** `<repo-root>/skills/omg-ultrawork/SKILL.md`

### Requires spawn (when parallel)

- HARD RULE: *Fan-out ONLY via Grok `spawn_subagent` (depth=1)*.
- Playbook §2: emit **multiple** `spawn_subagent` in one turn for independent slices.
- Write-heavy: isolation worktree + `background: true` + capability_mode.
- Children must not re-spawn.

### Explicitly allows leader solo

| Clause | Implication |
|--------|-------------|
| *Do not use when — Single tiny sequential edit — work directly; no fan-out tax* (L28) | Solo leader is **in-skill** for tiny goals |
| *Shared-file or prerequisite-heavy slices stay on the leader or run staged* (L54) | Leader may implement |
| Orchestrator agent: *not the primary writer when capable workers exist* | Preference, not CLI hard gate |
| Injected prompt: *Prefer spawn_subagent* | Soft language |

**Conclusion:** Skill is **spawn-first for multi-slice parallel**, but **leader-solo is legal** for tiny/shared-path work. CLI does **not** verify that any child was spawned. Live gate L-ULW-1 exploits the solo escape hatch (one-file goal).

### Playbook chain (intended multi-worker)

1. Decompose + acceptance criteria  
2. Parallel `spawn_subagent` (RW implementers / RO explore-plan-critic-verifier)  
3. Wait/join  
4. Write-heavy children leave envelopes → `omg integrate`  
5. Leader verification / `omg accept` — never self-set `verified`

---

## 3. Worker prepare / seal + integrate

### Why prepare/seal exists

`omg_cli/workers.py` docstring: RW workers **lack Execute/shell**, so they cannot `git commit`. Leader/operator runs CLI to create worktree and seal envelopes. **Only omg CLI owns** `.omg/artifacts/ulw-results/`.

### `omg worker prepare --task ID [--run ID]`

- CLI: `main.cmd_worker` + `workers.prepare_task`
- Creates `.omg/worktrees/<run_id>/<task_id>` via `git worktree add -b omg/<run>/<task> HEAD`
- Fallback: mkdir + `OMG_WORKTREE_NOTE.txt` clone path if git worktree fails
- **task_id** regex: `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`
- Requires active run or `--run`

### `omg worker seal --task ID [--message] [--status ok|failed] [--evidence]`

- `git add -A` + `git commit` in worktree when dirty
- **Fail-closed for ok:** refuse `status=ok` if head==base, no changes, still dirty after commit, or non-git worktree
- Writes envelope atomically to `.omg/artifacts/ulw-results/<task_id>.json`

### Envelope required fields

**Skill JSON (human contract)** + **`integrate.REQUIRED_ENVELOPE_KEYS`**:

| Field | Required | Notes |
|-------|----------|-------|
| `task_id` | yes | non-empty string |
| `base_sha` | yes | 7–64 hex; should match run `base_sha` |
| `head_sha` | yes | worker commit to apply |
| `worktree_path` | yes | under project root or `.omg/worktrees` (whitelist) |
| `status` | yes | `ok` \| `failed` only |
| `changed_files` | yes | list[str]; must match `git diff --name-only base head` (anti-forge empty claim) |
| `evidence` | optional | string |
| seal extras | optional | `writer=omg-cli`, `run_id`, `sealed_at`, `message`, `note` |

**When envelopes appear:** only after **`omg worker seal`** (or hand-written JSON that passes validate — product path is seal). Default `omg ulw` **never** creates envelopes. Pipeline ULW stage **expects** them and fails on `missing`.

### Integrate

**Path:** `omg integrate` → `integrate.integrate_results`

1. Load/validate/sort envelopes by `task_id`
2. No envelopes → result `status=missing` (not exception)
3. Non-dry: `preflight_clean_tree` (ignore `.omg/` dirt only)
4. Per ok envelope: base_sha match run → worktree allowlist → fetch objects → ancestry + no merges + changed_files verify (+ optional `--require-squash`) → `git cherry-pick base..head`
5. Conflict / failed: abort pick; if any prior apply, `reset --hard start_sha` (`partial_reset`)
6. Writes `.omg/state/runs/<run_id>/integrate.result.json` (`writer: omg-cli`)
7. **Never sets `verified`**

### Pipeline vs bare `omg ulw`

| | `omg ulw` | `omg pipeline … --implement ulw` |
|--|-----------|-----------------------------------|
| Launch | single grok leader | same implement stage |
| Integrate | **manual** | **required** stage; missing envelopes → fail for ulw |
| Dual-review / accept | optional later | stages after integrate |

`pipeline._should_integrate`: true if implement==`ulw` **or** any envelope files exist.

---

## 4. Gap: “live ulw wrote one file as leader” vs “real multi-worktree ULW”

### Live evidence (leader solo)

- Fixture: `scripts/fixtures/live/ulw_goal.txt` — create single file `live_ulw_ok.txt` / `LIVE-ULW-OK`; *prefer completing without extra scope* (no MUST-spawn).
- Suite report: `docs/research/live-gates-2026-07-20-suite.md` — **L-ULW-1 PASS**.
- Run example: `docs/research/live/ulw-runs-20260719T190456Z/20260719T190549Z-86c1dc5b/status.json`:
  - `mode=ulw`, `base_sha` set, `status=completed`, `verified=false`
  - Goal = one-file create; **no** requirement to spawn
- Cap-spawn is a **separate** gate (`cap_spawn_goal.txt` MUST spawn one child) — proves capability isolation, **not** multi-worktree product ULW.

### What “real multi-worktree ULW” needs (product end-to-end)

```text
create_run(base_sha)
  → leader decomposes N independent slices
  → for each write slice: prepare worktree + spawn RW child cwd=worktree
  → children edit only their paths (no shell)
  → leader/operator: seal each task → envelopes
  → omg integrate (cherry-pick in task_id order)
  → omg accept / leader checks
  → verified only via CLI acceptance
```

### Gap inventory

| Layer | Today | Multi-worktree need |
|-------|-------|---------------------|
| CLI launch | 1× `grok -p` leader | same (spawn is in-session) **or** supervised prepare |
| Decompose | prompt/skill only | structured tasks (optional CLI `tasks.json`) |
| Worktree provision | **manual** `omg worker prepare` | auto before/with spawn |
| Spawn | skill-instructed; **not enforced** | hard: N≥1 write workers for multi-file goals; capability_mode |
| Seal | **manual** `omg worker seal` | leader post-join seal loop (CLI) |
| Envelopes | absent on L-ULW-1 | required for integrate |
| Integrate after ulw | **not** in `run_mode` | auto or hard-gate “not integrated” |
| Live gate | one-file leader write | 2–3 worktrees prepare→edit→seal→integrate matrix |
| Process fanout | experimental N× grok, shared cwd goal | **not** substitute for spawn+worktree story |

**Bottom line:** Product **machinery** for multi-worktree ULW exists (prepare/seal/integrate/tests). Default **happy path** is still **prompt theater + leader may solo**; live “ulw works” only proves single-process goal completion.

---

## 5. Minimal product change set — multi-worker as DEFAULT happy path

Goal: make multi-worktree parallel the path of least resistance, without relying on experimental process fanout.

### P0 — Close the loop in CLI (smallest high leverage)

1. **`omg ulw` post-leader integrate gate (opt-out, not opt-in)**  
   - After grok returns, if any envelopes under `ulw-results/`, run `integrate_results` (or fail with “run `omg integrate`”).  
   - Flag: `--no-integrate` for solo-debug only.  
   - Align bare `ulw` with pipeline ULW expectations.

2. **Skill + prompt: hard multi-slice contract when goal is not “tiny”**  
   - Replace soft *Prefer spawn* with:  
     - If ≥2 independent write slices → **MUST** prepare+spawn ≥2 RW workers; leader glue only.  
     - Tiny single-file exception remains explicit (matches live L-ULW-1).  
   - Inject concrete CLI cheatsheet in `build_prompt` for ulw:
     ```text
     omg worker prepare --task tN
     # spawn with cwd = .omg/worktrees/<run_id>/tN
     omg worker seal --task tN
     omg integrate
     ```

3. **Leader playbook MUST seal via CLI**  
   - Skill today says children “leave a result envelope”; RW children **cannot** write sealed git+CLI envelopes reliably.  
   - Rewrite §4: **leader** (with shell) runs prepare before spawn and seal after join; children only edit.

### P1 — Auto-prepare on known task list (default happy path scaffolding)

4. **`omg ulw --tasks t1,t2,t3` or auto from simple decomposer**  
   - Before launch: for each task_id, `prepare_task(root, run_id, task_id)`.  
   - Inject absolute worktree paths + task table into prompt so leader does not invent paths.  
   - Default when user passes 2+ paths / `--workers N` (skill fanout, not process): prepare N worktrees named `w01…`.

5. **Post-run `omg worker seal --all`** (or seal dirty worktrees under run)  
   - Scan `.omg/worktrees/<run_id>/*`, seal each dirty tree; write envelopes; then integrate.  
   - Makes “forget seal” a CLI fix, not a human ritual.

### P2 — Live proof (product claim gate)

6. **Live matrix (council residual #6)**  
   - Hermetic: already in `tests/test_workers.py` + `test_integrate.py`.  
   - Live: 2–3 worktrees, independent files, prepare → spawn/edit (or scripted edit) → seal → integrate → assert tree.  
   - Do **not** treat L-ULW-1 as multi-worker evidence.

### Explicitly non-default

- **Do not** make `--fanout process` the happy path (violates spawn_subagent HARD RULE; env-gated for a reason).  
- **Do not** require workers to shell-commit.  
- **Do not** set `verified` without `omg accept`.

### Suggested minimal PR sequence

| PR | Change | Why first |
|----|--------|-----------|
| A | Skill §4 + `build_prompt` seal/prepare cheatsheet; leader owns seal | Correct mental model; no new APIs |
| B | `run_mode` ulw: auto-integrate if envelopes exist; fail/warn if worktrees dirty without seal | Closes product loop |
| C | `omg ulw --tasks …` pre-prepare + path injection | Default multi-worktree scaffolding |
| D | Live 3-worker prepare/seal/integrate gate | Claim language |

---

## 6. Evidence index (absolute paths)

| Topic | Path |
|-------|------|
| CLI entry | `<repo-root>/bin/omg` |
| Router + worker CLI | `<repo-root>/omg_cli/main.py` |
| Prompt + run_mode | `<repo-root>/omg_cli/modes.py` |
| Process fanout | `<repo-root>/omg_cli/fanout.py` |
| prepare/seal | `<repo-root>/omg_cli/workers.py` |
| integrate | `<repo-root>/omg_cli/integrate.py` |
| pipeline integrate gate | `<repo-root>/omg_cli/pipeline.py` |
| ULW skill | `<repo-root>/skills/omg-ultrawork/SKILL.md` |
| Orchestrator agent | `<repo-root>/agents/omg-orchestrator.md` |
| Worker tests | `<repo-root>/tests/test_workers.py` |
| Live ulw fixture | `<repo-root>/scripts/fixtures/live/ulw_goal.txt` |
| Cap-spawn fixture | `<repo-root>/scripts/fixtures/live/cap_spawn_goal.txt` |
| Live suite summary | `<repo-root>/docs/research/live-gates-2026-07-20-suite.md` |
| Example ulw status | `<repo-root>/docs/research/live/ulw-runs-20260719T190456Z/20260719T190549Z-86c1dc5b/status.json` |
| Council residuals (3-worker) | `<repo-root>/docs/research/council-v021-strictest-wins.md` |
| Parallel design | `<repo-root>/docs/research/council-v021-synthesis.md` |
| README command table | `<repo-root>/README.md` |

---

## 7. One-line verdict

**ULW CLI today = single leader `grok --prompt-file` + skill theater; multi-worktree is a complete side protocol (prepare → edit → seal → integrate) that is optional for bare `omg ulw` and only hard-gated inside `omg pipeline --implement ulw` — making multi-worker the default happy path is mostly product wiring + skill contract + live matrix, not new integrator design.**
