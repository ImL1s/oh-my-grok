# Parity schema v2 reference

Authoritative validator: `omg_cli.contracts.parity_schema.validate_parity_inventory` (dispatches on `schema_version`).

## Constants

- `PARITY_MATURITY_LEVELS` — ordered maturity enum
- `PARITY_V2_CLASSIFICATIONS` — claimability classifications
- `UPSTREAM_PIN_IDS` — `OMC`, `OMX`, `OmO`, `Antigravity`, `GROK_BUILD` (no `OMG`)
- `SOURCE_STATUS_IDS` — `OMC`, `OMX`, `OmO`, `Antigravity` (no `OMG`, no `GROK_BUILD`)
- `PARITY_CATEGORY_TAXONOMY` — required #78-B category keys (`runtime_orchestration`, `skills`, `agents_routing`, `team`, `jobs`, `hooks`, `tools_mcp`, `state_memory_observability`, `install_update`, `quality_visual_edit_safety`, `antigravity`, `platform_live_evidence`, `parity_governance`)
- `CATEGORY_STATUS_VALUES` — `bootstrapping` \| `complete` (shared by `category_status` and `source_status`)

## Top-level v2 fields

| Field | Notes |
| --- | --- |
| `inventory_status` | `bootstrapping` \| `complete` |
| `category_status` | map of category → status; keys must cover every entry in `PARITY_CATEGORY_TAXONOMY` (extra keys allowed); values ∈ `CATEGORY_STATUS_VALUES` |
| `source_status` | exact keys = `SOURCE_STATUS_IDS`; values ∈ `CATEGORY_STATUS_VALUES` |

`inventory_is_complete` / `inventory_completion_claims_allowed` require `inventory_status == complete` **and** every `category_status` **and** every `source_status` value == `complete`. Percent / green-check claims stay forbidden while any source or category is still bootstrapping.

`complete` is not a catalogue-seed claim: it requires a reproducible upstream completeness gate (deferred to #78-C / follow-up). Until that gate exists, keep sources and categories `bootstrapping`.

## Validation entry points

```python
from omg_cli.contracts.parity_schema import validate_parity_inventory

validate_parity_inventory(payload)  # schema only
validate_parity_inventory(payload, repo_root=root)  # also require omg_paths exist
validate_parity_inventory(
    payload,
    repo_root=root,
    upstream_roots={"Antigravity": Path("/pins/antigravity")},
)  # also require upstream source_paths exist under provided roots
```

## Claim helpers

- `inventory_completion_claims_allowed(inventory)` — false while inventory/category/source status is bootstrapping
- `claim_marker_for_capability(row, inventory=...)` — never emits `%` / ✅ while incomplete; never positive-claims `host_impossible` / `excluded`

## Strict check

```bash
python3 scripts/check_parity_inventory.py --strict
./bin/omg parity check --strict --json
```

`--strict` validates schema + local OMG path existence + overclaim rules. It does **not** require all gaps to be closed.

Under `--strict` (validator `repo_root` set):

- Claimable classifications (`faithful` / `omg_native` / `antigravity_native`) require non-empty `omg_paths` that exist under the repo.
- `healthy` / `live_verified` require `healthy_evidence` entries that are existing repo-relative paths (opaque strings like `"x"` fail closed).
- Alias rows may not exceed canonical maturity per runtime; aliases of `host_impossible` / `excluded` / `optional_unclaimed` cannot claim positive maturity. Claim markers for aliases derive from the canonical target.