# Hash-anchored edit protocol V1 (#76)

Library contract in `omg_cli.hash_edit`, plus a public CLI wrapper
(`omg edit plan|apply`). It **supplements** host-native edit tools. It does
**not** make unobserved host edits hash-anchored. There is no Team / read-only
role authority, and no `.omg/state` writer.

A caller may claim this protocol was used only after `apply_hash_edit`
returns a `HashEditApplyResultV1` (including via `omg edit apply`). Constructing
that dataclass by hand is not proof.

Refs #76. This slice does not close the issue.

## Surfaces

| Function / command | Role |
|--------------------|------|
| `parse_hash_edit_descriptor` | Strict V1 descriptor (allowlisted keys, canonical digest) |
| `plan_hash_edit` | Pure planner: descriptor + caller-supplied current bytes |
| `apply_hash_edit` | Confined re-read, re-plan, atomic same-dir replace |
| `omg edit plan --input <descriptor.json>` | Read-only CLI over parse + plan (no file write) |
| `omg edit apply --input <descriptor.json>` | CLI apply via `apply_hash_edit` only |

`kind` is `omg.hash_edit.v1`. `schema_version` is the integer `1` (not `true`).

## Public CLI

Workspace root follows `--project-root` / `OMG_PROJECT_ROOT` / discovery.
`--input` is the operator's descriptor file (pretty JSON is accepted; the
library re-canonicalizes mappings).

`omg edit plan` reads the descriptor, then reads the target through the
same pinned `O_NOFOLLOW` directory walk as apply (size is checked before
the file is loaded). It plans and prints a JSON envelope. It does not
mutate the target, does not `patch(1)` a unified diff, and does not write
`.omg/state`.

`omg edit apply` builds a plan from current bytes, then calls
`apply_hash_edit` (re-read, re-plan under lock, splice at offsets, atomic
replace). Apply JSON is copy-safe: relative `path`, digests, offsets,
`rebased`, `preserved_mode`. It omits raw source, replacement, unified-diff
text, and local absolute paths (including `--input` read failures).

Neither command writes `passes` / `verified` or any `.omg/state` stamp that
claims OMG accepted the edit. This does **not** claim `omo.edit.hash_anchored`
host parity.

Library failures map to stable CLI codes (exit `1`). Missing/unreadable
`--input` is `E_HASH_EDIT_USAGE` (exit `2`).

| Code | Library exception |
|------|-------------------|
| `E_HASH_EDIT_USAGE` | missing/unreadable `--input` |
| `E_HASH_EDIT_DESCRIPTOR` | `HashEditDescriptorError` |
| `E_HASH_EDIT_INPUT` | `HashEditInputError` |
| `E_HASH_EDIT_BIND` | `HashEditBindError` |
| `E_HASH_EDIT_STALE` | `HashEditStaleError` |
| `E_HASH_EDIT_AMBIGUOUS` | `HashEditAmbiguousError` |
| `E_HASH_EDIT_PATH` | `HashEditPathError` |
| `E_HASH_EDIT_CONCURRENCY` | `HashEditConcurrencyError` |
| `E_HASH_EDIT_APPLY` | `HashEditApplyError` |
| `E_HASH_EDIT_PLAN` | other `HashEditPlannerError` |
| `E_HASH_EDIT` | other `HashEditError` |

Stale, ambiguous, and path errors fail closed. Apply on hosts without
`O_NOFOLLOW` / `fcntl` (win32) fail closed in the library; do not weaken
that floor.

## Descriptor (fail closed)

Required: `schema_version`, `kind`, `edit_id`, `producer`, `path`,
`base_sha256`, `old_text`, `replacement`, `before_context`, `after_context`,
and the four per-field `*_sha256` hashes (UTF-8 bytes of the matching field).

Optional: `run_id`, `task_id`, 1-based `original_start_line` /
`original_end_line` (hint only — never sole authorization),
`revalidation` (`require_base` or `unique_shift`; omitted ⇒ `require_base`),
`expires_at` (`YYYY-MM-DDTHH:MM:SSZ`, year 2000–2100).

Unknown keys and future schema versions fail closed. `path` is a canonical
workspace-relative POSIX path (no absolute / UNC / drive / `\` / `.` / `..` /
NFC mismatch / Cf spoofing).

## Planner (no I/O)

`plan_hash_edit(descriptor, HashEditCurrentFact(path, current_bytes))`
does not touch the filesystem, network, subprocesses, or the clock.
`expires_at` is stored, not evaluated.

Matching is exact `before + old + after` only. Zero hits ⇒ stale
(or bind error if the base digest still matches). More than one hit ⇒
ambiguous. Hint cannot pick among duplicates. Similar / fuzzy / LF↔CRLF /
NFC↔NFD never match. `unique_shift` may admit exactly one exact hit when
the file digest moved (`rebased=True`).

The plan carries before/after SHA-256, byte offsets of `old_text`, line
span, `rebased`, descriptor digest, and a deterministic unified diff +
digest. Apply **splices at those offsets**. Do not `patch(1)` the diff.

Current-file and planned-file limit: 16 MiB. `omg edit plan|apply` inspects
the target size and performs a bounded read **before** allocating the
contents (growing a file past the cap is `E_HASH_EDIT_INPUT`). Invalid
UTF-8 and NUL bytes are rejected.

## Apply (confined)

`apply_hash_edit(workspace_root, descriptor, plan)`:

1. Opens the workspace root with `O_DIRECTORY|O_NOFOLLOW` and walks the
   descriptor path from that fd (top-level files such as `README.md` are
   valid even when `/tmp` is a symlink).
2. Takes an advisory `flock` on the parent **directory** fd. No lock
   sidecar is created in the project tree.
3. `lstat` then `O_RDONLY|O_NOFOLLOW|O_NONBLOCK`. Rejects symlink
   ancestor/leaf, FIFO, device, socket, multi-link, and non-regular files.
4. Re-reads and re-plans under the lock. A digest change is concurrent
   failure, not a new unique_shift.
5. Writes with `atomic_write_bytes_at` (fsync) and read-back of bytes
   plus `stat.S_IMODE`. Failures before replace leave bytes unchanged.

**Mode contract:** the existing file's `stat.S_IMODE` bits (including
execute) are preserved. Owner, xattr, ACL, and flags are not.

The result is copy-safe: relative `path`, digests, offsets, `rebased`,
`preserved_mode`. It does not include raw source, replacement, unified-diff
text, or local absolute paths.

## Out of scope

Comment hygiene, simplifier roles, lifecycle hooks, MCP, Antigravity
projection, Team ownership / read-only role authority, and claiming
`omo.edit.hash_anchored` host parity.
