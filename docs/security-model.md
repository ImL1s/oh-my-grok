# oh-my-grok security model

**Canonical truth table** for isolation claims. README, skills, and doctor footers should link here rather than invent stronger wording.

Last updated: 2026-07-21 · Plugin version: **0.3.2**

## Layer table (strongest → weakest)

| Layer | Mechanism | Hardness | What it stops | Residual / failure mode |
|-------|-----------|----------|---------------|-------------------------|
| **1. capability_mode** | Host tool-kind filter on `spawn_subagent` | **Hard-ish (host)** | Implementer with `read-write`: **no Execute** → no `run_terminal_command` → no `python -c` / `npx` / agent CLI from that worker. Critic/verifier `read-only`: no write + no Execute. | Omitted mode falls back to agent defaults (`general-purpose` ≈ full). `read-write` still includes Task/spawn — depth=1 needs `disallowedTools` / parent policy. |
| **2. Agent / headless tool filter** | `disallowedTools` frontmatter; parent `--disallowed-tools` | **Hard when honored** | Extra deny of shell/spawn on executor; RO stages inject shell deny in dual-review / ralplan. | Wrong tool id, TUI ignoring headless flags, or leader still has shell. |
| **3. OS sandbox** | Grok `--sandbox` / custom deny paths | **Kernel-ish when enabled** | Path denies (e.g. `.omg/state/**`) for the Grok process. | Default off; macOS child network restrictions limited; outer `omg` CLI is outside child sandbox. |
| **4. Permission rules** | `--allow` / `--deny` rules | **Gate, not removal** | Can refuse invocations that still appear in the toolset. | Wrappers/interpreters residual; not a general allowlist engine. |
| **5. PreToolUse hooks** | `hooks/bin/pre_tool_use_deny.py` + `omg_cli.deny` | **Soft (fail-open)** | Command-position deny of `claude`/`codex`/… when hook healthy and host honors deny. Subagents **inherit** parent PreToolUse (host source + unit tests). | Timeout / crash / missing binary / malformed JSON → **tool may still run**. Never market as hard sandbox. |
| **6. Acceptance allowlist** | `omg_cli.command_policy` + `omg accept` | **CLI gate (operator intent)** | Only frozen argv families run for `verified`: `true`/`false`/`pytest`/`python -m pytest\|unittest` / project `.py`; deny `python -c`, shells, `npx`, agent CLIs. | Approved runners still execute **repo code**. Not an OS sandbox. |
| **7. Ask broker** | `omg ask` child-only env + fixed providers; stdin prompt by default | **User-invoked path** | External advisors only when human runs CLI; `OMG_ALLOW_EXTERNAL_CLI` not exported to parent shell; prompt body not in argv (`OMG_ASK_STDIN=1`); freeform `--extra` off unless `OMG_ASK_ALLOW_EXTRA=1`. | Provider may ignore stdin; never auto-ingested into pipeline. |
| **8. Prompt / skills HARD RULES** | Skills, agent bodies, CLI-injected reminders | **Convention only** | Documents required `capability_mode`, depth=1, no external workers. | Models can ignore text. |

## Primary product contract

1. **Workers without shell** — spawn implementers with `capability_mode=read-write`; critic/verifier/explore with `read-only`. This is the main answer to interpreter escapes.
2. **Depth = 1** — children must not spawn; `omg-executor` disallows `spawn_subagent` **and** `run_terminal_command` / `run_terminal_cmd`.
3. **Only `omg` CLI** writes `passes` / `verified` under `.omg/state/` after semantic acceptance.
4. **Hooks are defense-in-depth** — fail-open; live canary via `scripts/canary_pretool.py` (PATH shim, never real claude/codex).

## In-session MCP server (`omg mcp-server`)

FOCUSED read + proposal surface (not OMC ~54-tool parity). The MCP process **is**
omg-cli code, so “verified is CLI-only” does not self-enforce — three mechanisms
hold the line:

| # | Mechanism | What it stops |
|---|-----------|---------------|
| 1 | Curated tool **allowlist** | No accept / set_verified / state_write / python_repl / … tools |
| 2 | **Structural refusal** (`OMG_MCP_SERVER=1`) | `set_verified` + `register_cli_acceptance_token` raise in-process |
| 3 | **Path confinement** on every write handler | No write into `.omg/state/**`; refuse `..` / symlink escape |

Kick-a-run tools (if ever added) must spawn a **fresh** `omg` subprocess without
the MCP env marker — never run acceptance/FSM in-process inside the MCP server.

## Acceptance policy (summary)

Acceptance child env (`omg_cli.acceptance.sanitized_env`) strips `OMG_ALLOW_*`
plus common hijack keys (`PYTHONSTARTUP`, `PYTHONPATH`, `GIT_DIR` /
`GIT_WORK_TREE`, `LD_PRELOAD` / `DYLD_*`, `NODE_OPTIONS` / `NODE_PATH`,
`npm_config_*`). PATH / HOME / VIRTUAL_ENV remain so venv runners work.
**Residual:** approved runners still execute repo code; not an OS sandbox.
Operator weaken: `OMG_ACCEPT_KEEP_PYTHONPATH=1` re-adds PYTHONPATH after scrub.

**UltraQA freeze (v0.3.2+):** `omg qa freeze` applies the **same** command
policy as acceptance (fail-closed at freeze). Tips point operators at
`python3 -m pytest` / project `.py` — this does **not** expand the allowlist.
Unquoted pytest marker tokens (`-m not live`) may be coalesced to a single
markexpr for UX; coalescing is not a policy bypass.

**Auto PRD / complete short-circuit (v0.3.2+):** missing `prd.json` may be
materialized from **CLI-stamped clean** UltraQA only (never overwrites an
existing operator PRD). `omg autopilot complete` may short-circuit when the
run is already disk-`verified` (phase sync only) — it does **not** create
`verified` without a prior CLI accept path.

**Goal verify multi-process residual:** `omg goal verify` may accept a disk
CLI acceptance stamp (`require_token=False`) when the linked run is already
disk-`verified`. That is weaker than same-process `set_verified` tokens —
treat goal promotion as multi-process disk-trust, not process-token grade.
See `omg_cli/goals.py` verify path.

See `omg_cli/command_policy.py` (`POLICY_VERSION`).

| Family | Allowed | Denied |
|--------|---------|--------|
| `true` / `false` | yes | — |
| `pytest` | any args | — |
| `python` / `python3` / `python3.N` | `-m pytest`, `-m unittest`, or `.py` under project | `-c`, `-e`, other `-m` modules, `python3evil` |
| `npm` | `test`, `run test`, `run pytest` | other scripts |
| `git` | read-only: `status`/`diff`/`log`/`show`/`rev-parse`/`rev-list`/`describe`/`ls-files`/`ls-tree`/`cat-file`; `branch`/`tag`/`stash` list-only | `clean`/`push`/`reset`/`checkout`/`restore`/`rebase`/`merge`/`pull`/`fetch`/`remote`/`config`/`add`/`commit`/…; mutate flags (`branch -D`, `tag -d`, `stash drop`); `-c` config injection |
| `make` | allowlisted targets only (`test`/`check`/`lint`/`unit`/`units`/`pytest`/`ci`/`verify`) | bare `make`; unknown targets; `-f`/`--file`/`-C`/`--directory`/`--eval` (incl. glued forms) |
| `cargo` | `test`/`check`/`clippy`/`fmt` | `run`/`install`/`publish`/`bench`/`script`/`build`; also `--manifest-path`/`--config`/`--target-dir`/`-C` |
| `go` | `test`/`vet`/`fmt`/`version` | `run`/`generate`/`get`/`install`/`mod`; `-exec`/`--exec`/`-toolexec`/`--toolexec` |
| `dart` | `test`/`analyze`/`format` | `run`/`compile`/`pub` |
| `flutter` | `test`/`analyze` | `run`/`pub`/other |
| `npx` / shells / `claude` / `codex` / `rm` / `sudo` | — | **always** |
| `--allow-cmd NAME` | extends basename set | floors still apply |
| `--no-allowlist` | TTY-only break-glass | floors still apply; non-TTY refused |

Beyond basename allowlisting, acceptance applies **argv grammar** per family (`POLICY_VERSION` ≥ 2): git is inspection-only (no bare `stash`, no branch/tag create), make requires an allowlisted target with no makefile/dir overrides, and cargo/go/dart/flutter admit only test/analysis-style subcommands so a frozen runner cannot become an install, publish, or long-running process launcher.

**Canary pass criteria** (`scripts/canary_pretool.py --live` / `omg_cli/canary_classify.py`):

| Status | Exit | Meaning |
|--------|------|---------|
| `DENIED_PARENT_AND_CHILD` | 0 | Parent **and** child show host signature `oh-my-grok: external agent CLI blocked` |
| `DENIED_PARENT_HOST_CHILD_CAPABILITY` | 0 | Parent host signature **and** child has **no shell tool** (capability isolation) + no marker |
| `DENIED_CLAIMED_NO_HOOK_ORACLE` | 2 | Model “denied” prose only — **not** suite green |
| `REAL_CLI_RAN_*` / marker present | 1 | Soft-gate failed |

Free-form model theater without host or capability evidence must not green the suite.

### Spawn soft fail-closed (Option A, shipped)

PreToolUse matcher includes `spawn_subagent|Task`. When the hook runs, `omg_cli.deny.decide_spawn_subagent` **denies** spawns that:

- omit `capability_mode` / `capabilityMode`, or
- set `execute` / `all`, or
- mismatch the role table (`general-purpose` / `omg-executor` → `read-write`; `explore` / critic / verifier → `read-only`).

This is still a **soft-gate** (host fail-open on hook crash/timeout). Primary isolation remains host `capability_mode` when correctly set. Escape hatch: process env `OMG_ALLOW_UNSAFE_SPAWN=1` only.

**Deny UX (2026-07-20):** missing/wrong mode must **not** cause the leader to abandon multi-agent work. Deny `reason` strings include `RETRY IMMEDIATELY` plus the suggested `capability_mode` so the model re-spawns in the same turn instead of falling back to solo-only. Skills/AGENTS/orchestrator also hard-code that retry protocol.

`--yes` skips confirmation UX only — **never** policy.

## Canary

```bash
python3 scripts/canary_pretool.py --dry
# optional live (skips if no grok):
python3 scripts/canary_pretool.py --live
```

Procedure + host source evidence: [`docs/research/subagent-pretooluse-spike.md`](research/subagent-pretooluse-spike.md).

### Global PreToolUse install (required for soft-gate effectiveness)

Live 2026-07-19 showed plugin-bundled `hooks/hooks.json` may not appear in
session `hook_execution` runs. Soft-gate effectiveness requires:

1. `scripts/install-plugin.sh` (writes `~/.grok/hooks/omg-pretool-deny.json`)
2. `omg doctor` hard check `global PreToolUse soft-gate` (fail if missing)

This remains **fail-open** on hook timeout/crash. Primary isolation is still
`capability_mode` without Execute on implementers.

## Host launcher: `omg --madmax` (break-glass)

**Operator-triggered** interactive Grok with full-open host permissions:

- Injects `--always-approve` + `--permission-mode bypassPermissions` (exactly once).
- Interactive + outside `$TMUX`: **requires tmux** — creates a **new** session each launch (`omg-<dir>-<digest>-<timestamp>`), then attaches. Missing tmux → exit 1 (no silent direct demotion).
- Inside tmux / headless (`-p`, `--single`, …): runs `grok` in-process (no nested session).
- Does **not** write `.omg/state`, does **not** touch `verified` / acceptance / ask deny lists.
- Root `--yolo` remains **mode-subcommand elevation only** — not a madmax alias.
- Detached full-open sessions keep running under tmux until you `tmux kill-session -t omg-…`.
- **Env forward:** madmax passes allowlisted `GROK_*` / `XAI_*` / a few shell vars into the session via `tmux new-session -e KEY=value` (not embedded in the pane start-command string). Values may still appear in the **tmux server process** environment for the session lifetime — prefer host identity / profile secrets over one-off env dumps on multi-user machines.

This is intentional break-glass, not a sandbox. Document and name-prefix (`omg-`) are the mitigations — not PreToolUse.

## Experimental team plane: `omg team` (D1 zero-config + D3 multi-CLI + D2 staged driver + D4 scale/resume/ralph)

Gated by **`OMG_EXPERIMENTAL_TMUX_TEAM=1`**. Lifecycle: `start` / `run` / `scale` / `resume` / `status` / `collect` / `stop`.

| Claim | Reality |
|-------|---------|
| Zero-config panes | **grok only** (D1 path via madmax `build_pane_command`) when `--routing` is omitted |
| Multi-CLI panes | **Present** behind the same gate when `--routing` maps role→`{provider,model?}` (providers: grok / codex / agy / cursor / gemini) |
| Isolation | **Integration** isolation only: ownership manifest + per-task git worktrees + `seal` + `integrate` — **not** an execution sandbox. D4 scale/resume/ralph add **no** new isolation claims. |
| Kill path | `stop` / scale-down kill **only** the recorded tmux session/window names + recorded `pgid`s — **no** self-matching `pkill -f` |
| `verified` | **Never** set by `collect` / `stop` / **`run`** / **`scale`** / **`resume`** / ralph loop; remains behind `omg accept` |
| Nested | Refuses start / run / scale / resume inside a spawned-worker context (`OMG_TEAM_WORKER` / related markers) |
| Routing floors | Reviewer/verifier → structured-verdict providers only (`grok`/`codex`/`claude`/`gemini`; **cursor forbidden**); unknown roles fail closed; posture derived from role (never free-form) |
| `omg team run` | **Staged DRIVER** only (`team-plan→team-prd→team-exec→team-verify→team-fix`). Does **not** reimplement ralplan/dual_review/planner/verifier — sequences the team plane + gates durable `stages/team-verifier.*` via POST-A2 `parse_verdict_file`. Decomposition is the leader’s / ralplan’s job (`--tasks-json` / `--tasks-path`). No autopilot parity beyond “sequences them.” |
| `omg team scale` | Dynamic `--add N` / `--remove N` under a run-dir **scale lock**; bounded by `max_workers_cap()`; monotonic window indices; scale-down preserves worktrees and never goes below 1 active pane |
| `omg team resume` | Idempotent liveness reconciliation into `team.json` after leader restart; fail-closed if not a team run |
| `omg team run --ralph` | Bounded outer max_iter loop (ralph discipline) around the same staged driver; `linked_ralph` ↔ `linked_team`; complete only via real team-verify APPROVE — **not** a second isolation boundary |

### Per-provider posture enforcement (NOT uniform)

Posture is **derived from role** (`omg_cli/team/roles.py` → `role_posture`) and applied by
`build_executor_argv` (`omg_cli/team/providers.py`). Enforcement strength **differs by provider**:

| Provider | read-only enforcement |
|----------|------------------------|
| **grok** | CLI-enforced (`--permission-mode plan` vs `bypassPermissions`) |
| **codex** | CLI-enforced (`-s read-only` vs `workspace-write`) |
| **agy** | `--sandbox` **best-effort** only (`--dangerously-skip-permissions` is present in **both** postures for headless autonomy) — OMG does **not** enforce agy's sandbox; cite agy's real `--sandbox` semantics, not a hard jail |
| **cursor** | `--mode ask` (read-only) vs default agent mode (read-write); **forbidden from reviewer/verifier roles** (no structured-verdict mode) |
| **gemini** | **NONE** — read-only and read-write argv are identical; a gemini pane (including a gemini reviewer) is contained **only** by the integration boundary, **not** CLI-sandboxed |

This is exactly why the contract is **“integration isolation, NOT execution isolation.”** A shell-capable executor pane runs with operator-level machine access; only worktree ownership + seal + integrate bound what reaches the leader tree, and `verified` stays CLI-only (`omg accept`).

Do **not** claim uniform sandboxing across providers, OMC multi-CLI team parity, or that multi-CLI panes are an execution sandbox.

## Do not claim

- “Workers cannot run external CLIs because PreToolUse blocks them” **without** stating fail-open residual and capability_mode primary.
- “Acceptance allowlist is a sandbox.”
- “`--permission-mode plan` is a hard read-only lock for all sessions.”
- “Live canary pass proves hard isolation forever” (re-run after Grok upgrades).
- “`omg --madmax` is sandboxed” or “madmax is a mode FSM / sets verified.”
- “`omg team` multi-CLI panes are an execution sandbox / uniform CLI sandbox across providers.” (Integration isolation only; see posture table.)
- “`omg team run` is a full planner/verifier / autopilot-parity mode.” (It is a thin staged driver over existing lanes.)
- “`omg team scale` / `resume` / `--ralph` add an execution sandbox or new isolation boundary.” (Lifecycle only; same integration-isolation-not-execution-sandbox contract.)
- “agy `--sandbox` is a hard read-only jail enforced by OMG.”
- “gemini reviewer panes are CLI-sandboxed.”

## Related

- Isolation research: `.omg/research/council-v021/` (local) / `docs/research/council-v021-synthesis.md`
- Install: `scripts/install-plugin.sh`
- Smoke: `scripts/smoke.sh`
