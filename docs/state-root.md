# State-root contract (#74 PR1)

Resolves **where** `.omg` state *would* live. This PR is the **contract
only** — not a writer cutover.

- API: `omg_cli.state_root.resolve_state_root`
- Writers still use `<project_root>/.omg`.
- No `omg` CLI flags for `OMG_STATE_DIR` (API / env only).
- The resolver does not `mkdir`, write, relocate trees, or mutate
  `passes` / `verified`.

Project **identity** is still [project-root.md](./project-root.md). This
module chooses the **physical state directory** for that identity.

## Surfaces

| Kind | Name | Role |
|------|------|------|
| API | `omg_cli.state_root.resolve_state_root` | Pure resolver |
| API | `explicit_state_dir=` | Same rank as `OMG_STATE_DIR` |
| API | `enable_workspace_marker=True` | Enable marker walk (unless killed) |
| API | `explicit_project_root=` / `here=` | Project identity (same idea as `--project-root` / `omg setup --here`) |
| Env | `OMG_STATE_DIR` | Central store root (storage only) |
| Env | `OMG_WORKSPACE_MARKER` | Enable nearest `.omg-workspace` (`1` / `true` / `yes` / `on`) |
| Env | `OMG_DISABLE_WORKSPACE_MARKER` | Kill switch; **wins** over env and API enable |
| Env | `OMG_PROJECT_ROOT` | Existing project-identity override |

There is no `omg --state-dir` (or similar) flag.

## State-dir precedence (highest first)

| Order | Trigger | `scope` | `source` | `state_dir` |
|------:|---------|---------|----------|-------------|
| 1 | `explicit_state_dir` or `OMG_STATE_DIR` | `centralized` | `centralized_env` | `canonical(central) / project_key` |
| 2 | Nearest `.omg-workspace` when enabled and not killed | `workspace_shared` | `workspace_marker` | `workspace_root / ".omg"` |
| 3 | Else | `per_worktree` | `project_derived` | `project_root / ".omg"` |

`scope` is exactly `per_worktree` | `workspace_shared` | `centralized`.

### Centralized (`OMG_STATE_DIR`)

- Storage only. Never replaces `project_root`.
- Sibling projects get distinct `project_key` directories under the same
  central root.
- Linked worktrees share `project_key` via the filesystem git **common
  dir** (`diagnostics.identity_kind=git_common`). Without git, identity
  is the project root.
- Per-worktree (no central env) still isolates each worktree:
  `<worktree>/.omg`.

### Workspace marker

Enabled only when `OMG_WORKSPACE_MARKER` is truthy **or**
`enable_workspace_marker=True`, **and** `OMG_DISABLE_WORKSPACE_MARKER` is
not truthy. Default is **off**: a present marker is ignored
(`diagnostics.marker=disabled`), even if malformed.

When enabled, walk ancestors of **cwd** (nearest wins). Kill switch
forces `per_worktree` (`diagnostics.marker=killed`). Centralized env
still wins without reading the marker.

### Default writers

Until a later cutover, product writers keep using
`<project_root>/.omg`. Calling this resolver does not relocate that
tree.

## Result

`StateRootResolution` (frozen):

| Field | Meaning |
|-------|---------|
| `project_root` | Canonical project identity |
| `state_dir` | Planned physical state directory (not created) |
| `source` | `centralized_env` \| `workspace_marker` \| `project_derived` |
| `scope` | `per_worktree` \| `workspace_shared` \| `centralized` |
| `project_key` | 64 lowercase hex from `path_keys.safe_path_key` (namespace `omg-state-root-v1`) |
| `diagnostics` | `Mapping[str, str]`, secret-free |
| `schema_version` | `1` (`STATE_ROOT_SCHEMA_VERSION`) |

Public dumps `to_public_dict()` / `serialize()` include `diagnostics`,
`project_key`, `schema_version`, `scope`, `source` only — **no raw
paths**, env values, or host tokens. Same injected inputs → byte-stable
`serialize()`.

Invalid input or a safety rejection raises `StateRootError`
(`ValueError`). The resolver is not wired to an `omg` subcommand.

### Diagnostics keys

| Key | Values |
|-----|--------|
| `authority` | always `none` |
| `home_scope` | `ok` \| `explicit_override` |
| `identity_kind` | `project_root` \| `git_common` \| `workspace` |
| `marker` | `disabled` \| `killed` \| `absent` \| `used` |
| `project_root_source` | `explicit` \| `env` \| `omg` \| `git` \| `cwd` \| `here` |
| `schema_version` | `"1"` |

## Marker format (v1)

Regular file named `.omg-workspace`. Opened with `O_NOFOLLOW`.
`nlink == 1`. UTF-8 JSON object **exactly**:

```json
{"version":1}
```

The directory that contains the file is the workspace root. The JSON
does not name a path.

Rejected when the marker is **enabled**:

- duplicate or unknown keys (including a `root` field)
- `version` not an integer (bool is not an integer)
- trailing data after the object
- future / unsupported `version`
- empty or non-UTF-8 or non-object
- larger than `MARKER_MAX_BYTES` (4096)
- symlink, hardlink, device, FIFO, socket, or non-regular file
- workspace root is `HOME` or the filesystem root

Disabled markers are not parsed.

## Project identity

Same order as [project-root.md](./project-root.md):

1. API `explicit_project_root` (CLI twin: `--project-root`)
2. `OMG_PROJECT_ROOT`
3. nearest real `.omg/` directory (symlinked `.omg` ignored for discovery)
4. filesystem git worktree root
5. cwd

`here=True` forces cwd and skips that discovery.

Git discovery **in this resolver** is filesystem-only (no subprocess):
`.git` directory, or a regular `nlink==1` `gitdir:` file plus
`commondir`. A symlinked `.git` yields no git identity. The existing
`omg_cli.project_root` helper still uses `git rev-parse`.

## HOME / filesystem-root safety

| Case | Behavior |
|------|----------|
| Implicit project root is `HOME` or `/` | Fail closed |
| Explicit `--project-root` / `OMG_PROJECT_ROOT` / `here` points there | Allowed; `home_scope=explicit_override` |
| Workspace marker selects `HOME` or `/` | Fail closed |
| `OMG_STATE_DIR` / `explicit_state_dir` is `/` | Fail closed |
| `OMG_STATE_DIR` points at `HOME` | Allowed (explicit storage); public dumps redact paths |
| Public diagnostics | Redact raw paths |

## Symlinks and planned paths

| Path | Rule |
|------|------|
| `.omg-workspace` symlink | Reject |
| `OMG_STATE_DIR` leaf symlink | Reject |
| `OMG_STATE_DIR` ancestor symlink | Reject |
| Existing non-symlink parent of a planned central path | Canonicalized (`.resolve()`); missing children appended, not created |
| Central store is filesystem root | Reject |
| Existing `<project_root>/.omg` symlink (per-worktree) | Reject |
| Existing `central / project_key` symlink | Reject |

`project_key` reuses `path_keys.safe_path_key`. This module does **not**
add a second write / confinement layer.

## Purity and authority

- No `mkdir`, write, network, or subprocess.
- Does not import `omg_cli.state`.
- `diagnostics.authority` is always `none`.
- Only existing OMG CLI state APIs may mutate `passes` / `verified`.

## Not in this PR

No search, session, replay, HUD, events, migration, install, or parity
work. No writer cutover. No `omg` flags for the state-dir env vars.
