# Verification summary — 2026-07-20

Public, path-scrubbed record of gates run on the author machine after core-purpose parity / P0 ship.  
**Raw machine logs are not shipped** — regenerate under `docs/research/live/` (gitignored) via `scripts/live_suite.sh` and `scripts/canary_pretool.py`. See [`live/README.md`](./live/README.md).

## Hermetic (reproducible on any clone)

| Gate | Result (author, 2026-07-20) | How to re-run |
|------|----------------------------|---------------|
| Unit / integration | **402** passed (author re-run 2026-07-20 post-OSS hygiene) (`pytest -m 'not live'`) | `python -m pytest -q -m "not live"` |
| Smoke e2e | **OK** + `ALL_REAL_E2E_OK` | `OMG_E2E=1 ./scripts/smoke.sh` |
| Plugin validate | **OK** | `grok plugin validate .` |
| `omg doctor` (hard) | **OK** (soft WARN: foreign orch / Claude hooks OK) | `omg doctor` |

CI: `.github/workflows/ci.yml` runs hermetic pytest on Python 3.11 and 3.12.

## Live (requires Grok auth + quota; local evidence only)

| Gate | Result (author, 2026-07-20) | Notes |
|------|----------------------------|-------|
| `canary_pretool.py --live` | **exit 0** | Parent host deny + child capability isolation pass path |
| `live_suite.sh --quick` | **OK** | Canary + ulw + ralph + accept |
| `live_suite.sh --full` | **OK** | Incl. dual-review semantic (not false APPROVE → verified) |
| `live_suite.sh --quota-heavy` | **OK** earlier same day; not always re-run | Cap-spawn no-shell on correct RW spawn; cancel killpg |

Narrative suite write-up (no binary logs): [`live-gates-2026-07-20-suite.md`](./live-gates-2026-07-20-suite.md).  
Council STATUS: [`omc-parity-council/STATUS.md`](./omc-parity-council/STATUS.md).

## Claim language

| Allowed | Forbidden |
|---------|-----------|
| Hermetic suite green + CI | “Production hard sandbox” from unit green alone |
| Soft-gate + capability_mode live evidence when re-run | “Plugin hooks alone guarantee isolation” |
| `verified` only via `omg accept` / CLI path | Models may set `verified` |
| Core-purpose parity subset | Full OMC surface parity |

## Open-source note

Absolute home paths and suite JSON/logs were removed from the public tree in the packaging hygiene commit. History may still contain older live artifacts; working tree + default clone do not ship them.
