# Project root selection (#22)

Project-scoped `omg` commands resolve **one** control-plane root for `.omg/`
state. Nested working directories no longer invent a second tree by default.

## Precedence (highest first)

| Order | Source | Notes |
|------:|--------|--------|
| 1 | `--project-root PATH` | Must exist and be a directory. Exit **2** if invalid. |
| 2 | `OMG_PROJECT_ROOT` | Same validation as explicit flag. |
| 3 | Nearest ancestor with a real `.omg/` directory | Symlinked `.omg` is ignored for discovery. |
| 4 | `git rev-parse --show-toplevel` | Linked worktrees use the worktree root. |
| 5 | Current working directory | Only when none of the above apply. |

## Special cases

| Case | Behavior |
|------|----------|
| `omg setup --here` | Force cwd; skip discovery (intentional nested init). |
| Nested `.omg` under a parent `.omg` | Nearest wins; stderr **warning** lists shadowed ancestors (no auto-merge/delete). |
| Install / `install-hook` / global rules | Install-scoped; not driven by project-root discovery for their install target. |
| Host launch (`omg --madmax`, interactive) | Uses the same discovery from cwd (no argv flag until parse). |
| Team pane supervisor (`omg team supervisor`) | Uses the validated `OMG_TEAM_LEADER_ROOT` (and matching state root). **Skips** ancestor discovery so nested worktree `.omg` directories do **not** print shadow warnings into the pane (#100). Ordinary interactive CLI outside that path still warns. |

## Team bootstrap diagnostics (#100)

Worker pane bootstrap is silent on success. Failures print one redacted line in the
pane and write a bounded `bootstrap.log` under:

```text
.omg/state/runs/<run>/team/<team-key>/workers/<worker-key>/bootstrap.log
```

Inspect via `omg team status <run> --full` (descriptor only — not pane scrollback).

## Diagnostics

- `omg doctor` prints `project_root` and `source=…`.
- Resolution API: `omg_cli.project_root.resolve_project_root`.

## State directory (#74)

The table above is **project identity** only. The physical `.omg` state
directory is a separate contract: [state-root.md](./state-root.md)
(`omg_cli.state_root.resolve_state_root`). Core run-state writers in
`omg_cli/state.py` (runs/, `active.json`, `create.lock`) honor that
directory. Other writers still use `<project_root>/.omg` until later
#74 slices. There is no `omg` flag for `OMG_STATE_DIR` (API / env only).

## Migration

If you already created a nested `.omg` by accident:

1. Prefer the intended root: `omg --project-root /path/to/repo state`
2. Or move/merge state manually; OMG does **not** delete nested trees automatically.
3. Remove the nested `.omg` only after you have copied any needed runs/notes.
