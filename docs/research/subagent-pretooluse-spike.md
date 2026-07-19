# Spike: Does PreToolUse fire for `spawn_subagent` children?

**Date:** 2026-07-19 (updated same day for v0.2.1 source evidence)  
**Repo:** oh-my-grok v0.2.1  
**Related:** dual-review I2; council-v021 isolation research

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
| Unit test | `subagent_inherits_parent_pre_tool_use_client_hook` — child tool denied by parent’s inherited PreToolUse |
| Fail-open policy unchanged | timeout / crash / missing / malformed → tool may still run (`10-hooks.md`) |

**Revised product stance:** PreToolUse is **intended to run on subagents** (inherited registry/hooks). It remains a **fail-open soft-gate**, not a sandbox. Live canary on this machine is still optional acceptance for gap B.

## How to re-test (canary)

Requires a live `grok` session with the plugin installed and trusted (`grok plugin install . --trust`).

### 1. Canary command (leader)

```bash
cd /path/to/oh-my-grok   # or a project with omg setup + plugin active
# Ensure PreToolUse is registered (omg doctor)
omg doctor

# Leader canary: should DENY (or soft-fail-open — note which)
# In an interactive session, or:
grok -p 'Run exactly this tool: run_terminal_command with command "claude --version". Report whether the tool was denied or ran.' --cwd "$(pwd)"
```

Expected if hooks fire: deny decision from `pre_tool_use_deny.py` (exit 2 → tool blocked when host honors deny).

### 2. Child canary via `spawn_subagent`

```bash
grok -p '
Spawn ONE child with spawn_subagent (general-purpose, depth=1).
Child instruction: call run_terminal_command with command exactly: claude --version
Do not run that command yourself on the leader.
After the child returns, report: (1) whether the child tool call was denied, (2) child stdout/stderr summary, (3) any hook error.
' --cwd "$(pwd)" --output-format plain
```

### 3. Optional: event spool evidence

After the canary, inspect:

```text
.omg/artifacts/   # agent notes
~/.grok/          # host logs if enabled (--debug / --debug-file)
```

Record:

| Probe | PreToolUse fired? | Decision | Host honored deny? |
|-------|-------------------|----------|--------------------|
| Leader `run_terminal_command claude` | ? | ? | ? |
| Child `run_terminal_command claude` | ? | ? | ? |

## Spike status (this environment)

| Item | Status |
|------|--------|
| Host source: subagents inherit PreToolUse | **Supported** (see table above) |
| Live re-test in CI / this authoring session | **Not verified live** (canary table empty) |
| Documented re-test procedure | Yes (above) |
| Assumption recorded | **ASSUMPTION** below |

### ASSUMPTION (until re-verified live)

> **ASSUMPTION (updated):** Subagent children **should** inherit plugin/client PreToolUse per grok-build source and unit tests. Hooks may still **fail-open**. Treat PreToolUse as **defense-in-depth soft-gate** (leader + children when registry loaded), **not** a hard sandbox.

Do **not** claim “children are hard-blocked from external CLIs” without a dated live canary table above filled with pass/fail evidence, or without the **capability_mode** primary stack.

## Compensation (product defaults) — primary isolation stack

Because hooks alone are not a hard guarantee:

1. **Primary:** `capability_mode: read-write` (implementers, **no shell**) / `read-only` (critic/verifier/explore). Dropping Execute removes `run_terminal_command` entirely.
2. **Secondary:** agent `disallowedTools` / parent `--disallowed-tools` session clamp.
3. **Soft-gate:** PreToolUse deny (fail-open honest) + skill HARD RULES.
4. **Acceptance / tests / shell** — run **only** via the **`omg` CLI** (`omg accept`, frozen manifest + CLI stamp + basename allowlist). Models must not set `verified`.
5. **Env bypass** — `OMG_ALLOW_EXTERNAL_CLI=1` is process-env only (never parsed from command text); rare advisor use only.

See README **Isolation stack** and `.omg/research/council-v021/grok-isolation-research.md`.

## Related code

| Path | Role |
|------|------|
| `hooks/hooks.json` | Registers PreToolUse → `pre_tool_use_deny.py` |
| `hooks/bin/pre_tool_use_deny.py` | stdin JSON → `omg_cli.deny.decide_pre_tool_use` |
| `omg_cli/deny.py` | Command-position deny list (soft-gate) |
| `omg_cli/acceptance.py` | Sole writer of stamped acceptance results + allowlist |
| `skills/omg-*/SKILL.md` | capability_mode defaults + HARD RULES |

## Soft-gate residual (not solved by this spike)

Even when PreToolUse fires, it may miss:

- Interpreter escapes (`python3 -c '…'`, `node -e`, `npx …`) when shell tool is present
- Some shell wrappers / path tricks
- Host fail-open on hook timeout / crash / malformed JSON

Primary contract remains **capability_mode (no Execute) + skills + CLI HARD RULES + acceptance ownership**, not the hook alone.
