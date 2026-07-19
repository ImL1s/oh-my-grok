# Spike: Does PreToolUse fire for `spawn_subagent` children?

**Date:** 2026-07-19 (updated for v0.2.3 canary script + command policy)  
**Repo:** oh-my-grok  
**Related:** dual-review I2; council-v021 isolation research; [`docs/security-model.md`](../security-model.md)

## Question

Does Grok Build invoke the plugin `PreToolUse` hook when a **child** agent (spawned via `spawn_subagent`) runs `run_terminal_command`? Or only for the leader session?

If children do **not** inherit PreToolUse, the deny list in `hooks/bin/pre_tool_use_deny.py` / `omg_cli/deny.py` is **leader-only defense-in-depth** and cannot stop a child from shelling out to `claude` / `codex` / etc.

## Source evidence (grok-build) — inheritance designed in

Council research (`.omg/research/council-v021/grok-isolation-research.md`, Grok **0.2.103**) found **host source + unit tests** that subagents **inherit** parent PreToolUse:

| Evidence | Path / note |
|----------|-------------|
| File hooks always `dispatch_pre_tool_use` when active | `xai-grok-shell/.../tool_calls.rs` (~918–964); payload includes `subagent_type` |
| Client hooks snapshot for inheritance | `SessionCommand::SnapshotClientHooks` — “so a subagent inherits the same PreToolUse gate” |
| Spawn clones hooks into child | `handle_request.rs` ~1157 / ~1176: `client_hooks.clone()` + `hook_registry.clone()` |
| Subagent module | `xai-grok-shell/.../subagent/mod.rs` ~147–151, ~294: parent hooks / `hook_registry` passed into child |
| Unit test | `subagent_inherits_parent_pre_tool_use_client_hook` — child tool denied by parent’s inherited PreToolUse |
| Fail-open policy unchanged | timeout / crash / missing / malformed → tool may still run (`10-hooks.md`; `xai-grok-hooks` dispatcher) |

**Revised product stance:** PreToolUse is **intended to run on subagents** (inherited registry/hooks). It remains a **fail-open soft-gate**, not a sandbox. Primary isolation is **`capability_mode`** (see security-model).

## Canary script (PATH shim — never real claude)

**Code:** [`scripts/canary_pretool.py`](../../scripts/canary_pretool.py)

Design:

1. Create a temp dir and a **PATH shim** named `claude` that **only appends a marker file** if executed, then exits 99.
2. **Never** invoke a real `claude` / `codex` binary.
3. **`--dry` (default):** write plan JSON with parent/child `grok -p` argv + shim paths; no process launch of grok required.
4. **`--live` (optional):** if `grok` is on PATH, run the parent canary with `PATH=<shim-bin>:$PATH`. If PreToolUse + host deny works, the shim never runs → **marker absent**. If fail-open or deny missed, marker appears → exit 1.

```bash
# Dry: argv + shim layout only
python3 scripts/canary_pretool.py --dry
python3 scripts/canary_pretool.py --dry -o /tmp/canary-plan.json

# Live: optional; skips cleanly when grok missing
python3 scripts/canary_pretool.py --live
# evidence default: docs/research/canary-pretool-latest.json
```

### Manual live prompts (if not using the script’s argv)

Requires plugin installed/trusted (`scripts/install-plugin.sh` or `grok plugin install . --trust`).

**Leader:**

```bash
export PATH="/path/to/shim-bin:$PATH"   # from dry plan path_prefix
grok -p 'Run exactly this tool: run_terminal_command with command "claude --version". Report whether the tool was denied or ran.' --cwd "$(pwd)"
```

**Child via `spawn_subagent`:**

```bash
grok -p '
Spawn ONE child with spawn_subagent (general-purpose, depth=1).
Child instruction: call run_terminal_command with command exactly: claude --version
Do not run that command yourself on the leader.
After the child returns, report: (1) whether the child tool call was denied, (2) child stdout/stderr summary, (3) any hook error.
' --cwd "$(pwd)" --output-format plain
```

### Live results table (fill when run)

| Probe | Date | Grok ver | PreToolUse fired? | Decision | Host honored deny? | Marker present? |
|-------|------|----------|-------------------|----------|--------------------|-----------------|
| Leader `run_terminal_command claude` | — | — | ? | ? | ? | ? |
| Child `run_terminal_command claude` | — | — | ? | ? | ? | ? |

## Spike status (this environment)

| Item | Status |
|------|--------|
| Host source: subagents inherit PreToolUse | **Supported** (see table above) |
| Dry canary script | **`scripts/canary_pretool.py --dry`** |
| Live re-test in CI / this authoring session | **Not verified live** (table empty; `--live` skips without grok) |
| Documented re-test procedure | Yes |
| Assumption recorded | **ASSUMPTION** below |

### ASSUMPTION (until re-verified live)

> **ASSUMPTION (updated):** Subagent children **should** inherit plugin/client PreToolUse per grok-build source and unit tests. Hooks may still **fail-open**. Treat PreToolUse as **defense-in-depth soft-gate** (leader + children when registry loaded), **not** a hard sandbox.

Do **not** claim “children are hard-blocked from external CLIs” without a dated live canary table above filled with pass/fail evidence, or without the **capability_mode** primary stack.

## Compensation (product defaults) — primary isolation stack

Because hooks alone are not a hard guarantee:

1. **Primary:** `capability_mode: read-write` (implementers, **no shell**) / `read-only` (critic/verifier/explore). Dropping Execute removes `run_terminal_command` entirely.
2. **Secondary:** agent `disallowedTools` (executor: `spawn_subagent` + `run_terminal_command` + `run_terminal_cmd`) / parent `--disallowed-tools` session clamp.
3. **Soft-gate:** PreToolUse deny (fail-open honest) + skill HARD RULES.
4. **Acceptance / tests / shell** — run **only** via the **`omg` CLI** (`omg accept`, frozen manifest + CLI stamp + **semantic command policy** in `omg_cli/command_policy.py`). Models must not set `verified`.
5. **Env bypass** — `OMG_ALLOW_EXTERNAL_CLI=1` is process-env only (never parsed from command text); rare advisor use only (`omg ask`).

See [`docs/security-model.md`](../security-model.md) and README **Isolation stack**.

## Related code

| Path | Role |
|------|------|
| `scripts/canary_pretool.py` | PATH shim canary (dry/live) |
| `hooks/hooks.json` | Registers PreToolUse → `pre_tool_use_deny.py` |
| `hooks/bin/pre_tool_use_deny.py` | stdin JSON → `omg_cli.deny.decide_pre_tool_use` |
| `omg_cli/deny.py` | Command-position deny list (soft-gate) |
| `omg_cli/command_policy.py` | Semantic acceptance policy |
| `omg_cli/acceptance.py` | Sole writer of stamped acceptance results |
| `agents/omg-executor.md` | `disallowedTools`: shell + spawn |
| `skills/omg-*/SKILL.md` | capability_mode **MUST** defaults + HARD RULES |

## Soft-gate residual (not solved by this spike)

Even when PreToolUse fires, it may miss:

- Interpreter escapes (`python3 -c '…'`, `node -e`, `npx …`) when shell tool is present (workers should not have shell)
- Some shell wrappers / path tricks
- Host fail-open on hook timeout / crash / malformed JSON

Primary contract remains **capability_mode (no Execute) + skills + CLI HARD RULES + acceptance ownership**, not the hook alone.
