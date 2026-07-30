---
name: omg-team
description: Durable tmux team workers via omg team N[:role] "goal". Use when user says team, team N, tmux team, or wants parallel panes — not spawn_subagent ULW. Slash invoke is /oh-my-grok:omg-team only (no bare /team).
---

# omg-team — Durable tmux team (default on)

Launch **real tmux worker panes** coordinated by the `omg` CLI. This is **not**
`spawn_subagent` / `omg-ultrawork` and **not** host `--madmax`.

**Default on.** Kill switch: `OMG_DISABLE_TMUX_TEAM=1` (legacy
`OMG_EXPERIMENTAL_TMUX_TEAM=0` also disables). Isolation remains **integration**
only (worktree + seal) — not an execution sandbox. Do not claim full OMX
`$team` 33-op parity.

Launch readiness is **process-level** (`omg team worker-ready` receipt) before
the agent binary; mailbox `ACK` is optional enrichment. Timeout:
`OMG_TEAM_READY_TIMEOUT_MS` (default 45000). Partial/zero readiness exits
non-zero and leaves state for diagnosis — never silent dry-run/ULW fallback.

## HARD RULES
- Launch authority is **only** the `omg team …` CLI. Do not fake team with
  `spawn_subagent`, and do not hand-write `passes` / `verified`.
- Prefer CLI-first mailbox/task ops (`omg team api …`) over ad-hoc `tmux send-keys`.
- Never `pkill -f`. Stop with `omg team stop` / `omg cancel`.
- **Slash honesty (host probe 2026-07-25):** Grok advertises plugin skills as
  `/<plugin>:<skill>` (or the bare skill name when unambiguous). There is **no**
  plugin frontmatter / manifest field to register an unnamespaced `/team` alias
  for `omg-team`, and `grok inspect` already shows other plugins’ `team` skills.
  Document and invoke **`/oh-my-grok:omg-team` only** — do **not** claim bare
  `/team`. Natural language `team 3 …` and the terminal CLI remain valid.

## Canonical launch

```bash
omg team 3:executor "fix flaky tests"
# equivalent:
omg team launch --workers 3 --role executor --goal "fix flaky tests"
# disable: export OMG_DISABLE_TMUX_TEAM=1
```

In-session: `/oh-my-grok:omg-team 3:executor fix flaky tests` (or natural
`team 3 …`). Not `/team`.

Dry-run (state only, no tmux):

```bash
omg team 2:executor "map ownership" --dry-run
```

## Lifecycle

```bash
omg team status <team-name-or-run> --json
omg team resume <team-name-or-run>
omg team stop <team-name-or-run>    # shutdown alias → stop
omg team api send-message --input '{...}' --json
```

## When to use ULW instead

Independent in-session fan-out with no durable panes → `omg-ultrawork` /
`spawn_subagent` with explicit `capability_mode`.
