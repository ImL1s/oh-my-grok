# Parity schema v2 reference

Authoritative validator: `omg_cli.contracts.parity_schema.validate_parity_inventory` (dispatches on `schema_version`).

## Constants

- `PARITY_MATURITY_LEVELS` — ordered maturity enum
- `PARITY_V2_CLASSIFICATIONS` — claimability classifications
- `UPSTREAM_PIN_IDS` — `OMC`, `OMX`, `OmO`, `Antigravity`, `GROK_BUILD` (no `OMG`)

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

- `inventory_completion_claims_allowed(inventory)` — false while bootstrapping
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