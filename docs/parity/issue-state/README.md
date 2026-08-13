# Pinned issue-state evidence

`v1.json` is **bounded release-time** observation of GitHub issue state
for closure-sensitive claims (`#67`, `#68`, `#78`). It is **not**
perpetual live GitHub truth: `omg parity check --strict` never calls the
network; it only hashes and interprets this committed receipt.

## Honesty

| Issue | Observed GitHub state at pin | Inventory role |
| --- | --- | --- |
| `#67` | `closed` / completed | historical closed P0 (`gap.antigravity.provider`) |
| `#68` | `closed` / completed | historical closed P0 (`gap.jobs.durable`) |
| `#78` | `open` / `reopened` | historical governance close (`gap.parity.governance.remaining`); close pending PR #158 merge |

`#78` is recorded as **open** because that is what GitHub returned at
`source.observed_at`. The receipt still sets `blocks_open_p0: true` so
the inventory cannot treat `#78` as an Open P0 owner. Do not rewrite
`observed_state` to `closed` without a close-event **after** the recorded
reopen event.

Freshness semantics are `release_pin`: the observation is pinned to
`observed_git_commit` + `observed_at`. This is not a live webhook.
`max_age_days` is valid only when `freshness.semantics` is `ttl` (test
fixtures); production `--strict` uses the release pin, not a TTL clock.

`content_digest` is SHA-256 of the object **without** `content_digest`
(canonical JSON: `sort_keys=True`, `separators=(",", ":")`,
`ensure_ascii=False`). Tampering fails closed. The validator never
contacts GitHub.

Source identity is exact: `host=github.com`, `owner=ImL1s`,
`name=oh-my-grok`, `html_url=https://github.com/ImL1s/oh-my-grok`.
`schema_version` must be the non-bool integer `1`. Every `issues` key
`#N` must carry exact integer `number` `N` and URL
`https://github.com/ImL1s/oh-my-grok/issues/N`.
