# Skills catalog (oh-my-grok)

English | [简体中文](./skills.zh.md) | [繁體中文](./skills.zh-TW.md)

**16 in-session skills** under [`skills/omg-*/SKILL.md`](../skills/).  
Same *idea* as OMC’s skill zoo, **Grok-native** runtime: playbooks + `omg` CLI stamps.

Machine catalog (aliases, classifications, pipeline metadata, continuation
policy): [`skills/catalog.json`](../skills/catalog.json) · generated table
[`docs/parity/skills-catalog.md`](./parity/skills-catalog.md). Inspect with
`omg skill list|show|resolve|resources` (never sets `verified`). Catalog-only
rows are classified only. Antigravity projections are **not** an
installed AG plugin.

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
5. Stop pin on grok **≥0.2.107** (cap **8**/turn, fail-open) pins incomplete autopilot — beyond cap or after turn end, re-invoke skill, `/loop`, or say **continue**.

---

## In-session shortcuts (OMC-style table)

| Trigger / phrase | Skill | Terminal CLI | What it does |
|------------------|-------|--------------|--------------|
| `how to use omg`, first session | `omg-using` | `omg doctor` · `omg setup` · `omg resume` | Router + install health |
| `autopilot`, `full auto`, `build me`, `handle it all` | `omg-autopilot` | `omg autopilot *` | interview→…→verified playbook |
| `ulw`, `ultrawork`, parallel | `omg-ultrawork` | `omg ulw` + `worker` + `integrate` | Parallel fan-out |
| `team N`, tmux team, parallel panes | `omg-team` | `omg team …` | Durable tmux panes — slash **`/oh-my-grok:omg-team` only** (no bare `/team`) |
| `ralph`, don’t stop, keep going | `omg-ralph` | `omg ralph` | One-story outer loop |
| `ralplan`, plan consensus | `omg-ralplan` | `omg ralplan` | Plan → critic → verifier (no code) |
| `deep interview`, clarify | `omg-deep-interview` | `omg interview *` | Requirements gate |
| `ultragoal`, multi-story, goal ledger | `omg-ultragoal` | `omg goal *` | Durable story ledger + host `/goal` session pressure |
| `ultraqa`, fix tests, retest | `omg-ultraqa` | `omg qa *` | Freeze → run → repair (**≠ verified**) |
| `dual-review`, don’t self-approve | `omg-dual-review` | `omg dual-review` · `omg review` | Critic → verifier |
| `pipeline` | `omg-pipeline` | `omg pipeline` | plan→implement→review→accept FSM |
| `ask codex` / second opinion | `omg-ask` | `omg ask` | Human broker for external CLIs |
| `cancel`, abort, kill workers | `omg-cancel` | `omg cancel` | Safe abort |
| `wiki`, project memory | `omg-wiki` | `omg wiki *` | Local markdown wiki |
| `hud`, statusline | `omg-hud` | `omg hud` | One-line run status |
| `lsp`, symbols | `omg-lsp` | `omg lsp *` | Inspect host-owned `.lsp.json`; no semantic proxy |

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
omg setup                 # grok + project (default); also: --runtime grok|antigravity|both --scope project|user
omg install-hook          # (re)install/repair just the global soft-gate; omg setup --no-global-hook opts out
# after session restart:
# read .omg/state/RESUME.md then:
omg resume
omg resume --clear   # after successfully continuing
```

> Recovery (a grok session bricked by an old checkout-path hook can't run `omg`
> through its blocked terminal): from any plain shell run
> `python3 -m omg_cli.hook_install`, or `rm "${GROK_HOME:-$HOME/.grok}/hooks/omg-pretool-deny.json"`
> to disable the soft-gate, then restart grok.

---

### `omg-autopilot` — full lifecycle (in-session)

| | |
|--|--|
| **When** | End-to-end: clarify → plan → implement → review → QA → verified |
| **Invoke** | `autopilot …` · `full auto` · `/oh-my-grok:omg-autopilot` |
| **CLI** | `omg autopilot start\|transition\|status\|await\|complete\|run` |
| **Deep guide** | [`autopilot.md`](./autopilot.md) |
| **SKILL** | [`skills/omg-autopilot/SKILL.md`](../skills/omg-autopilot/SKILL.md) |

```bash
omg autopilot start "ship feature X with tests"
# or: omg autopilot start "…" --skip-interview
omg autopilot run "ship feature X with tests" --unattended   # hands-off outer (#40)
omg autopilot run --resume RUN --unattended                  # after cap / crash
omg autopilot status --run RUN
omg autopilot await --run RUN --set   # pause for destructive/credential confirm
omg autopilot complete --run RUN
```

Phases: `interview → ralplan → implement → review → (rework) → qa → acceptance → verified`  
Stop pin on grok **≥0.2.107** (cap **8**/turn, fail-open) — beyond cap see [autopilot.md](./autopilot.md#stop-pin-honesty) (`omg autopilot run --resume … --unattended`, `/loop`, outer `omg ralph`).

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

### `omg team` — tmux team plane (default on; D1 zero-config + D3 multi-CLI + D2 staged driver + D4 scale/resume/ralph)

| | |
|--|--|
| **When** | Multi-pane ULW with real worktrees; hermetic dry-run / fixture smoke for tests |
| **Gate** | **Default on.** Kill switch `OMG_DISABLE_TMUX_TEAM=1` (legacy `OMG_EXPERIMENTAL_TMUX_TEAM=0` also disables) |
| **Skill** | `omg-team` — in-session slash **`/oh-my-grok:omg-team` only**; natural `team N …` |
| **CLI** | `omg team launch` (argv shorthand `N`/`N:role`+goal → launch; `--io-mode auto\|interactive\|headless`); also `start\|run\|scale\|resume\|status\|collect\|stop\|api\|supervisor\|panes\|capture\|focus\|key\|input\|watch\|view\|hyperplan\|security-research` |
| **Honesty** | Zero-config = grok panes; `--routing` enables multi-CLI (codex/agy/cursor/gemini) with role floors. **Integration** isolation only (ownership + seal + integrate) — **not** an execution sandbox (see `docs/security-model.md` posture table). `collect` / `run` / `scale` / `resume` never set `verified`. Scaling/resume/ralph are **lifecycle extensions** of the same team plane (no new isolation claims). Shorthand uses **split-pane** topology + seeds team API (P0′ surface; catalog v4 has 39 named ops / 28 implemented — not full OMX catalog parity; see `docs/team-operation-catalog-v4.md`). Live promotion proof: `scripts/live_team_smoke.py --live` → `LIVE_TEAM_SMOKE_OK` (2026-07-30 local; not CI-required). **No bare `/team` slash alias** — 2026-07-25 host probe (`grok inspect` / plugin skill docs): skills are `/name` or `/plugin:name`; no frontmatter to register an unnamespaced `/team` for `omg-team`, and other plugins already expose `team` skills. |

**Canonical shorthand (OMX-like):** `omg team` accepts `N` / `N:role` before the
goal and normalizes to `launch` (not a separate argparse choice named `3`).
Inside tmux, launch defaults to **same-window** (`view_mode=same_window`: leader
left, workers stacked right; detached splits + `main-vertical`). Use
`--dedicated-window` for a dedicated Team window; outside tmux / `--detach`
records `detached_session`. `stop` never kills the shared leader window for
same_window runs. Plan-only / dry-run / live JSON expose `view_mode`.

```bash
omg team launch --workers 3 --role executor --goal "fix flaky tests"
# argv shorthand (same launch path): omg team <N[:role]> "<goal>"
omg team launch --workers 2 --role executor --goal "map A and B" --dry-run
# Live launch waits for provider-ready gate (#99): schema-v2 phases
# pane_created → provider_spawned → provider_ready → task_dispatched
# (optional mailbox_ack enrichment). Legacy worker-ready v1 receipts are
# wrapper_ready_legacy only and cannot produce startup_status=running.
# process_stable requires live provider binary identity (cmdline/exe
# basename); a mislabeled ``python -c sleep`` cannot green. After
# provisional ready, a bounded post-stable observe window catches delayed
# auth/trust (TUI idle uses a longer floor). Auth that appears *after*
# finalize is out of that window by design — not an infinite watch
# (#101 pane input is separate). Timeout knob: OMG_TEAM_READY_TIMEOUT_MS
# (default 45000). Partial/zero/blocked_start leaves state for diagnosis
# and exits non-zero (no silent dry-run fallback). --no-wait →
# unverified_start only.
# `--io-mode interactive` skips supervisor ACK receipts: the leader waits
# the same timeout for TUI_READY:<nonce> on the pane TTY, then CLI-promotes
# input_ready. Timeout fails closed (no headless downgrade). Default/auto
# stay headless. Live Grok marker LIVE_TEAM_INTERACTIVE_TTY_OK is optional.
# Worker panes bootstrap silently (#100): no worker-ready JSON envelope and
# no nested-.omg shadow warnings in pane scrollback. Failures print one
# redacted line; details live in workers/<id>/bootstrap.log — inspect with
# `omg team status <run> --full` (not pane scrollback).
# Attach: inside tmux → new window + split (shared session; stop never
# kill-session). Outside TTY → new session + `tmux attach -t …` hint.
# Non-interactive without --detach fails closed.
# status (shorthand identity or --run): human table includes aggregate
# extras — topology/startup_acks, mailbox metadata (leader-fixed; no
# bodies — use startup_acks for ACK counts), api_summary, worktrees[].
# --json keeps the LOCKED field set (status_locked_view). --full / --json
# --full dumps the full aggregate for operators/scripts.
omg team status --run RUN --json
omg team status --run RUN --full
omg team status --run RUN --presentation --json
omg team status TEAM_NAME
# Identity-fenced operator pane control (#101 / #147). Prefer durable
# mailbox/task API for automation; pane input is recovery / steering only.
# PR1: current supervisor panes are headless_stream / unsupported — key/input
# refuse with E_OPERATOR_*_UNSUPPORTED even under --operator-override.
# status --full shows per-worker io_mode / input_ready / operator_input_supported
# (locked --json status_locked_view keys unchanged).
omg team panes --run RUN --json
omg team capture --run RUN --worker w1 --lines 200 --json
omg team focus --run RUN --worker w1
omg team key --run RUN --worker w1 --key Enter
omg team input --run RUN --worker w1 --text 'continue' --submit --operator-override
omg team watch --run RUN --worker w1 --interval 1
# #103: restore exact Team window/leader (or --worker via #101). Default
# resume never attaches; --json never changes the tmux client.
omg team resume --run RUN                # reconcile only
omg team resume --run RUN --view         # reconcile + restore view
omg team view --run RUN                  # view only (no relaunch)
omg team view --run RUN --print          # print attach/switch argv only
omg team stop --run RUN
```

**`omg team run`** is a **staged DRIVER** over the team plane (not a new planner/verifier):

`team-plan → team-prd → team-exec → team-verify → team-fix` (terminal: `complete` / `failed` / `blocked`).

- **team-plan / team-prd** — pass-through markers. Decomposition is the **leader’s / ralplan’s** job; `run` only consumes `--tasks-json` or `--tasks-path`.
- **team-exec** — `start_team` then `collect_team` (dry-run: start only; no tmux/subprocess).
- **team-verify** — gates a durable artifact at `stages/team-verifier.md|json` via POST-A2 `parse_verdict_file`. APPROVE → `complete`; else → `team-fix`. Does **not** author verdicts.
- **team-fix** — bounded by `--max-fix` (default 3); re-enters exec with findings; exceeding budget → `failed`.
- **`--ralph [--max-iter N]`** (D4) — outer **bounded** persistence loop (default max_iter=3 from ralph) around exec→verify→fix; records `linked_ralph` on `team.json` and `linked_team` on `stages/team-ralph.json` so stop/cancel can cancel both; still completes only on real team-verify APPROVE — **never** sets `verified`.
- Stale verify stamps are invalidated on (re)entry to exec/fix (mirror autopilot). `verified` remains behind `omg accept` only.

**Lifecycle (D4):**

- **`omg team scale --run ID --add N|--remove N [--dry-run]`** — dynamic panes under a run-dir **scale lock**. Live scale-up publishes an immutable generation-scoped **WAL** before side effects, then binds windows with `@omg_scale_nonce` + rename and **fail-closed ownership readback** (exact `display-message`; never trust mutable `session:index` alone). Pending scale-up WAL or future **identity-receipt** generations block dry-run add, remove, resume/relaunch, collect/join/integrate, and stop until the original op is recovered. `--add` respects `max_workers_cap()` and monotonic window indices; `--remove` graceful drain (idle/newest) on first attempt, **receipt-bound victims** on recovery (wrong `--remove N` fails closed with generation + task ids), kills only recorded pgids + authenticated panes (**not** the session; **no** `pkill -f`), marks `scaled_down`, preserves worktrees; never below 1 active pane. Meta commit result-loss classifies committed / not_committed / unknown via identity readback (not volatile `last_scale.actions` alone). **Not** an execution sandbox — see `docs/security-model.md`.
- **`omg team resume --run ID`** — re-read `team.json` under the same scale lock; if a relaunch WAL is pending, exact relaunch recovery runs before raw liveness reconciliation; otherwise reconcile pane liveness after leader restart (idempotent status writes). After pane reconcile/relaunch (still under the same lock), reconcile Team API task claims: preserve coherent unexpired claims byte-for-byte; release only coherent expired claims back to `pending` (version +1; old token fenced). Missing API board is a non-materializing no-op; claim corruption fails closed before mutation. Additive `claim_reconcile` on resume output (IDs/counts only — no tokens). Remain-on-exit dead panes that still match receipt identity may clean then commit as `needs_collect` when the process is absent. **Default never attaches or changes the tmux client** (script-safe). Pass `--view` to restore the exact Team window/leader pane after the lifecycle lock is released (same-session `select-*`, cross-session `switch-client`, outside-TTY `attach-session`; `--takeover` adds `-d`; `--json` never executes view effects). Pass `--provider-session` to request host ACP provider-session resume gated by `host_probe` (AVAILABLE starts/reuses a durable internal ACP stdio jobs sidecar with no-replay receipt; LEGACY/BLOCKED never spawn; missing resume → LEGACY + next_action; BLOCKED fails closed; independent of tmux view). `omg team view` restores the view without reconcile/relaunch; `--print` prints argv only. Reconcile / provider-session / tmux-view outcomes are reported separately. A public `omg team api reconcile` / MCP twin still requires a future catalog version — see `docs/team-operation-catalog-v1.md`.

```bash
omg team start --goal "parallelize A/B" --tasks-json '[{"task_id":"t1","owned_files":["a.py"]},{"task_id":"t2","owned_files":["b.py"]}]' --plan-only
omg team start --goal "parallelize A/B" --tasks-json '[{"task_id":"t1","owned_files":["a.py"]},{"task_id":"t2","owned_files":["b.py"]}]' --dry-run
# job-backed workers via durable Jobs plane (#69 PR4; fake|antigravity):
omg team start --goal "…" --tasks-json '[{"task_id":"t1","owned_files":["a.py"],"provider":"fake"}]' \
  --worker-topology=job --dry-run
# multi-CLI (role→provider); floors reject cursor-on-reviewer and unknown roles:
omg team start --goal "…" --tasks-json '[{"task_id":"t1","role":"executor","owned_files":["a.py"]}]' \
  --routing '{"executor":{"provider":"codex"}}' --dry-run
# staged pipeline (sequences existing lanes; no new planner):
omg team run --goal "x" --tasks-json '[{"task_id":"t1","owned_files":["a.py"]}]' --dry-run --max-fix 3
omg team run --goal "x" --tasks-json '[{"task_id":"t1","owned_files":["a.py"],"provider":"fake"}]' \
  --worker-topology=job --dry-run
# ralph composition (bounded outer loop; never verified):
omg team run --goal "x" --tasks-json '[{"task_id":"t1","owned_files":["a.py"]}]' --ralph --max-iter 2 --dry-run
omg team scale --run RUN --add 2 --dry-run
omg team resume --run RUN
omg team resume --run RUN --view
omg team view --run RUN --print
omg team status --run RUN --json
omg team collect --run RUN   # seal_all_tasks + integrate; never verified
omg team stop --run RUN      # kill recorded session + pgids only (no pkill -f)
omg team api catalog
omg team api replace-worker --input '{"run_id":"RUN","team_id":"t","worker":"t1","mode":"lost","expected_attempt":1,"expected_launch_generation":1,"idempotency_key":"repl-1"}' --json
omg team api read-presentation-state --input '{"run_id":"RUN","team_id":"t"}' --json
omg team api bulk-create-tasks --input '{"schema_version":1,"run_id":"RUN","team_id":"t","batch_id":"b1","idempotency_key":"k1","source":{"kind":"fixture","source_id":"s1","digest":"<sha256>"},"tasks":[{"task_key":"root","subject":"s","description":"d","depends_on":[],"requires_code_change":false,"expected_artifact":{"kind":"omg.team.test.artifact","schema_version":1,"required_fields":["summary"]}}]}' --json
omg team status --run RUN --presentation --json
# Hyperplan V1 hermetic produce + task driver + lane protocol + fixture execute (#69 PR14; compile execution_supported=false):
omg team hyperplan plan --spec SPEC.json --json
omg team hyperplan materialize --spec SPEC.json --run RUN --json
omg team hyperplan validate-decision --run RUN --input DECISION.json --json
omg team hyperplan produce-decision --run RUN --input RESULT_BUNDLE.json --json
omg team hyperplan admit-tasks --run RUN --team-id TEAM --json
omg team hyperplan collect-tasks --run RUN --team-id TEAM --json
omg team hyperplan claim-lane --run RUN --team-id TEAM --lane-id LANE --json
omg team hyperplan submit-lane-result --run RUN --team-id TEAM --claim-file CLAIM.json --result RESULT.json --json
omg team hyperplan execute --run RUN --team-id TEAM --executor fixture --input RESULT_BUNDLE.json --json
# Security Research V1 hermetic produce + task driver + lane protocol + fixture execute (#69 PR14; execution_supported=false on compile):
omg team security-research plan --spec SPEC.json --json
omg team security-research materialize --spec SPEC.json --run RUN --json
omg team security-research validate-report --run RUN --input REPORT.json --json
omg team security-research produce-report --run RUN --input RESULT_BUNDLE.json --json
omg team security-research admit-tasks --run RUN --team-id TEAM --json
omg team security-research collect-tasks --run RUN --team-id TEAM --json
omg team security-research claim-lane --run RUN --team-id TEAM --lane-id LANE --json
omg team security-research submit-lane-result --run RUN --team-id TEAM --claim-file CLAIM.json --result RESULT.json --json
omg team security-research execute --run RUN --team-id TEAM --executor fixture --input RESULT_BUNDLE.json --json
omg team api send-message --input '{"run_id":"RUN","team_id":"t","from_worker":"leader","to_worker":"w1","body":"hi"}' --json
# P0′ ops + replace-worker + read-presentation-state + bulk-create-tasks; catalog v4 = 39 named / 28 implemented (see docs/team-operation-catalog-v4.md; v1/v2/v3 goldens unchanged)
# disable: export OMG_DISABLE_TMUX_TEAM=1
```

See also `docs/team.md` for job-backed worker invariants (#69 PR4),
`docs/team-hyperplan-v1.md` for Hyperplan V1 hermetic produce + task driver +
lane worker protocol + fixture execute (#69 PR14), and
`docs/team-security-research-v1.md` for Security Research V1 (#69 PR14).

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
omg ralplan "…" --run <existing-run-id>   # reuse run (autopilot/pipeline embed)
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
| **CLI** | `omg goal init\|status\|set-host\|link-run\|start-story\|checkpoint\|block-story\|resume-story\|complete-story\|verify\|repair` |
| **SKILL** | [`skills/omg-ultragoal/SKILL.md`](../skills/omg-ultragoal/SKILL.md) |

Grok **has** slash `/goal` (session-scoped, single goal, replace-on-set; Active
bypasses Stop; restart → paused, use `/goal resume`). Multi-story ledger is
under `.omg/ultragoal/` via `omg goal *` (no OMX `get_goal`/`create_goal` tool API).  
`omg goal set-host --goal GOAL` prints a `/goal …` handoff (prompt turn only).  
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
| **CLI** | `omg ask list-advisors` · `omg ask explain <id>` · `omg ask codex\|claude\|gemini\|agy "…"` |
| **SKILL** | [`skills/omg-ask/SKILL.md`](../skills/omg-ask/SKILL.md) |

```bash
omg ask list-advisors
omg ask explain fable
omg ask codex "review this patch"
omg ask claude "second opinion on the plan"
```

`list-advisors` / `explain` are an **offline registry** of catalog facts (every harness is `unproven`; binaries are `not_probed`). They do not qualify a harness and do not run a provider.

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

### `omg-lsp` — host-owned LSP registration

| | |
|--|--|
| **When** | Inspect the public `.lsp.json` registration and local server-command availability |
| **Invoke** | `lsp` · `/oh-my-grok:omg-lsp` |
| **CLI** | `omg lsp status` · `omg lsp validate` · legacy: `check`/`symbols`/`diagnostics` → `E_LSP_HOST_OWNED` |
| **SKILL** | [`skills/omg-lsp/SKILL.md`](../skills/omg-lsp/SKILL.md) |

`omg lsp status` / `omg lsp validate` inspect host-owned `.lsp.json` without
starting a server. Status reports `semantic_proxy_count: 0`; configured but
unobserved is never healthy. Legacy `check`/`symbols`/`diagnostics` always
return `E_LSP_HOST_OWNED` / `semantic_proxy_unsupported` with exit code 1
(#28). Use Grok's host tools for semantic language operations and
`read_file` / `grep` for repository lookup.

---

### In-session MCP (`omg mcp-server`) — focused ops surface

A **FOCUSED** in-session read + proposal MCP surface, **NOT** OMC ~54-tool
parity. Exposes reads and non-authoritative proposal writes only;
`passes` / `verified` / accept are **never** MCP tools (CLI-only **and**
structurally refused when `OMG_MCP_SERVER=1`); semantic LSP operations are not
registered; no code-exec / state-mutation / authoritative-write tools.
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
`ast_grep_replace` (mutates code), all semantic LSP operations including
`goto` / `hover` / `rename` / `find_references` / `symbols` / `diagnostics`,
`shared_memory`, `session_search`, `merge_readiness`, and **any**
accept / verify / `set_verified` / token-registration tool.

---

### Product services and repository workflows (0.6.0)

These are CLI contracts rather than additional chat skills. A leader may call
them from a skill, but authority and evidence remain in the CLI artifacts.

| Command | Contract |
|---|---|
| `omg session allocate\|route` | Exact create/resume/continue/fork argv; named child UUIDs cannot be reused. |
| `omg recover` | Immutable bounded JSONL suffix; partial recovery preserves broken-chain/unknown-record warnings. |
| `omg memory put\|search\|show\|export\|import\|rescan` | Redacted deterministic project facts. |
| `omg tracker status\|project\|reconcile` | Passive generation-fenced lifecycle projection. |
| `omg compact create\|show\|render` | Lossless guidance checkpoint and restore. |
| `omg notify status\|send\|process` | Outbound-only, non-authoritative delivery queue. |
| `omg workflow install\|list\|show\|plan\|run` | Immutable workflow registry, deterministic waves, receipt-bound ship gate. |
| `omg parity run\|release-readback\|release-bundle\|release-evidence\|check\|gaps\|refresh` | Frozen W0 manifest delegation, canonical bundle/evidence producers, bundle verification, inventory check, gap listing, and plan-only upstream pin refresh. |
| `omg capabilities` / `omg native-status` | Independent capability tiers plus read-only `agents_catalog`, `skills_catalog`, `hooks_registry`, and `tools_sidecar`; no private-sidecar probing. |
| `omg agents list\|explain` | Dual-host agent/model policy inspect (#131) plus host-neutral UX (#134): `--width` / `COLUMNS` narrow-normal-wide, `NO_COLOR`, CJK aliases. Stock Grok Build uses explicit inherit; Medley caps are unsupported (not installation failed). No paid probe. Medley TUI remains #290. |
| `omg skill list\|show\|resolve\|resources` | Read-only skill catalog inspect (#70). Never sets `verified`. Host-native names such as `plan`/`goal` resolve as aliases only. |
| `omg provider antigravity capabilities\|doctor\|run` | Antigravity (`agy`) probe + headless run (#67-A/B): capabilities envelope, doctor, and `ProviderAdapter.run` (text/json/stream-json). `omg ask agy` cutover (#67-C); Team panes via `build_launch_envelope` (#67-D; supervisor owns PTY/PID/readiness). Never claims `live_call_ready`. |
| `omg visual compare` | Visual Contract V1 `compare()` wrapper (#75): reads `--input` JSON and emits a scored/blocked envelope. Callers compare `aggregate` to `threshold`. Never writes `passes`/`verified`, never decodes images, never talks to agents. Capture, overlay/diff, reviewer agents, and visual-Ralph remain later #75 work. See [visual-contract-v1.md](./visual-contract-v1.md). |
| `omg tools doctor\|serve\|lsp\|ast\|codegraph\|research` | OMG-owned sidecar (#73): semantic LSP / AST-grep / CodeGraph / opt-in research. **Not** Grok-native LSP (`omg lsp` stays host-owned). **Not** live Antigravity evidence. `omg mcp-server` still forbids `lsp.*`. See [tools-sidecar.md](./tools-sidecar.md). |
| `omg job start\|status\|wait\|collect\|cancel\|list\|retry\|auto-retry\|gc\|recover` | Durable background jobs (#68 PR1–PR5): `.omg/jobs/<id>/` store, subprocess runner owning `ProviderAdapter.run`, owner lease/heartbeat observation, explicit `recover` → `lost`, explicit `retry --attempt N` with attempt archive, bounded `auto-retry` scheduler tick (automatic class only; one pass, no daemon), terminal `gc --retention-days`, and `omg ask --background` → job_id. `--provider fake` (hermetic) and `--provider antigravity` (fail-closed preflight; stream-json default; evidence artifacts only). Fake-only flags rejected with Antigravity. Authenticated Antigravity execution not claimed. Remaining live/job-backed evidence is owned by #69; closed #68 is historical. See `docs/durable-jobs.md`. |
| `omg edit plan\|apply` | Hash-anchored edit CLI (#76): `plan` is read-only; `apply` calls `apply_hash_edit` (re-read, re-plan, atomic replace). Never writes `passes`/`verified`. Does not claim `omo.edit.hash_anchored` host parity. See `docs/hash-edit.md`. |

Workflow planning never launches a foreign CLI. The leader executes plan tasks
through Grok-native `spawn_subagent`, supplies the exact `capability_mode`, and
passes task-ID-bound receipts to `omg workflow run`. See
[workflows.md](./workflows.md).

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

Machine-readable plugin skill catalog: [`skills/catalog.json`](../skills/catalog.json)
(loader `omg_cli/skills_catalog.py`; `omg skill list|show|resolve|resources` or
`omg capabilities` → `skills_catalog`). Antigravity `SKILL.md` files under
[`docs/parity/projections/antigravity/skills/`](./parity/projections/antigravity/skills/)
are **projections only**. The 16 Grok plugin playbooks remain the in-session
surface.

Machine-readable plugin agent catalog: [`agents/catalog.json`](../agents/catalog.json)
(loader `omg_cli/agents_catalog.py`; inspect via `omg capabilities` →
`agents_catalog`). Antigravity `agent.md` files under
[`docs/parity/projections/antigravity/agents/`](./parity/projections/antigravity/agents/)
are **projections only** — not an installed AG plugin and not live AG evidence.
Team routing floors remain in `omg_cli/team/roles.py`. Dual-host model policy
(#131) consumes this catalog via `agents/model_policies.json` and
`omg agents list|explain` (Grok baseline shipped; Medley exact/receipts remain
Refs). Grok built-ins (`explore`, `plan`, `general-purpose`) are policy
profiles, not a second registry.

---

## Skill ↔ CLI matrix

| Skill | Primary CLI | Sets `verified`? |
|-------|-------------|------------------|
| omg-using | doctor / setup / resume | no |
| omg-autopilot | `autopilot *` + accept/complete | via complete/accept only |
| omg-ultrawork | `ulw` / worker / integrate | no (need accept) |
| omg-team | `team` / launch / status / stop / api | no (need accept) |
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
