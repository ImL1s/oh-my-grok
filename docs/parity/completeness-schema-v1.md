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
| Mapping | `parity-completeness-mapping` | `1` |
| Proof | `parity-completeness-proof` | `1` |

Top-level policy/mapping/proof `schema_version` stays `1`. Only
`discovery_rules.version` may be `1` or `2`.

## Policy (`parity-completeness-policy/v1`)

One policy per upstream parity source (`OMC` / `OMX` / `OmO` /
`Antigravity`). Defines the reviewed discovery boundary **without**
claiming the source is already covered.

Required fields:

- `store_kind`, `schema_version`
- `source`, `repository`
- `discovery_rules` (versioned):
  - `version` — `1` (JSON registry) or `2` (real-source extractors)
  - `authoritative_registries[]` — relative paths + `extraction_method`
    (v2 also requires `id` + `options`)
  - `category_assignment` — registry `kind` → inventory category
  - `non_surface_exceptions[]` — path + rationale + issue reference

### discovery_rules v1

Supported extraction method: `json_registry_v1` (JSON object with
`kind` + `entries[{id,path,anchor}]`). Discovery enumerates
**user-observable** registered surfaces — not file counts, README heading
counts, or inventory row counts.

### discovery_rules v2

Static extractors in `omg_cli/parity_discovery.py` (no upstream JS/TS/npm
execution). Admitted methods:

- `claude_plugin_skills_v1`
- `markdown_command_tree_v1`
- `typescript_agent_registry_v1`
- `commander_command_graph_v1`
- `claude_hooks_manifest_v1` (also admits `${PLUGIN_ROOT}` + optional `plugin_root`)
- `typescript_tool_family_graph_v1`
- `package_surface_v1` (optional `include_bins`)
- `omx_catalog_manifest_v1`
- `omx_help_surface_v1`
- `omx_launcher_bin_v1`
- `codex_plugin_manifest_v1`

V2 requires a committed **mapping store** and bidirectional
surface↔inventory coverage (every discovered surface mapped; every
non-alias inventory row for that source referenced).

Committed OMC and OMX proofs exist at their inventory pins but
`source_status` for both remains `bootstrapping` (unpromoted).
## Mapping (`parity-completeness-mapping/v1`)

Committed under `docs/parity/completeness/mappings/{SOURCE}.json`.

- `store_kind`, `schema_version`, `source`
- `surfaces[]` sorted by `surface_id`, each with `category` and sorted
  non-empty `capability_ids[]`

Legacy `{surface_id: [capability_id, …]}` dicts remain accepted by the
plan/build APIs for hermetic fixtures; committed artifacts use the store
form. For v2, the normalized mapping projection is part of
`coverage_digest`.

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

Committed layout:

- `docs/parity/completeness/policies/{SOURCE}.json`
- `docs/parity/completeness/mappings/{SOURCE}.json`
- `docs/parity/completeness/proofs/{SOURCE}.json`

OMC currently has a committed policy/mapping/proof triple that is
technically sufficient for source promotion, while canonical
`source_status.OMC` (and categories / inventory) remain `bootstrapping`.

## Artifact consistency vs source reproduction vs promotion

Three distinct outcomes — do not conflate them:

| Outcome | What it means | When |
| --- | --- | --- |
| Artifact consistency | Committed policy + mapping + proof digest-bind to inventory/seed; no orphan members | Network-free `--check` / strict inventory (no `--upstream-root`) |
| Source reproduction | Re-run extractors against an authenticated checkout at `pin_revision` | Maintainer `--plan` / `--check --upstream-root` or hermetic fixtures |
| Promotion | `source_status` / `category_status` / `inventory_status` set to `complete` | Explicit inventory edit + promotion gate; **not** implied by artifacts |

Network-free checks report `artifact_consistency_verified: true` and
`source_reproduced: false`. Only an explicitly supplied authenticated
checkout may report `source_reproduced: true`. Artifact verification never
mutates maturity or live evidence.

## Reproducibility boundary

- PR CI validates committed artifact consistency with **no network**.
- Full filesystem reproduction requires an explicitly supplied
  `--upstream-root` (maintainer `--plan` / `--check`, hermetic fixtures).
- Before hashing, the checkout is authenticated to `pin_revision`:
  `git rev-parse HEAD` must equal the pin, the path must be its own
  work-tree root, `git status --porcelain` must be empty, and every pin
  blob OID must match `git hash-object` of the worktree path (so
  `skip-worktree` / `assume-unchanged` mutations fail closed). All
  completeness git calls use `--no-replace-objects` /
  `GIT_NO_REPLACE_OBJECTS=1` so `refs/replace/<pin>` cannot rebind the
  pin. Registry and surface digests are read via `git cat-file` at the pin —
  not from possibly skewed worktree bytes. Provenance is stamped into
  `checkout_provenance`.
- Source paths must be relative, confined under the checkout root, regular
  files, and non-symlinks.
- Mapping a discovered surface to an alias-only row, unknown capability,
  cross-source row, or cross-category row fails closed.

## Bootstrapping example

Hermetic fixtures live under `tests/fixtures/parity/completeness/`:

- `upstream/OMC/` — tiny v1 `json_registry_v1` tree
- `real_source/OMC/` — synthetic v2 registry-syntax tree (not a copy of
  upstream OMC)

They remain explicitly bootstrapping examples for the gate; they do **not**
promote the canonical inventory.
