---
name: omg-team
description: Durable tmux team workers via omg team N[:role] "goal". Use when user says team, /team, tmux team, or wants parallel panes — not spawn_subagent ULW.
---

# omg-team — Durable tmux team (experimental)

Launch **real tmux worker panes** coordinated by the `omg` CLI. This is **not**
`spawn_subagent` / `omg-ultrawork` and **not** host `--madmax`.

Requires `OMG_EXPERIMENTAL_TMUX_TEAM=1`. Still experimental until live Grok smoke
promotion; do not claim full OMX `$team` parity.

## HARD RULES
- Launch authority is **only** the `omg team …` CLI. Do not fake team with
  `spawn_subagent`, and do not hand-write `passes` / `verified`.
- Prefer CLI-first mailbox/task ops (`omg team api …`) over ad-hoc `tmux send-keys`.
- Never `pkill -f`. Stop with `omg team stop` / `omg cancel`.
- Literal `/team` may be unavailable on Grok — use this skill /
  `/oh-my-grok:omg-team` / natural language `team 3 …`.

## Canonical launch

```bash
export OMG_EXPERIMENTAL_TMUX_TEAM=1
omg team 3:executor "fix flaky tests"
# equivalent:
omg team launch --workers 3 --role executor --goal "fix flaky tests"
```

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
