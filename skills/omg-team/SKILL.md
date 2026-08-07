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

Launch readiness is **provider-aware** (#99): the pane supervisor spawns the
provider child, records exact provider PID/PGID/start identity, and advances
monotonic phases (`pane_created` → `provider_spawned` → `provider_ready` →
`task_dispatched`; optional `mailbox_ack`). Only workers that reach the
configured gate (default `task_dispatched`) with a **live** provider identity
contribute to `startup_status=running`. `process_stable` also requires the
live cmdline/exe basename to match the expected provider binary (or an
explicit descriptor allowlist) — a mislabeled silent process cannot green.
Provisional ready keeps a bounded post-stable observe window for delayed
auth/trust (TUI idle uses a longer floor); auth *after* that finalize window
is out of scope (not an infinite watch). Legacy `omg team worker-ready` v1
receipts are `wrapper_ready_legacy` only and **cannot** false-green a new
launch. Mailbox `ACK` is optional enrichment and cannot elevate a dead or
unspawned provider. Auth/trust prompts → `blocked_start`. Timeout:
`OMG_TEAM_READY_TIMEOUT_MS` (default 45000). Partial/zero/blocked readiness
exits non-zero and leaves state for diagnosis — never silent dry-run/ULW
fallback. Explicit `--no-wait` → `unverified_start` only.

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

## Window topology (#96)

```text
┌─────────────┬──────────────┐
│   leader    │   worker 1   │
│  (kept)     ├──────────────┤
│             │   worker 2   │
│             ├──────────────┤
│             │   worker N   │
└─────────────┴──────────────┘
  same_window (default inside tmux)
```

- **Inside tmux (default):** `view_mode=same_window` — first worker
  `split-window -h -d` from the exact leader pane; later workers
  `split-window -v -d` on the worker stack; `main-vertical` layout with
  clamped leader width. Leader pane ID/PID/session/window stay unchanged and
  finish selected. `stop` / failed rollback kill **only** owned worker panes —
  never the shared leader window/session.
- **`--dedicated-window`:** legacy dedicated `omg-team-*` window (inside tmux
  only; refuse with `--detach` / outside tmux).
- **Outside tmux / `--detach`:** `view_mode=detached_session`.
- Persisted on plan-only, dry-run, and live `team.json` as `view_mode` (+
  `layout`). Legacy runs missing `view_mode` keep dedicated/detached stop
  behavior (never guessed as same_window).

```bash
omg team launch --workers 3 --goal "…"                 # same_window inside tmux
omg team launch --workers 3 --goal "…" --dedicated-window
omg team launch --workers 3 --goal "…" --detach        # detached_session
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
