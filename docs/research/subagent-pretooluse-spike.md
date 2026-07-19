# Spike: Does PreToolUse fire for `spawn_subagent` children?

**Date:** 2026-07-19  
**Repo:** oh-my-grok v0.2  
**Related:** dual-review I2 / Codex Important (subagent soft-gate coverage)

## Question

Does Grok Build invoke the plugin `PreToolUse` hook when a **child** agent (spawned via `spawn_subagent`) runs `run_terminal_command`? Or only for the leader session?

If children do **not** inherit PreToolUse, the deny list in `hooks/bin/pre_tool_use_deny.py` / `omg_cli/deny.py` is **leader-only defense-in-depth** and cannot stop a child from shelling out to `claude` / `codex` / etc.

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
| Live re-test in CI / this authoring session | **Not verified live** |
| Documented re-test procedure | Yes (above) |
| Assumption recorded | **ASSUMPTION** below |

### ASSUMPTION (until re-verified live)

> **ASSUMPTION:** Subagent children may **not** reliably inherit plugin `PreToolUse` the same way as the leader, **or** hooks may fail-open. Treat PreToolUse as **defense-in-depth soft-gate on the leader only**, not a sandbox for children.

Do **not** claim “children are hard-blocked from external CLIs” without a dated live canary table above filled with pass/fail evidence.

## Compensation (product defaults)

Because the spike is not a hard guarantee:

1. **Write workers** — prefer `capability_mode: read-write` (edit tools, **no shell**) when the host supports capability modes. Avoid giving implementers unrestricted `run_terminal_command` by default.
2. **Critic / verifier / explore** — prefer `capability_mode: read-only` (or permissionMode `plan` / read-only allow lists).
3. **Acceptance / tests / shell** — run **only** via the **`omg` CLI** (`omg accept`, frozen `acceptance.manifest.json` + CLI-stamped `acceptance.result.json`). Models must not set `verified`.
4. **Skills** — `omg-ultrawork`, `omg-ralph`, `omg-ralplan` document these capability defaults; HARD RULES still forbid external agent CLIs as workers regardless of hooks.
5. **Env bypass** — `OMG_ALLOW_EXTERNAL_CLI=1` is process-env only (never parsed from command text); intended for rare advisor use, not default workers.

## Related code

| Path | Role |
|------|------|
| `hooks/hooks.json` | Registers PreToolUse → `pre_tool_use_deny.py` |
| `hooks/bin/pre_tool_use_deny.py` | stdin JSON → `omg_cli.deny.decide_pre_tool_use` |
| `omg_cli/deny.py` | Command-position deny list (soft-gate) |
| `omg_cli/acceptance.py` | Sole writer of stamped acceptance results |
| `skills/omg-*/SKILL.md` | capability_mode defaults + HARD RULES |

## Soft-gate residual (not solved by this spike)

Even when PreToolUse fires, it may miss:

- Interpreter escapes (`python3 -c '…'`, `node -e`, `npx …`)
- Some shell wrappers / path tricks
- Host fail-open on hook timeout / crash / malformed JSON

Primary contract remains **skills + CLI HARD RULES + acceptance ownership**, not the hook alone.
