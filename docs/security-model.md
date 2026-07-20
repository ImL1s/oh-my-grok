# oh-my-grok security model

**Canonical truth table** for isolation claims. README, skills, and doctor footers should link here rather than invent stronger wording.

Last updated: 2026-07-21 · Plugin version: **0.3.0**

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

## Acceptance policy (summary)

Acceptance child env (`omg_cli.acceptance.sanitized_env`) strips `OMG_ALLOW_*`
plus common hijack keys (`PYTHONSTARTUP`, `PYTHONPATH`, `GIT_DIR` /
`GIT_WORK_TREE`, `LD_PRELOAD` / `DYLD_*`, `NODE_OPTIONS` / `NODE_PATH`,
`npm_config_*`). PATH / HOME / VIRTUAL_ENV remain so venv runners work.
**Residual:** approved runners still execute repo code; not an OS sandbox.
Operator weaken: `OMG_ACCEPT_KEEP_PYTHONPATH=1` re-adds PYTHONPATH after scrub.

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

## Do not claim

- “Workers cannot run external CLIs because PreToolUse blocks them” **without** stating fail-open residual and capability_mode primary.
- “Acceptance allowlist is a sandbox.”
- “`--permission-mode plan` is a hard read-only lock for all sessions.”
- “Live canary pass proves hard isolation forever” (re-run after Grok upgrades).
- “`omg --madmax` is sandboxed” or “madmax is a mode FSM / sets verified.”

## Related

- Isolation research: `.omg/research/council-v021/` (local) / `docs/research/council-v021-synthesis.md`
- Install: `scripts/install-plugin.sh`
- Smoke: `scripts/smoke.sh`
