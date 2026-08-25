# Hash-anchored edit protocol V1 (#76)

Library contract in `omg_cli.hash_edit`, plus a public CLI
(`omg edit plan|apply|verify|comments|simplify`). It **supplements** host-native
edit tools. It does **not** make unobserved host edits hash-anchored. The
CLI never writes `passes` / `verified`.

A caller may claim the hash-anchored protocol was used only after
`apply_hash_edit` returns a `HashEditApplyResultV1` (including via
`omg edit apply`). Constructing that dataclass by hand is not proof.

Refs #76. This slice does not close the issue: there is still no
`omo.edit.hash_anchored` host parity.

## Surfaces

| Function / command | Role |
|--------------------|------|
| `parse_hash_edit_descriptor` | Strict V1 descriptor (allowlisted keys, canonical digest) |
| `plan_hash_edit` | Pure planner: descriptor + caller-supplied current bytes |
| `apply_hash_edit` | Confined re-read, re-plan, atomic same-dir replace |
| `omg edit plan --input <descriptor.json>` | Read-only CLI over parse + plan (no file write) |
| `omg edit apply --input <descriptor.json>` | CLI apply via `apply_hash_edit` only, after Team / read-only gates |
| `omg edit verify EDIT_ID\|--input <descriptor.json>` | Re-read + re-plan; report ok/stale/conflict; no file write |
| `verify_hash_edit` | Same `O_NOFOLLOW` confinement as apply; never writes `verified` |
| `omg edit comments --input PATH\|--git-diff\|--paths` | Language-aware AI-slop / comment report (optional conservative `--fix`) |
| `omg edit simplify --paths …` | Bounded simplifier assignment; optional `--provider grok` Jobs proposal (never applies) |

`kind` is `omg.hash_edit.v1`. `schema_version` is the integer `1` (not `true`).

## Public CLI

Workspace root follows `--project-root` / `OMG_PROJECT_ROOT` / discovery.
`--input` is the operator's descriptor file (pretty JSON is accepted; the
library re-canonicalizes mappings).

`omg edit plan` reads the descriptor, then reads the target through the
same no-follow confinement as apply (POSIX `O_NOFOLLOW` walk or Windows
`CreateFileW`/`NtCreateFile` reparse-reject; size is checked before
the file is loaded). It plans and prints a JSON envelope. It does not
mutate the target, does not `patch(1)` a unified diff, and does not write
`.omg/state`.

`omg edit apply` builds a plan from current bytes, then calls
`apply_hash_edit` (re-read, re-plan under lock, splice at offsets, atomic
replace). Apply JSON is copy-safe: relative `path`, digests, offsets,
`rebased`, `preserved_mode`. It omits raw source, replacement, unified-diff
text, and local absolute paths (including `--input` read failures). Successful
apply also writes a redacted artifact under `.omg/artifacts/edit/<digest>.json`.

`omg edit verify` takes `EDIT_ID` and/or `--input` (a V1 descriptor JSON;
positional `EDIT_ID` may be the descriptor file path). It re-reads the
target through the same pinned `O_NOFOLLOW` walk as apply, re-plans, and
prints copy-safe JSON (`status=ok` or stale/conflict). It does not mutate
the target, does not write `.omg/state`, and does not set `verified`.

**Team / role authority:** `apply` (and other mutating edit tools: `comments
--fix`, `simplify --apply-edits`) refuse when `OMG_CAPABILITY_MODE=read-only`
(`E_READ_ONLY`). When `--run-id`/`--task-id` or `OMG_RUN_ID`/`OMG_TASK_ID`
are set **and** an ownership manifest exists for that run, the target path
must be owned by the calling task (`E_OWNERSHIP`, via
`omg_cli.workers.load_ownership_manifest`). If no manifest exists, host
edits still proceed.

These commands do not write `passes` / `verified` or any `.omg/state` stamp that
claims OMG accepted the edit. This does **not** claim `omo.edit.hash_anchored`
host parity.

Library failures map to stable CLI codes (exit `1`). Missing/unreadable
`--input` / `EDIT_ID` is `E_HASH_EDIT_USAGE` (exit `2`).

| Code | Library exception |
|------|-------------------|
| `E_HASH_EDIT_USAGE` | missing/unreadable `--input` or `EDIT_ID` |
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
| `E_READ_ONLY` | `OMG_CAPABILITY_MODE=read-only` on mutating tools |
| `E_OWNERSHIP` | active ULW/Team manifest and path not owned by calling task |

Stale, ambiguous, and path errors fail closed. POSIX plan/apply use pinned
`O_NOFOLLOW` / `dir_fd`. Windows uses `CreateFileW` / `NtCreateFile` with
`FILE_FLAG_OPEN_REPARSE_POINT` and rejects symlink/mount-point reparse
(no `Path.open()` follow). Hosts with neither backend fail closed.

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

Current-file and planned-file limit: 16 MiB. `omg edit plan|apply|verify` inspects
the target size and performs a bounded read **before** allocating the
contents (growing a file past the cap is `E_HASH_EDIT_INPUT`). Invalid
UTF-8 and NUL bytes are rejected.

## Apply (confined)

`apply_hash_edit(workspace_root, descriptor, plan)`:

1. POSIX: opens the workspace root with `O_DIRECTORY|O_NOFOLLOW` and walks
   the descriptor path from that fd (top-level files such as `README.md` are
   valid even when `/tmp` is a symlink). Windows: `CreateFileW` on the root
   with `FILE_FLAG_OPEN_REPARSE_POINT`, then `NtCreateFile` relative opens
   with `FILE_OPEN_REPARSE_POINT`; symlink and mount-point reparse tags are
   rejected.
2. POSIX: advisory `flock` on the parent **directory** fd. Windows: exclusive
   share-mode open of the leaf. No lock sidecar is created in the project tree.
3. POSIX: `lstat` then `O_RDONLY|O_NOFOLLOW|O_NONBLOCK`. Windows: reparse
   inspection on the pinned handle. Rejects symlink ancestor/leaf, FIFO,
   device, socket, multi-link, and non-regular files. The pinned handle is
   read until the expected size or EOF.
4. Re-reads and re-plans under the lock. A digest change is concurrent
   failure, not a new unique_shift.
5. POSIX: `atomic_write_bytes_at` (fsync). Windows: same-directory exclusive
   temp + `MoveFileEx` replace from the parent handle's path. Read-back of
   bytes (POSIX also `stat.S_IMODE`). Failures before replace leave bytes
   unchanged.

**Mode contract:** the existing file's `stat.S_IMODE` bits (including
execute) are preserved. Owner, xattr, ACL, and flags are not.

The result is copy-safe: relative `path`, digests, offsets, `rebased`,
`preserved_mode`. It does not include raw source, replacement, unified-diff
text, or local absolute paths.

## Comment checker

`omg edit comments` is language-aware and **report-only by default**. It
flags redundant narration, AI/task meta-commentary, stale TODOs without
ownership, unverifiable claims, banner noise, best-effort
comment-vs-code inconsistency, copied prompt/reasoning, and optional
banned patterns from `.omg/edit-comments.json`. Findings include path,
line, rule id, severity, and a suggested repair. SPDX / Copyright /
`security:` comments are allowlisted and never flagged or deleted.

`--fix` is explicit and conservative: it only deletes whole-line
auto-fixable `ai_meta` / `banner_noise` comments. It does not rewrite
legal comments.

## Bounded simplifier

`omg edit simplify --paths …` is **disabled** unless `--enable` or
`.omg/simplify.json` has `enabled: true`. Bounds: extension list, max
files, max bytes. Generated / vendor / lock / minified paths are skipped.
A once-per-stage marker is written to `.omg/state/simplify-guard.json`
(not `verified`).

Default (no `--provider`) does **not** call an LLM and does not invent
edits. With no `--apply-edits` descriptors it records an assignment
artifact for `omg-code-simplifier` (`read-write`) and returns blocked with
`next_action` to spawn that role then `omg-code-reviewer` (`read-only`).
Optional `--provider grok` records the same assignment/guard, then starts
a Jobs grok job (`prompt_text`, bounded timeout) that must emit hash-edit
descriptor JSON. The CLI writes
`.omg/artifacts/` `omg.edit.simplify.proposal.v1` (job_id, provider=grok,
descriptors) and returns `ok:true` `verified:false` `status=proposed`.
It does **not** apply, does **not** write `passes`/`verified`, and still
requires independent review before `--apply-edits`. Grok missing, job
failure, or non-descriptor output is `E_SIMPLIFY_PROVIDER` (assignment
still recorded). The simplifier cannot approve itself.

`--apply-edits` applies descriptors in order. If a later descriptor fails,
prior successful applies **in that invocation** are rolled back to the
original bytes (same confined write as apply). Incomplete restore records
`status=dirty` / `failed` on the simplify guard and a redacted artifact;
it never writes `passes` / `verified`. A post-apply digest check runs
without spawning pytest.

## Out of scope

Lifecycle hooks, MCP, Antigravity projection, and claiming
`omo.edit.hash_anchored` host parity.
