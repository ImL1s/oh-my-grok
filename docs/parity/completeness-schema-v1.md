# Parity completeness artifacts (schema v1)

Machine-readable **policy** and **proof** contracts for promoting
`source_status` / `category_status` / `inventory_status` to `complete`.

Catalogue seeds under `docs/parity/upstream-snapshots/` are **not**
completeness evidence. This PR ships the gate only — canonical inventory
statuses remain `bootstrapping` (promotion remains unperformed and is
proof-gated).

Authoritative implementation: `omg_cli/parity_completeness.py`.
Maintainer entrypoint: `scripts/check_parity_completeness.py` (`--plan` /
`--check` only; never mutates inventory status).

## Store kinds

| Artifact | `store_kind` | `schema_version` |
| --- | --- | --- |
| Policy | `parity-completeness-policy` | `1` |
| Proof | `parity-completeness-proof` | `1` |

## Policy (`parity-completeness-policy/v1`)

One policy per upstream parity source (`OMC` / `OMX` / `OmO` /
`Antigravity`). Defines the reviewed discovery boundary **without**
claiming the source is already covered.

Required fields:

- `store_kind`, `schema_version`
- `source`, `repository`
- `discovery_rules` (versioned):
  - `authoritative_registries[]` — relative paths + `extraction_method`
  - `category_assignment` — registry `kind` → inventory category
  - `non_surface_exceptions[]` — path + rationale + issue reference

Supported extraction method in v1: `json_registry_v1` (JSON object with
`kind` + `entries[{id,path,anchor}]`). Discovery enumerates
**user-observable** registered surfaces — not file counts, README heading
counts, or inventory row counts.

## Proof (`parity-completeness-proof/v1`)

A proof generated deterministically from a policy and a supplied checkout
at the exact pinned revision.

Required bindings:

- `source`, `repository`, `pin_revision`
- `checkout_provenance` — `{ method: "git_head_clean", observed_revision }`
  (must equal `pin_revision`)
- `policy_digest`, `seed_digest`, `coverage_digest`
- `source_input_digest`, `surface_index_digest`
- `discovered_surfaces[]` — `surface_id`, `kind`, `category`,
  `source_path`, `anchor`, `content_digest`, `capability_ids[]`
- `unresolved_surfaces[]`
- `empty_category_partitions[]` — explicit empty category evidence

Digests are SHA-256 over canonical JSON projections (sorted keys, stable
list ordering). Coverage projection includes pins, capability IDs,
categories, classifications, upstream paths, aliases, and gap bindings —
not full inventory bytes.

## Promotion transaction

Shared by `omg parity check --strict` and
`scripts/check_parity_inventory.py --strict`:

1. `source_status[source] == complete` requires a valid proof for that
   source with **no** unresolved surfaces.
2. `category_status[category] == complete` requires valid proofs for
   **all four** parity sources and either discovered surfaces or an
   explicit empty partition for that category in each proof.
3. `inventory_status == complete` requires every source and category
   status to be complete (and therefore proof-gated).
4. A matching upstream seed catalogue alone is **insufficient**.
5. Drift in pin, policy digest, seed digest, coverage digest, mapping, or
   source input fails closed.

Committed layout (when a source is later promoted):

- `docs/parity/completeness/policies/{SOURCE}.json`
- `docs/parity/completeness/proofs/{SOURCE}.json`

## Reproducibility boundary

- PR CI validates committed artifact consistency with **no network**.
- Full filesystem reproduction requires an explicitly supplied
  `--upstream-root` (maintainer `--plan` / `--check`, hermetic fixtures).
- Before hashing, the checkout is authenticated to `pin_revision`:
  `git rev-parse HEAD` must equal the pin, the path must be its own
  work-tree root, `git status --porcelain` must be empty, and every pin
  blob OID must match `git hash-object` of the worktree path (so
  `skip-worktree` / `assume-unchanged` mutations fail closed). Registry and
  surface digests are read via `git cat-file` at the pin — not from possibly
  skewed worktree bytes. Provenance is stamped into `checkout_provenance`.
- Source paths must be relative, confined under the checkout root, regular
  files, and non-symlinks.
- Mapping a discovered surface to an alias-only row, unknown capability,
  cross-source row, or cross-category row fails closed.

## Bootstrapping example

Hermetic fixtures live under `tests/fixtures/parity/completeness/` (tiny
fake upstream — not a copy of real OMC/OMX/OmO/Antigravity trees). They
remain explicitly bootstrapping examples for the gate; they do **not**
promote the canonical inventory.
