# Parity schema v2 reference

Authoritative validator: `omg_cli.contracts.parity_schema.validate_parity_inventory` (dispatches on `schema_version`).

## Constants

- `PARITY_MATURITY_LEVELS` — ordered maturity enum
- `PARITY_V2_CLASSIFICATIONS` — claimability classifications
- `UPSTREAM_PIN_IDS` — `OMC`, `OMX`, `OmO`, `Antigravity`, `GROK_BUILD` (no `OMG`)
- `SOURCE_STATUS_IDS` — `OMC`, `OMX`, `OmO`, `Antigravity` (no `OMG`, no `GROK_BUILD`)
- `HOST_BASELINE_PIN_ID` — `GROK_BUILD` (host runtime baseline; **not** part of parity score)
- `PARITY_CATEGORY_TAXONOMY` — required #78-B category keys (`runtime_orchestration`, `skills`, `agents_routing`, `team`, `jobs`, `hooks`, `tools_mcp`, `state_memory_observability`, `install_update`, `quality_visual_edit_safety`, `antigravity`, `platform_live_evidence`, `parity_governance`)
- `CATEGORY_STATUS_VALUES` — `bootstrapping` \| `complete` (shared by `category_status` and `source_status`)

## Top-level v2 fields

| Field | Notes |
| --- | --- |
| `inventory_status` | `bootstrapping` \| `complete` |
| `category_status` | map of category → status; keys must cover every entry in `PARITY_CATEGORY_TAXONOMY` (extra keys allowed); values ∈ `CATEGORY_STATUS_VALUES` |
| `source_status` | exact keys = `SOURCE_STATUS_IDS`; values ∈ `CATEGORY_STATUS_VALUES` |

`inventory_is_complete` / `inventory_completion_claims_allowed` require `inventory_status == complete` **and** every `category_status` **and** every `source_status` value == `complete`. Percent / green-check claims stay forbidden while any source or category is still bootstrapping.

`complete` is not a catalogue-seed claim: it requires a reproducible upstream completeness **proof** (see [`completeness-schema-v1.md`](completeness-schema-v1.md)). The #78-C refresh + release claim gate is landed; the #78-D promotion proof gate is landed — **promotion remains unperformed and is proof-gated**. Keep sources and categories `bootstrapping` until explicitly promoted with valid proofs.

## Upstream snapshots

Pinned catalogues live under `docs/parity/upstream-snapshots/{OMC,OMX,OmO,Antigravity}.json`:

| Field | Notes |
| --- | --- |
| `source` | One of `SOURCE_STATUS_IDS` |
| `pin_revision` | 40-hex commit; must match `upstream_pins[source].revision` |
| `capabilities[]` | `{ id, source_paths, promise }` extracted from inventory rows at that pin |

Refresh workflow: `omg parity refresh --source … --pin … --plan --catalog …` emits a review plan; unresolved drift blocks `--release`.

## Host baseline (Grok Build)

`GROK_BUILD` is a **host runtime pin**, not a `SOURCE_STATUS_IDS` sibling. Machine-readable baseline:

- Snapshot: `docs/parity/upstream-snapshots/grok-build.json` (`store_kind=host_baseline_snapshot`)
- Validator: `validate_host_baseline_snapshot` (independent of `validate_upstream_catalog`)
- Classifications: `host_owned` \| `consumed_downstream` \| `irrelevant` (every release delta must be classified)
- Generated docs: `docs/parity/generated/host-baseline.md`, `host-capability-matrix.md`
- Pin transitions use the same committed ledger path as other sources:
  `docs/parity/reviews/GROK_BUILD-<from>-<to>-<digest>.json` with a required `host_baseline` block (`snapshot_hash`, `reviewed_pin`, `generated_docs_hash`). The filename digest for GROK_BUILD is content-bound (`change_digest` + current snapshot/docs hashes) so a new receipt can be added instead of rewriting an immutable ledger.

Release gate (`check_parity_release_claims`) requires the host snapshot to match `FROZEN_PINS["GROK_BUILD"]` and `upstream_pins.GROK_BUILD.revision`, rejects symlink/malformed/stale snapshots, and fails closed when a `GROK_BUILD` pin moves without a matching review. `host_owned` rows must not claim OMG `omg_paths` as implementation evidence. Catalogue maturity starts at `catalogued`; do not overclaim live promotion here.

## Issue-state evidence (closure-sensitive)

Pinned offline receipt: [`issue-state/v1.json`](issue-state/v1.json)
(`store_kind=parity-issue-state-evidence`, `schema_version` 1). `--strict`
with `repo_root` binds Open-P0 owners to this digest-bound observation —
no network. See [`issue-state/README.md`](issue-state/README.md).

This is **bounded release-time** evidence, not live GitHub. Observed pin:
`#67` / `#68` closed/completed; `#78` **open/reopened**, close pending
PR 158. Do not treat the receipt as current GitHub truth.

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
- `claim_marker_for_capability(row, inventory=...)` — never emits `%` / green-checkmark glyphs while incomplete; never positive-claims `host_impossible` / `excluded`

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
- Closure-sensitive GitHub issue state is bound to [`issue-state/v1.json`](issue-state/v1.json) (offline `release_pin`; `#78` observed open/reopened). Schema-only checks without `repo_root` skip this file.

## Release check

```bash
python3 scripts/check_parity_inventory.py --strict --release --base-ref <previous-v-tag>
./bin/omg parity check --strict --release --base-ref <previous-v-tag> --json
# optional: --base-inventory /path/to/base-omg-parity.json MUST be paired with
# --base-ref / OMG_PARITY_BASE_REF whose inventory blob matches the file
```

`--release` implies `--strict` and additionally runs the release claim gate (`omg_cli.parity_claim_gate`):

- doc overclaim scan (forbidden phrases / per-capability maturity overclaim while bootstrapping)
- live-evidence freshness for `live_verified` rows
- upstream drift vs `docs/parity/upstream-snapshots/*.json` (each unresolved add/delete/rename/change fails closed unless acknowledged in a refresh review artifact)
- **pin-transition ledger:** when any source pin changes vs the durable base inventory (`--base-ref` / `OMG_PARITY_BASE_REF`, previous `v*` release tag, or `origin/main|main` — not `HEAD^` in release mode; optional `--base-inventory` only when bound to a matching `--base-ref` blob), a **git-tracked** review must exist at `docs/parity/reviews/<source>-<from>-<to>-<change-digest>.json` with matching HEAD blob bytes, canonical change digest, and dispositions. Intermediate inventory pin bumps between the trusted base and the candidate are scanned via the `base_ref..HEAD` commit DAG (so an unreviewed bump cannot be masked by a later revert). `--base-inventory` alone is insufficient for `--release` (endpoint-only compare would miss mid-DAG transitions). Optional local `.omg/artifacts/` paths alone are not sufficient.

PR CI uses `--strict` only; `release.yml` uses `--strict --release`.