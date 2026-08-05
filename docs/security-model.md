# oh-my-grok security model

English | [简体中文](./security-model.zh.md) | [繁體中文](./security-model.zh-TW.md)

**Canonical truth table** for isolation claims. README, skills, and doctor footers should link here rather than invent stronger wording.

Last updated: 2026-08-01 · Plugin version: **0.7.4**

## Layer table (strongest → weakest)

| Layer | Mechanism | Hardness | What it stops | Residual / failure mode |
|-------|-----------|----------|---------------|-------------------------|
| **1. capability_mode** | Host tool-kind filter on `spawn_subagent` | **Hard-ish (host)** | Implementer with `read-write`: **no Execute** → no `run_terminal_command` → no `python -c` / `npx` / agent CLI from that worker. Critic/verifier `read-only`: no write + no Execute. | Omitted mode falls back to agent defaults (`general-purpose` ≈ full). `read-write` still includes Task/spawn — depth=1 needs `disallowedTools` / parent policy. |
| **2. Agent / headless tool filter** | `disallowedTools` frontmatter; parent `--disallowed-tools` | **Hard when honored** | Extra deny of shell/spawn on executor; RO stages inject shell deny in dual-review / ralplan. | Wrong tool id, TUI ignoring headless flags, or leader still has shell. |
| **3. OS sandbox** | Grok `--sandbox` / custom deny paths | **Kernel-ish when enabled** | Path denies (e.g. `.omg/state/**`) for the Grok process. | Default off; macOS child network restrictions limited; outer `omg` CLI is outside child sandbox. |
| **4. Permission rules** | `--allow` / `--deny` rules | **Gate, not removal** | Can refuse invocations that still appear in the toolset. | Wrappers/interpreters residual; not a general allowlist engine. |
| **5. PreToolUse hooks** | global: self-contained `omg_pretool_deny_standalone.py` under `$GROK_HOME/hooks` (from `omg_cli.deny`); logic = `omg_cli.deny` | **Soft (fail-open)** | Command-position deny of `claude`/`codex`/… when hook healthy and host honors deny (deny via stdout JSON, always exit 0, `-I -S \|\| true` launcher). Subagents **inherit** parent PreToolUse (host source + unit tests). | Timeout / crash / missing binary / malformed JSON → **tool may still run**. Never market as hard sandbox. |
| **6. Acceptance allowlist** | `omg_cli.command_policy` + `omg accept` | **CLI gate (operator intent)** | Only frozen argv families run for `verified`: `true`/`false`/`pytest`/`python -m pytest\|unittest` / project `.py`; deny `python -c`, shells, `npx`, agent CLIs. | Approved runners still execute **repo code**. Not an OS sandbox. |
| **7. Ask broker** | `omg ask` child-only env + fixed providers; stdin prompt by default | **User-invoked path** | External advisors only when human runs CLI; `OMG_ALLOW_EXTERNAL_CLI` not exported to parent shell; prompt body not in argv (`OMG_ASK_STDIN=1`); freeform `--extra` off unless `OMG_ASK_ALLOW_EXTRA=1`. | Provider may ignore stdin; never auto-ingested into pipeline. |
| **8. Prompt / skills HARD RULES** | Skills, agent bodies, CLI-injected reminders | **Convention only** | Documents required `capability_mode`, depth=1, no external workers. | Models can ignore text. |

## Primary product contract

1. **Workers without shell** — spawn implementers with `capability_mode=read-write`; critic/verifier/explore with `read-only`. This is the main answer to interpreter escapes.
2. **Depth = 1** — children must not spawn; `omg-executor` disallows `spawn_subagent` **and** `run_terminal_command` / `run_terminal_cmd`.
3. **Only `omg` CLI** writes `passes` / `verified` under `.omg/state/` after semantic acceptance.
4. **Hooks are defense-in-depth** — fail-open; live canary via `scripts/canary_pretool.py` (PATH shim, never real claude/codex).

## External-agent CLI classification

The PreToolUse hook classifies **active executable contexts**, not every textual
mention of a protected CLI name. The v0.7.2 contract is:

| Command intent | Expected decision | Why |
|----------------|-------------------|-----|
| Direct execution such as `claude --version` | **Deny** | Even a diagnostic flag executes the PATH-resolved provider and may load configuration, plugins, telemetry, or updater code. Use `omg ask` for an advisor. |
| Discovery such as `which claude`, `command -v claude`, or `type -a claude` | **Allow** | The provider name is data to a discovery command, not the executable head. |
| Passive inspection such as `strings`, `file`, `stat`, `readlink`, or a hash tool with a provider path operand | **Allow** | A path component or ordinary argument containing `claude`/`codex`/… is not external-agent execution. |
| Quoted literals, comments, inert heredoc data, or commit text such as `fix(kimi)` | **Allow** | Inert text is not executed. |
| Command/process substitution or another recognized active shell body that directly executes a protected CLI | **Deny** | The protected CLI is in an executable context. |

`OMG_ALLOW_EXTERNAL_CLI=1` is a **child-process capability** used by the
fixed-argv `omg ask` broker. Do not export it in a shell profile, project
`.env`, parent session, or persistent flag file. The hook never treats an
assignment written inside command text as authorization.

This parser is deliberately a bounded, name-based soft guard, not a complete
shell interpreter. Wrapper options, dynamic executable heads, platform-specific
utility grammar, aliases, renamed binaries, and arbitrary interpreter bodies
remain known parser limitations. Parse timeout, crash, or ambiguity may fail
open. Therefore:

- keep `capability_mode` / tool removal as the primary isolation boundary;
- treat unexpected passive-inspection denial as possible installed-hook drift
  or a compound command containing a real executable context;
- use `omg doctor` plus the recovery procedure below before weakening policy;
- never “fix” a false positive by adding an ambient bypass.

Detailed audit status and non-sensitive reproduction evidence:
[`research/external-cli-soft-gate-audit-2026-07-28.md`](research/external-cli-soft-gate-audit-2026-07-28.md).

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

The plugin `.mcp.json` is conventional registration only. `configured` and
locally `loadable` do not mean Grok enabled, observed, or verified the server in
the current session. A fresh host observation is required for those claims.

## Repository workflow boundary

`repository-workflow/v1` is product-owned. Definitions are immutable by name +
version; the planner fixes task IDs, actor identities, generations, permission
requests, and dependency waves. The CLI **does not spawn** shell or foreign
agents: Grok's leader/skill performs native `spawn_subagent`, then supplies
task-ID-bound receipts to `omg workflow run`.

Effective permission is the intersection of repository policy, host
capabilities, and launch-receipt permissions. MCP servers and write paths need
separate allowlists. Missing/duplicate/foreign receipts, actor mismatch,
permission denial, or an external effect without a verified receipt blocks
shipment. Independent verifier and skeptic identities are required.

Grok `/create-workflow`, `.grok/workflows/*.rhai`, and the native dashboard are
`optional_unclaimed`. Help text or local files are not stable-schema or fresh
invocation proof. OMG never probes undocumented localhost/private sidecars.

## Managed `.omg` store confinement

Authoritative local stores under `.omg` use the primitives in
`omg_cli/contracts/path_keys.py`. On POSIX hosts those primitives:

- open a pre-existing base directory, then walk/create every **managed**
  component (from the `.omg` marker downward, or the missing suffix of a
  non-marker path) with descriptor-relative `dir_fd` operations and
  `O_NOFOLLOW`;
- reject managed-component symlinks for directories, destinations, temporary
  files, journals, and lock files — even when the symlink target remains inside
  the project;
- publish with exclusive create + descriptor-relative rename/link, and enforce
  modes via `fchmod` on the opened descriptor;
- fail closed with `ContractPathError` when equivalent no-follow/`dir_fd`
  support is unavailable (no weaker path-based fallback).

System path prefixes above the managed region (for example macOS `/tmp` →
`/private/tmp`) may still contain platform symlinks; confinement is claimed for
managed components, not for the entire absolute path from `/`. Issue #16 closed
after Linux unit/adversarial gates **and** the #25 `macos-platform` CI lane
(path_keys / lock / symlink contracts). Early plans deferred that close until
the harness existed; both #16 and #25 are now closed on GitHub.

## Recovery, memory, tracking, compaction, notifications

- Recovery opens only a regular non-symlink source, copies a bounded suffix,
  re-checks file identity, writes immutable evidence, redacts context, and keeps
  broken-chain/unknown-record warnings. It is intentionally partial recovery.
- Project memory redacts values and preserves user facts over scanner/import
  data. Tracker projections and compaction checkpoints are generation-fenced.
- Notification adapters are outbound-only, bounded, SSRF-checked where
  applicable, and explicitly non-authoritative. They cannot set `passes`,
  `verified`, workflow terminal state, or release state.
- `.lsp.json` is host-owned registration. OMG validates config and local command
  presence only; it does not proxy semantic LSP operations or infer health.

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
`verified` without a prior CLI accept path. For **autopilot** runs (Round 12),
bare `omg accept` is refused; terminal verify goes through
`omg autopilot complete`, and `set_verified` requires sidecar
`phase==acceptance` (fail-closed).

**Goal verify multi-process residual:** `omg goal verify` may accept a disk
CLI acceptance stamp (`require_token=False`) when the linked run is already
disk-`verified`. That is weaker than same-process `set_verified` tokens —
treat goal promotion as multi-process disk-trust, not process-token grade.
See `omg_cli/goals.py` verify path.

See `omg_cli/command_policy.py` (`POLICY_VERSION`).

### Autopilot `break_glass` vs CLI stamps

Autopilot phase gates treat **on-disk CLI stamps** (`writer=omg-cli`, matching
`run_id`, under `.omg/state/runs/<run_id>/stages/`) as the preferred proof of
progress. Caller-supplied `--evidence-json` booleans or inline receipt objects
are **not** equivalent: they are trivially forgeable in the same turn and do not
prove the CLI writers ran.

When an operator intentionally bypasses a missing stamp (dry-run, recovery,
local debugging), they must pass `break_glass=true` on the transition evidence.
The autopilot FSM records `gate_audit` on history (for example
`break_glass:consensus`) so later forensics can distinguish stamp-backed advances
from audited overrides. Break-glass does **not** weaken acceptance: `verified`
still requires same-process `omg autopilot complete` (autopilot) or `omg accept`
(non-autopilot) with command-policy checks — it only documents operator intent
at earlier gates. Autopilot implementation receipts additionally require a
lease `invocation_id` on the CLI stamp (Round 12) so a hand-written
`writer=omg-cli` file cannot forge the implement→review gate.

Review/QA **fingerprint rechecks** (Round 1) re-validate hash fields on existing
CLI stamps against the current workspace; they do not grant trust to un-stamped
inline JSON. Residual: single-file atomic writes and hash rechecks are not a full
cross-file WAL or cryptographic writer identity (planned hardening). Concurrent
`run_autopilot` invocations are serialized by an exclusive
`autopilot.driver.lock` flock (Round 12) — integration isolation only, not an
execution sandbox. Grok spawn linearization (Round 13) holds cancel-check +
`Popen` + pid publish under a short `transition_guard` and kills the child if
pid publish fails, so cancel cannot race past an unpublished leader pid.

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
session `hook_execution` runs. Soft-gate effectiveness requires a global hook
under `$GROK_HOME/hooks/`, installed by BOTH end-user and dev paths:

1. `omg setup` (and `omg install-hook`) — the end-user path — installs it.
2. `scripts/install-plugin.sh` — the dev path — calls the same installer.
3. `omg doctor` hard check `global PreToolUse soft-gate` + soft freshness check.

**The hook must be SELF-CONTAINED and live under `$GROK_HOME`, never a checkout
path (2026-07-22 fix).** Root cause of the prior design's failure: the global
hook pointed `python3 "<checkout>/hooks/bin/pre_tool_use_deny.py"`, a script under
macOS-TCC-protected `~/Documents` that also `import`ed `omg_cli`. A grok session
in another workspace (or without Documents access) could not `open()` it, so
`python3` exited **2** — and grok's hook contract reads a PreToolUse exit code of
2 as an *explicit deny*. Every tool call (even `ls`) was blocked. The in-code
fail-open never ran because python could not even open the file.

The self-contained standalone (`hooks/bin/omg_pretool_deny_standalone.py`,
generated from `omg_cli/deny.py` + `_common.hook_disabled` by
`scripts/generate_standalone_hook.py`, drift-guarded by `--check` in CI) closes
this with a layered fail-**open** ladder:

1. **Wire contract** — grok honors a stdout `{"decision":"deny"}` *regardless of
   exit code*, and treats any non-`{0,2}` exit as fail-open. So the standalone
   signals deny ONLY via stdout JSON and **always exits 0** — a nonzero exit
   (especially 2) can never come from us.
2. **Launcher** — installed as `python3 -I -S "<abs>" || true`. `-I -S` isolates
   the interpreter (no `PYTHONPATH` / user-site / sibling-module injection);
   `|| true` normalizes any interpreter/startup failure (e.g. rc 2 "can't open
   file") to rc 0 → fail-open.
3. **In-code** — whole-body `try/except` defaults to allow on any error.
4. **doctor** — realpath-under-`$GROK_HOME` + real `open()` + a behavioral
   subprocess smoke (allow/deny) + installed-vs-committed hash (WARN on stale).
   `os.access` is *not* trusted (it checks permission bits, not TCC).

Migration: an existing checkout-path json is auto-repaired on `omg setup` /
`install-hook`; if it cannot be replaced it is **quarantined** to a non-`.json`
name (grok discovers `*.json`) so it can no longer deny every tool. This all
remains **fail-open** on hook timeout/crash; primary isolation is still
`capability_mode` without Execute on implementers.

**Out-of-band recovery** (a session already bricked by the OLD hook cannot run
`omg` through its blocked terminal): from any plain shell, run
`python3 -m omg_cli.hook_install` (repairs it), or as a last resort
`rm "${GROK_HOME:-$HOME/.grok}/hooks/omg-pretool-deny.json"` to disable the
soft-gate, then restart grok. Run `omg doctor` afterward and require the global
hook freshness/hash check to pass. If the v0.7.2 `omg install-hook` subcommand
is misrouted into the Grok launcher, use the module command above; this is
tracked by [issue #18](https://github.com/ImL1s/oh-my-grok/issues/18). Never
create a persistent `ALLOW_EXTERNAL_CLI` flag file:
it is non-canonical and disables the whole external-CLI soft guard.

## Host launcher: bare `omg` / `omg --madmax`

OMX/Sol-aligned root entry (not a mode FSM; never stamps `verified`):

- **Bare / prompt:** launches interactive Grok at safe defaults (no authority inject).
- **`--madmax`:** injects `--always-approve` + `--permission-mode bypassPermissions` (exactly once). Rejects incompatible `--safe` / permission modes in the pre-`--` head (`SAFE-01`).
- **Transport policy:** `OMG_LAUNCH_POLICY` / `--direct` / `--tmux` (last CLI flag wins; values `auto|direct|tmux|detached-tmux`). Auto + TTY + tmux available → detached owned session then attach; auto without tmux warns once and falls back direct; explicit `--tmux` fails closed (`E_LAUNCH_TMUX_UNAVAILABLE` / `E_LAUNCH_TTY_REQUIRED`) **before** headless/print shortcuts. Inside `$TMUX` → direct in-process. Under **auto** (not explicit `--tmux`), headless (`-p`, `--single`, …) stays direct to preserve stdout.
- **`--` boundary:** suffix after the first `--` is opaque and never scanned for wrapper flags.
- Does **not** write `.omg/state`, does **not** touch `verified` / acceptance / ask deny lists.
- Root `--yolo` remains **mode-subcommand elevation only** — not a madmax alias.
- **Env forward:** allowlisted `GROK_*` / `XAI_*` / a few shell vars via `tmux new-session -e KEY=value` when tmux is used. Prefer host identity / profile secrets over one-off env dumps on multi-user machines.

`--madmax` is intentional break-glass, not a sandbox. Document and name-prefix (`omg-`) are the mitigations — not PreToolUse.

## Team plane: `omg team` (default on; D1 zero-config + D3 multi-CLI + D2 staged driver + D4 scale/resume/ralph)

**Default on** (promoted 2026-07-30; `LIVE_TEAM_SMOKE_OK` local). Kill switch **`OMG_DISABLE_TMUX_TEAM=1`** (legacy **`OMG_EXPERIMENTAL_TMUX_TEAM=0`** also disables). Lifecycle: `start` / `run` / `scale` / `resume` / `status` / `collect` / `stop`.

| Claim | Reality |
|-------|---------|
| Zero-config panes | **grok only** (D1 path via madmax `build_pane_command`) when `--routing` is omitted |
| Multi-CLI panes | **Present** behind the same gate when `--routing` maps role→`{provider,model?}` (providers: grok / codex / agy / cursor / gemini) |
| Isolation | **Integration** isolation only: ownership manifest + per-task git worktrees + `seal` + `integrate` — **not** an execution sandbox. D4 scale/resume/ralph add **no** new isolation claims. |
| Kill path | `stop` validates the recorded session identity; scale-down signals only receipt-bound `pgid`s and closes panes through one tmux-side predicate bound to session/launch nonce/window/pane identity — **no** mutable `session:index` destructive target and **no** self-matching `pkill -f` |
| `verified` | **Never** set by `collect` / `stop` / **`run`** / **`scale`** / **`resume`** / ralph loop; remains behind `omg accept` |
| Nested | Refuses start / run / scale / resume inside a spawned-worker context (`OMG_TEAM_WORKER` / related markers) |
| Routing floors | Reviewer/verifier → structured-verdict providers only (`grok`/`codex`/`claude`/`gemini`; **cursor forbidden**); unknown roles fail closed; posture derived from role (never free-form) |
| `omg team run` | **Staged DRIVER** only (`team-plan→team-prd→team-exec→team-verify→team-fix`). Does **not** reimplement ralplan/dual_review/planner/verifier — sequences the team plane + gates durable `stages/team-verifier.*` via POST-A2 `parse_verdict_file`. Decomposition is the leader’s / ralplan’s job (`--tasks-json` / `--tasks-path`). No autopilot parity beyond “sequences them.” |
| `omg team scale` | Dynamic `--add N` / `--remove N` under a run-dir **scale lock**; each new add transaction is bounded by `max_workers_cap()` (an already-published transaction remains recoverable if that mutable cap later drops); monotonic window indices; scale-down preserves worktrees and never goes below 1 active pane. Before live scale-up side effects, an immutable generation-scoped WAL binds the normalized request, base receipt, session identity, and exact per-task launch plan. After WAL-planned new-window or orphan adopt, scale-up sets `@omg_scale_nonce`, renames the window, and **fail-closed ownership readback** requires exact `display-message` identity (`window_id`/`pane_id`/task/nonce) — a rename/option that does not stick aborts bind rather than trusting mutable `session:index`. A pending scale-up WAL **or future identity-receipt generation** under `identity-receipts/*.json` blocks dry-run add, remove, resume/relaunch, collect/join/integrate, and stop until the original add (or exact remove receipt recovery) is recovered. Remove recovery binds victims from the receipt (`tasks_before − tasks_after`), not a re-drained selection; wrong `--remove N` fails closed with generation and receipt victim ids. Meta commits that lose the success path classify as **committed / not_committed / unknown** via identity readback (generation, receipt hash, victim/task identity — not volatile `last_scale.actions` alone); unknown preserves live windows/receipts and demands retry. |
| `omg team resume` | `resume_for_identity` holds one **scale lock** across idempotent liveness reconciliation and dead-worker relaunch. If a relaunch WAL is pending, exact relaunch recovery runs before raw reconciliation; otherwise reconciliation runs first. Concurrent scale/resume cannot double-spawn; non-team runs fail closed. |
| Dead-worker relaunch | Before any tmux respawn side effect, an immutable generation-scoped relaunch WAL binds the base receipt/generation, exact session id + launch nonce, exact target window, and each task + random relaunch nonce/start-command fingerprint. The `$TMUX_PANE` bootstrap verifies the exact session id, launch nonce, and window, writes and reads back the task/relaunch-nonce pane markers, then executes the original command. An exact retry adopts only one unique matching pre-marker or marked pane instead of spawning a duplicate; foreign, duplicate, ambiguous, or drifted identity fails closed. While the WAL is pending, read-only status and exact relaunch recovery remain available; raw resume, scale, stop, collect, join, and integrate are blocked. |
| Relaunch target | Shared/inside split topology requires the exact window recorded in `team.json` and a receipt-authorized live match. A detached, owned split session may derive a missing window id only when the exact receipt-owned session id + launch nonce enumerate one unique window; otherwise relaunch fails closed. |
| `omg team run --ralph` | Bounded outer max_iter loop (ralph discipline) around the same staged driver; `linked_ralph` ↔ `linked_team`; complete only via real team-verify APPROVE — **not** a second isolation boundary |
| Identity receipt chain | Binds session id, launch nonce, generation, and per-task pane/pid/pgid/pid-start; scaled generations also bind immutable window id + a random per-window nonce. V2 scale-up receipts bind the WAL hash, normalized request, exact pane plan, and ownership/argv/prompt artifact hashes; relaunch receipts bind the relaunch WAL hash, request, and exact relaunched records. Future receipt generations (meta uncommitted) gate the same lifecycle paths as pending WAL; recovery is “retry the original live scale/relaunch/remove”, not raw resume for a foreign op. Remain-on-exit dead panes that still match receipt identity are cleaned exactly then may commit as `needs_collect` when the process is absent — presence alone must not wedge the generation. An exact same-session retry adopts a unique pre-marker, marked, renamed, or receipt-bound pane instead of spawning a duplicate; incompatible or ambiguous recovery fails closed. Launch/legacy and relaunch receipts do **not** make a broader execution-sandbox claim. |
| `owner_token` | Shared `uuid4().hex` in `team.json` (`0o600`) + injected into each pane env (`OMG_TEAM_OWNER_TOKEN`). Same-UID processes can read or replace it, and the current API does not authenticate it against authoritative state. It is advisory env-bound attribution, not proof of membership or cross-user isolation. Per-worker tokens are not claimed. |

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
- “A `.mcp.json` / `.lsp.json` file proves the host enabled or verified it.”
- “A local `.rhai` file or `/create-workflow` help text proves native workflow parity.”
- “Notifications or a native dashboard are authoritative for run/release state.”

## Related

- Isolation research: `.omg/research/council-v021/` (local) / `docs/research/council-v021-synthesis.md`
- Install: `scripts/install-plugin.sh`
- Smoke: `scripts/smoke.sh`
