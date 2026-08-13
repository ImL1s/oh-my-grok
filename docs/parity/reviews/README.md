# Committed pin-transition refresh reviews (immutable ledger).

Ledgers live at `docs/parity/reviews/<source>-<from>-<to>-<digest>.json`.

- Comparison sources (`OMC` / `OMX` / `OmO` / `Antigravity`) use upstream catalogue digests from `build_refresh_plan`.
- Host baseline (`GROK_BUILD`) uses the same filename convention with an extra `host_baseline` object (`snapshot_hash`, `reviewed_pin`, `generated_docs_hash`) produced by `build_host_baseline_refresh_plan` — do not invent a parallel `review-ledgers/` tree.
- GROK_BUILD filename digest is **content-bound**: `sha256` of `{change_digest, snapshot_hash, generated_docs_hash}`. The JSON `change_digest` field stays the changes-only digest. When snapshot/docs hashes change, mint a new receipt; leave older files as historical provenance. Do not rewrite hashes in place.
- Current-content and pin-transition gates share one fail-closed validator: exact non-bool `schema_version`, exact `store_kind`/`source`/`from`/`to`/`reviewed_pin`/`previous_pin`/`snapshot_path`, canonical `changes` + `change_digest`, recomputed `content_binding_digest`, filename bind, required acknowledgments, and a HEAD-committed blob. Nested `host_baseline` hashes alone never authorize; untracked files never authorize; there is no change_digest-only filename fallback.
