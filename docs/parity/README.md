# Parity inventory schema v2

Machine-readable claimability contract for oh-my-grok cross-runtime parity.

## Index

| Artifact | Role |
| --- | --- |
| [`omg-parity.json`](omg-parity.json) | Canonical inventory (authoritative) |
| [`schema-v2.md`](schema-v2.md) | Schema v2 field / validator reference |
| [`SUMMARY.md`](SUMMARY.md) | Generated summary (EN) |
| [`SUMMARY.zh.md`](SUMMARY.zh.md) | Generated summary (zh) |
| [`SUMMARY.zh-TW.md`](SUMMARY.zh-TW.md) | Generated summary (zh-TW) |
| [`FEATURE-MATRIX.md`](FEATURE-MATRIX.md) | Generated full capability matrix |
| [`MATRIX-OMC.md`](MATRIX-OMC.md) | Generated `OMC` matrix |
| [`MATRIX-OMX.md`](MATRIX-OMX.md) | Generated `OMX` matrix |
| [`MATRIX-OmO.md`](MATRIX-OmO.md) | Generated `OmO` matrix |
| [`MATRIX-Antigravity.md`](MATRIX-Antigravity.md) | Generated `Antigravity` matrix |
| [`GAPS.md`](GAPS.md) | Generated open / tracked gaps |
| [`upstream-snapshots/`](upstream-snapshots/) | Pinned upstream capability catalogues for release drift gate |

Regenerate with `python3 scripts/generate_parity_docs.py` (drift-gated via `--check`).

## Upstream snapshots and refresh

Each file under [`upstream-snapshots/`](upstream-snapshots/) (`OMC.json`, `OMX.json`, `OmO.json`, `Antigravity.json`) is a **catalogue seed** at the pinned revision:

```json
{ "source": "OMC", "pin_revision": "<40-hex>", "capabilities": [ { "id", "source_paths", "promise" } ] }
```

`pin_revision` must equal `upstream_pins[source].revision` in [`omg-parity.json`](omg-parity.json). Capability rows are derived from inventory rows whose `upstream.source` matches.

When upstream moves to a new revision, update the snapshot catalogue **first**, then plan:

```bash
# 1) Refresh docs/parity/upstream-snapshots/OMC.json capabilities for the new
#    upstream revision and set pin_revision to that same <new-40-hex>.
# 2) Emit a plan-only review (pin must match the catalogue pin_revision):
./bin/omg parity refresh --source OMC --pin <new-40-hex> --plan \
  --catalog docs/parity/upstream-snapshots/OMC.json --json
```

Review the emitted plan, acknowledge drift in a review artifact, update inventory pins/rows to match, then re-run checks. Plan-only refresh never mutates inventory maturity.

## Release claim gate

PR CI runs `--strict` only (schema, local paths, overclaim rules; gaps may remain open).

Release workflow (`release.yml`) runs **`--strict --release`**, which additionally:

- scans README / generated parity docs for forbidden overclaims while bootstrapping
- enforces live-evidence freshness for any `live_verified` maturity
- compares inventory upstream rows against `docs/parity/upstream-snapshots/*.json` (unresolved drift fails closed)

```bash
python3 scripts/check_parity_inventory.py --strict          # PR / local
python3 scripts/check_parity_inventory.py --strict --release  # release tags
./bin/omg parity check --strict --release --json
```

Seeding snapshots does **not** promote `inventory_status`, `source_status`, or `category_status` to `complete` — that requires a separate completeness promotion (#78 follow-up).

## Header

| Field | Meaning |
| --- | --- |
| `store_kind` | Always `parity_inventory` |
| `schema_version` | `2` |
| `inventory_status` | `bootstrapping` \| `complete` |
| `upstream_pins` | Exact upstream revisions only — **never** an OMG candidate commit |
| `category_status` | Per-category `bootstrapping` \| `complete` (#78-B taxonomy: runtime_orchestration, skills, agents_routing, team, jobs, hooks, tools_mcp, state_memory_observability, install_update, quality_visual_edit_safety, antigravity, platform_live_evidence, parity_governance) |
| `source_status` | Per-source `bootstrapping` \| `complete` (`OMC`, `OMX`, `OmO`, `Antigravity`) |
| `live_evidence_max_age_days` | Freshness window for `live_verified` |

## Maturity (ordered enum)

`catalogued` → `configured` → `installed` → `enabled` → `loadable` → `observed` → `healthy` → `live_verified`

A single enum value per runtime — not independent booleans. Higher levels require non-empty prerequisite evidence fields.

`live_verified` additionally requires runtime/platform/version-bound live evidence inside the freshness window.

## Classifications

`faithful`, `antigravity_native`, `omg_native`, `alias`, `host_owned`, `host_impossible`, `optional_unclaimed`, `excluded`

- `alias` must reference an existing non-alias canonical capability id.
- `host_impossible` / `excluded` cannot claim `healthy` or `live_verified`.

## Capability rows

Stable **user-observable** dotted ids (for example `antigravity.provider.adapter`), not requirement ids like `DUAL-001`.

Each row binds upstream pin + source paths, OMG implementation paths, per-runtime maturity, evidence, issue links, and gap notes.

## Completeness honesty

While the inventory header, any `category_status`, or any `source_status` is `bootstrapping`, generators must not emit parity percentages or green checkmark glyphs (`%` / checkmark / `✓`). Open gaps are expected and listed honestly in [`GAPS.md`](GAPS.md). Do not treat historical research matrices as claimability truth — prefer this inventory.

`complete` on `source_status` / `category_status` requires a **reproducible upstream completeness gate** (catalogue seed ≠ completeness). #78-C landed plan-only refresh + the release claim gate; completeness promotion remains manual — until then the canonical inventory stays `bootstrapping`.

## Migration

Schema v1 inventories remain valid via the migration fixture at `tests/fixtures/parity/omg-parity-v1.json`. Canonical `docs/parity/omg-parity.json` is v2.
