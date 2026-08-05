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

Regenerate with `python3 scripts/generate_parity_docs.py` (drift-gated via `--check`).

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

While the inventory header, any `category_status`, or any `source_status` is `bootstrapping`, generators must not emit parity percentages or green checkmarks (`%` / `✅` / `✓`). Open gaps are expected and listed honestly in [`GAPS.md`](GAPS.md). Do not treat historical research matrices as claimability truth — prefer this inventory.

`complete` on `source_status` / `category_status` requires a **reproducible upstream completeness gate** (catalogue seed ≠ completeness). That gate is deferred to #78-C / follow-up — until then the canonical inventory stays `bootstrapping`.

## Migration

Schema v1 inventories remain valid via the migration fixture at `tests/fixtures/parity/omg-parity-v1.json`. Canonical `docs/parity/omg-parity.json` is v2.
