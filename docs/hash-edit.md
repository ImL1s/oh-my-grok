# Hash-anchored edit protocol V1 (#76 PR1)

Library contract in `omg_cli.hash_edit`. It **supplements** host-native edit
tools. It does **not** make unobserved host edits hash-anchored. There is no
public CLI, no Team / read-only role authority, and no `.omg/state` writer.

A caller may claim this protocol was used only after `apply_hash_edit`
returns a `HashEditApplyResultV1`. Constructing that dataclass by hand is
not proof.

Refs #76. This PR1 does not close the issue.

## Surfaces

| Function | Role |
|----------|------|
| `parse_hash_edit_descriptor` | Strict V1 descriptor (allowlisted keys, canonical digest) |
| `plan_hash_edit` | Pure planner: descriptor + caller-supplied current bytes |
| `apply_hash_edit` | Confined re-read, re-plan, atomic same-dir replace |

`kind` is `omg.hash_edit.v1`. `schema_version` is the integer `1` (not `true`).

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

Current-file and planned-file limit: 16 MiB (growing a file past the cap
fails in the planner, before apply writes). Invalid UTF-8 and NUL bytes
are rejected.

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

## Out of scope (PR1)

Comment hygiene, simplifier roles, lifecycle hooks, MCP, Antigravity
projection, Team ownership, public `omg` commands, and claiming
`omo.edit.hash_anchored` host parity.
