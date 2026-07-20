# Live suite evidence — 2026-07-20 (UTC 2026-07-19 evening)

Executed via `scripts/live_suite.sh` after Tasks 1–8 hermetic landings.

## Hermetic baseline

| Check | Result |
|-------|--------|
| `pytest -q` | **274 passed** |
| `OMG_E2E=1 smoke` | OK + `ALL_REAL_E2E_OK` |
| `omg doctor` hard | global PreToolUse soft-gate **OK** |

## `--quick` (ts `20260719T185729Z`)

| Gate | Result |
|------|--------|
| L-CANARY | `DENIED_PARENT_AND_CHILD` exit 0 |
| L-ULW-1 | `LIVE-ULW-OK` |
| L-RALPH-1 | `LIVE-RALPH-OK` |
| L-ACCEPT-1 | fixed in `28c4337`; verified path proven on leftover + full/heavy runs |

## `--full` (ts `20260719T190043Z`)

| Gate | Result |
|------|--------|
| Canary / ulw / ralph / accept | all OK; `verified=true` after accept |
| L-DUAL-1 | live dual-review ran; verdict **REQUEST_CHANGES** on fixture README `base` (expected); **did not** set omg `verified` |

## `--quota-heavy` (ts `20260719T190456Z`)

| Gate | Result |
|------|--------|
| Prior gates | OK |
| **L-CAP-SPAWN** | **`DENIED_OR_RAN=denied`** — child `omg-executor` / `capability_mode=read-write` reported **no** `run_terminal_command` in toolset; CHILD_ID `019f7bc8-cd5d-75c2-b474-576dff5a1725` |
| **L-CANCEL** | `status=cancelled`, `kill_actions: ["leader:killpg:SIGTERM"]` |

Evidence dir: `docs/research/live/` (`canary-*.json`, `suite-*-*.summary.json`, `cap-spawn-*.txt`).

## Re-verify after P0 ship (same calendar day, afternoon)

Canonical write-up: [`live/verification-2026-07-20.md`](./live/verification-2026-07-20.md) · advisor honesty: [`omc-parity-council/STATUS.md`](./omc-parity-council/STATUS.md).

| Gate | Result |
|------|--------|
| pytest -m 'not live' | **301 passed** |
| canary --live | **DENIED_PARENT_HOST_CHILD_CAPABILITY** exit 0 (parent host signature + child no-shell capability) |
| live_suite --quick | **OK** (`suite-20260720T050557Z-quick`) |
| live_suite --full | **OK** + L-DUAL-1 semantic (`dual_rc=1`, `verdict=UNKNOWN`, not false APPROVE) (`suite-20260720T050859Z-full`) |
| quota-heavy | not re-run |

## Claim language (post this run)

| Allowed | Forbidden |
|---------|-----------|
| Soft-gate parent+child **deny** with global hook | Plugin hooks alone guarantee isolation |
| capability_mode live: implementer **without shell tool** | Hard sandbox / cannot escape interpreter on leader |
| dual-review live sequential ran; verified still CLI-owned | Native dual-review shipped |
| accept → `verified=true` only via omg CLI | Models may set verified |

## Residual notes

- Dual-review CLI summary line may print `APPROVE` while stage markdown is REQUEST_CHANGES (parser residual) — stages/artifacts remain source of truth.
- Leader still has shell by design (R1); isolation proof is on **spawned implementer**, not leader.
- `doctor --strict` still fails on host `~/.claude` compat WARNs (expected soft/compat).
