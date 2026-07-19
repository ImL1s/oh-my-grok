---
name: omg-cancel
description: Cancel oh-my-grok runs safely via omg cancel and PID files. Use when user says cancel, stop omg, abort run, or kill workers.
---

# omg-cancel — Abort runs safely

Stop supervised oh-my-grok runs without killing the wrong processes. Prefer the CLI; never use self-matching `pkill -f`.

## HARD RULES (non-negotiable)
- Fan-out ONLY via Grok `spawn_subagent` (depth=1; children must NOT spawn).
- NEVER invoke claude/codex/omc team/agy/cursor-agent as default workers.
- Use Grok tool names: read_file, search_replace, run_terminal_command, spawn_subagent, grep, list_dir.
- Write-heavy work: isolation worktree + background true; wait with wait_commands_or_subagents / get_command_or_subagent_output.
- State: only omg CLI is authoritative for passes/verified; you may write proposals under .omg/artifacts/.

## Use when

- User says `cancel`, `stop omg`, `abort`, `kill workers`, `cancelomc`-style abort for oh-my-grok.
- A run is stuck, wrong branch, or user changed intent.

## Canonical cancel

```bash
omg cancel
```

Optional (when supported by CLI):

```bash
omg cancel --run <run_id>
omg state   # inspect active run before/after
```

The CLI should:

1. Read active run metadata under `.omg/state/`.
2. Signal PIDs recorded under `.omg/state/runs/` (PID files).
3. Mark run cancelled in authoritative state (CLI only).
4. Leave artifacts intact for post-mortem (do not delete unless user asks).

## PID / state layout (actual v0.1)

```text
.omg/state/
  active.json                 # pointer to active run_id (CLI-managed)
  runs/<run_id>/
    status.json               # authoritative status, verified, goal, mode
    pid                       # last launched grok PID (single file)
    last_argv.json            # last argv (if written by mode launcher)
    last_prompt.md            # last prompt (if written)
    prd.json                  # ralph scaffold (if present)
    launch_error              # present when Popen failed
```

`omg cancel` (when CLI available):

1. Loads active run (or `--run <id>`).
2. Best-effort `SIGTERM` to the PID in `runs/<id>/pid` (ignores missing process).
3. Marks status `cancelled` and clears `active.json` if it pointed at that run.
4. Leaves artifacts under `.omg/artifacts/` for post-mortem.

Manual fallback rules:

1. Prefer `omg cancel` always.
2. If CLI missing, kill by **PID file only**:

```bash
# Example — kill the recorded mode launcher PID
kill "$(cat .omg/state/runs/<run_id>/pid)" 2>/dev/null || true
```

3. **Never** use self-matching patterns:

```bash
# FORBIDDEN — wrapper/self argv can match and suicide or get blocked
pkill -f 'omg|grok|ulw|ralph'
pkill -f "spawn_subagent"
```

4. If you must scan processes, use `ps` + `awk` with a pattern that does **not** appear verbatim in the same shell command line, or kill only exact PIDs from files.

Note: v0.1 records a **single** PID per run (last launch). Process groups / multi-worker PIDs are not fully tracked yet.

## Session behavior after cancel

- Stop spawning new children.
- Do not continue implementing the cancelled goal.
- Summarize what was cancelled and where artifacts remain (`.omg/artifacts/`).
- Do not flip verified/passes; cancelled ≠ verified.

## Anti-patterns

- `pkill -f` with long patterns that match the cancel command itself.
- Killing unrelated `grok` / IDE sessions by name.
- Deleting `.omg/state/` wholesale without user request.
- Claiming "cancelled and verified" or clearing evidence silently.
