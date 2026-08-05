# Parity inventory schema v2

Machine-readable claimability contract for oh-my-grok cross-runtime parity.

## Header

| Field | Meaning |
| --- | --- |
| `store_kind` | Always `parity_inventory` |
| `schema_version` | `2` |
| `inventory_status` | `bootstrapping` \| `complete` |
| `upstream_pins` | Exact upstream revisions only — **never** an OMG candidate commit |
| `category_status` | Per-category `bootstrapping` \| `complete` |
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

While any category (or the inventory header) is `bootstrapping`, generators must not emit parity percentages or green checkmarks. Open gaps are expected and listed honestly.

## Migration

Schema v1 inventories remain valid via the migration fixture at `tests/fixtures/parity/omg-parity-v1.json`. Canonical `docs/parity/omg-parity.json` is v2.
