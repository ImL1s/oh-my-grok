# Parity Refresh + Release Claim Gate (#78-C) Implementation Plan

I'm using the writing-plans skill to create the implementation plan.

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Fresh subagent per task + review between tasks.

**Goal:** Ship #78-C — pin refresh (plan-only), live-evidence freshness enforcement, and a release claim gate so upstream add/delete/rename, expired live evidence, and README/docs overclaims all exit non-zero before release.

**Architecture:** Add a plan-only refresh engine (`omg_cli/parity_refresh.py`) that diffs a fixture (or optional upstream checkout) against the canonical inventory and writes a review artifact under `.omg/artifacts/parity/` — never mutates maturity. Extend the shared parity check with a `--release` claim gate (`omg_cli/parity_claim_gate.py` + `parity_check.check_parity_release_claims`) that fails closed on (1) unresolved upstream drift vs last reviewed pin snapshot, (2) stale `live_verified` evidence, (3) marketing/docs claims that outrank inventory maturity. Wire the gate into `scripts/check_parity_inventory.py --release`, `omg parity refresh|check --release`, and `.github/workflows/release.yml`. Keep inventory `bootstrapping`; do not close #78 via PR keywords.

**Tech Stack:** Python 3.11+, pytest, existing `omg_cli/contracts/parity_schema.py`, `omg_cli/parity_check.py`, `scripts/check_parity_inventory.py`, argparse under `omg_cli/commands/inspect.py`.

**Source:** GPT Pro plan `/tmp/chatgpt-pro-issues-plan.txt` §`#78-C` @ main `707a3e71`; issue #78; prior plans `docs/plans/2026-08-05-parity-inventory-v2-78a.md` (+ #78-B landed as PR #88).

**Review risk:** P0 — trust root for later “implemented / verified” claims.

**Out of scope:** Antigravity adapter (#67), Team v3 (#69), jobs (#68), promoting any product row to fake `live_verified`, network scrape on every PR CI, closing #78/#79, Antigravity installer (#77).

**Close-rule reminder:** After this slice #78 may still need careful close rules. PR body must say partial of #78 / remaining governance notes — **never** `closes #78` / `fix #78` keywords (GitHub auto-closed #78 after #78-A PR #85).

---

### Task 1: Branch + refresh review-artifact schema (failing tests)

**Files:**
- Create: `tests/test_parity_refresh.py`
- Create: `omg_cli/parity_refresh.py` (skeleton later)
- Create: `tests/fixtures/parity/upstream_catalog_v1.json` (baseline snapshot shape)
- Create: `tests/fixtures/parity/refresh_review_schema_notes.md` only if needed for humans — prefer JSON fixtures

**Step 1: Write the failing tests**

```python
# tests/test_parity_refresh.py
def test_refresh_plan_emits_review_artifact_without_mutating_inventory(tmp_path):
    """--plan writes artifact; omg-parity.json bytes unchanged; no maturity upgrades."""

def test_refresh_plan_classifies_upstream_added_capability(tmp_path):
    """New upstream id → change_kind=added in review artifact."""

def test_refresh_plan_classifies_upstream_deleted_capability(tmp_path):
    """Missing upstream id → change_kind=deleted."""

def test_refresh_plan_classifies_upstream_renamed_capability(tmp_path):
    """Same source_paths / promise fingerprint, different id → change_kind=renamed (or deleted+added pair with rename hint)."""

def test_refresh_plan_never_auto_upgrades_maturity(tmp_path):
    """Even if upstream row looks 'done', planned inventory patch keeps maturity=catalogued."""

def test_refresh_rejects_apply_without_explicit_break_glass(tmp_path):
    """omg parity refresh without --plan (or --apply) must fail closed; no silent apply path in #78-C."""
```

**Step 2: Run tests to verify they fail**

```bash
PYTHONPATH=. .venv/bin/python -m pytest -q \
  tests/test_parity_refresh.py::test_refresh_plan_emits_review_artifact_without_mutating_inventory \
  tests/test_parity_refresh.py::test_refresh_plan_classifies_upstream_added_capability \
  tests/test_parity_refresh.py::test_refresh_plan_classifies_upstream_deleted_capability \
  tests/test_parity_refresh.py::test_refresh_plan_classifies_upstream_renamed_capability \
  tests/test_parity_refresh.py::test_refresh_plan_never_auto_upgrades_maturity \
  tests/test_parity_refresh.py::test_refresh_rejects_apply_without_explicit_break_glass
```

Expected: FAIL (module / symbols missing).

**Step 3: Minimal refresh engine (plan-only)**

Implement in `omg_cli/parity_refresh.py`:

- `UpstreamCatalog` fixture JSON: `{ "source": "OMC", "pin_revision": "<40-hex>", "capabilities": [ { "id", "source_paths", "promise" } ] }`
- `build_refresh_plan(*, inventory, upstream_catalog, source, new_pin) -> dict`
- Diff by stable id: `added` / `deleted` / `changed` (path or promise drift) / `renamed` (optional heuristic: identical `source_paths` frozenset + promise, different id)
- Output review artifact schema (exact keys):

```json
{
  "store_kind": "parity_refresh_review",
  "schema_version": 1,
  "source": "OMC",
  "from_revision": "...",
  "to_revision": "...",
  "generated_at": "<iso8601>",
  "changes": [
    {"change_kind": "added", "capability_id": "...", "detail": {}},
    {"change_kind": "deleted", "capability_id": "...", "detail": {}},
    {"change_kind": "renamed", "from_id": "...", "to_id": "...", "detail": {}},
    {"change_kind": "changed", "capability_id": "...", "detail": {"fields": ["source_paths"]}}
  ],
  "proposed_inventory_patch": {
    "upstream_pins": {},
    "capabilities": []
  },
  "guards": {
    "auto_maturity_upgrade": false,
    "requires_manual_mapping": true
  }
}
```

- `write_refresh_review_artifact(repo_root, plan) -> Path` under `.omg/artifacts/parity/refresh-<source>-<to_revision[:12]>.json` (or `tmp_path` in tests via injectable root)
- `proposed_inventory_patch` capability stubs **must** force `"maturity": { "<runtime>": "catalogued" }` and empty live evidence — never copy a higher maturity from anywhere
- `apply_refresh_plan` is **not** implemented in #78-C (or exists only as `raise NotImplementedError` / break-glass stub returning error). Default CLI path is `--plan` only.

**Step 4: Re-run Task 1 tests → PASS**

**Step 5: Commit**

```bash
git add tests/test_parity_refresh.py tests/fixtures/parity/ omg_cli/parity_refresh.py
git commit -m "$(cat <<'EOF'
feat(parity): add plan-only upstream refresh review engine (#78-C)

EOF
)"
```

---

### Task 2: CLI `omg parity refresh --plan`

**Files:**
- Modify: `omg_cli/commands/inspect.py` (`cmd_parity`, `register_inspect_parsers`)
- Modify: `docs/skills.md` (parity row — keep docs↔CLI drift test green)
- Modify: `tests/test_command_family_inspect.py`
- Modify: `tests/test_docs_cli_drift.py` only if argparse surface list needs update
- Extend: `tests/test_parity_refresh.py`

**Step 1: Failing CLI tests**

```python
def test_parity_refresh_plan_cli_writes_artifact(tmp_path, monkeypatch, capsys):
    """main(['parity','refresh','--source','OMC','--pin', PIN, '--plan', '--catalog', path]) → 0 + artifact."""

def test_parity_refresh_without_plan_flag_exits_nonzero(tmp_path):
    """Missing --plan → exit 2 (or 1) and no inventory write."""

def test_parity_refresh_uses_global_json_envelope(tmp_path, capsys):
    """--json emit_data surface parity.refresh."""
```

**Step 2: Run → FAIL**

```bash
PYTHONPATH=. .venv/bin/python -m pytest -q \
  tests/test_parity_refresh.py::test_parity_refresh_plan_cli_writes_artifact \
  tests/test_parity_refresh.py::test_parity_refresh_without_plan_flag_exits_nonzero \
  tests/test_parity_refresh.py::test_parity_refresh_uses_global_json_envelope
```

**Step 3: Wire argparse**

Under existing `parity_sub`:

```python
p_parity_refresh = parity_sub.add_parser(
    "refresh",
    parents=[common],
    help="plan-only upstream pin refresh (writes review artifact; never upgrades maturity)",
)
p_parity_refresh.add_argument("--source", required=True, choices=[...SOURCE_STATUS_IDS...])
p_parity_refresh.add_argument("--pin", required=True, help="full git commit oid")
p_parity_refresh.add_argument(
    "--plan",
    action="store_true",
    help="required: emit review artifact only (no inventory mutation)",
)
p_parity_refresh.add_argument(
    "--catalog",
    default=None,
    help="path to upstream catalog fixture JSON (tests / offline)",
)
p_parity_refresh.set_defaults(func=cmd_parity, parity_action="refresh")
```

In `cmd_parity`:
- `action == "refresh"`: require `--plan`; load inventory + catalog; call `build_refresh_plan` + `write_refresh_review_artifact`; `emit_data(args, "parity.refresh", {...})`; return 0
- Update error string: `run|release-readback|check|gaps|refresh`

Update `docs/skills.md` table row to include `refresh`.

**Step 4: Tests PASS**

**Step 5: Commit**

```bash
git add omg_cli/commands/inspect.py docs/skills.md tests/test_parity_refresh.py tests/test_command_family_inspect.py
git commit -m "$(cat <<'EOF'
feat(parity): wire omg parity refresh --plan CLI (#78-C)

EOF
)"
```

---

### Task 3: Release claim gate — overclaim + expired live evidence

**Files:**
- Create: `omg_cli/parity_claim_gate.py`
- Modify: `omg_cli/parity_check.py` (export `check_parity_release_claims`)
- Create: `tests/test_parity_claim_gate.py`
- Create fixtures under `tests/fixtures/parity/claims/`:
  - `readme_overclaim.md` (contains `live_verified` / “full 1:1” / green-check style claims)
  - `readme_honest.md`
  - `inventory_with_expired_live.json` (or build in-test from canonical copy)

**Step 1: Failing tests (exact names required for acceptance)**

```python
# tests/test_parity_claim_gate.py

def test_release_gate_rejects_expired_live_evidence(tmp_path):
    """live_verified row with observed_at older than live_evidence_max_age_days → non-zero / ContractValidationError."""

def test_release_gate_rejects_readme_overclaim(tmp_path):
    """README/docs claim text exceeding inventory maturity → fail."""

def test_release_gate_rejects_unresolved_upstream_add(tmp_path):
    """Upstream catalog has added id not reflected in inventory and no accepted refresh review → fail."""

def test_release_gate_rejects_unresolved_upstream_delete(tmp_path):
    """Inventory still lists capability deleted upstream without review disposition → fail."""

def test_release_gate_rejects_unresolved_upstream_rename(tmp_path):
    """Rename drift without review artifact → fail."""

def test_release_gate_passes_honest_bootstrapping_inventory(tmp_path):
    """Canonical bootstrapping inventory + honest docs → ok (gaps may remain open)."""
```

Overclaim scanner rules (YAGNI but fail-closed):
- Scan allowlisted marketing surfaces: `README.md`, `docs/parity/SUMMARY.md`, `docs/parity/FEATURE-MATRIX.md`, `docs/parity/SUMMARY.zh.md`, `docs/parity/SUMMARY.zh-TW.md` (generated files must already refuse `%`/`✅` while bootstrapping — gate double-checks)
- Forbidden while any capability peak maturity `< healthy` **or** inventory incomplete: phrases matching `(?i)live[ _-]?verified`, `full 1:1`, `complete parity`, `✅`, bare `parity \d+%`
- If a doc names a capability id and claims `healthy`/`live_verified`/`implemented` above the row’s `max_runtime_maturity`, fail with that id in the error

Expired live evidence:
- Reuse `validate_parity_inventory(..., now=fixed_now)` / `_assert_fresh_live_evidence` — release gate always runs with `strict=True` semantics + explicit `now`
- Simulate by cloning a row to `live_verified` with stale `observed_at`

Unresolved upstream drift:
- Release gate accepts optional `--upstream-catalog` / default fixture path `docs/parity/upstream-snapshots/<SOURCE>.json` **or** a recorded `last_reviewed_catalog_sha` in inventory later — for #78-C use **fixture catalogs checked into** `tests/fixtures/parity/upstream/` and a repo path `docs/parity/upstream-snapshots/` seeded from the same pin revisions as `upstream_pins`
- Algorithm: `build_refresh_plan(inventory, catalog)` → if any `changes` non-empty and no matching accepted review marker file under `.omg/artifacts/parity/` **or** (for CI) under `docs/parity/refresh-reviews/` committed disposition — fail
- Simpler #78-C acceptance path: gate takes `expected_catalog` + `inventory`; any add/delete/rename in the diff fails unless `review_artifact` path is passed **and** lists those changes with `disposition: acknowledged` (reviews are artifacts, not maturity upgrades)

**Step 2: Run → FAIL**

```bash
PYTHONPATH=. .venv/bin/python -m pytest -q tests/test_parity_claim_gate.py
```

**Step 3: Implement `parity_claim_gate.py`**

```python
def scan_docs_for_overclaims(*, repo_root: Path, inventory: dict) -> list[str]:
    ...

def assert_live_evidence_fresh(inventory: dict, *, now: datetime | None = None) -> None:
    # validate_parity_inventory already enforces for live_verified; call it with now=
    ...

def assert_upstream_drift_resolved(
    *,
    inventory: dict,
    upstream_catalog: dict,
    review_artifact: dict | None,
) -> None:
    ...

def check_parity_release_claims(
    *,
    inventory_path: Path,
    repo_root: Path,
    upstream_catalog_path: Path | None = None,
    review_artifact_path: Path | None = None,
    now: datetime | None = None,
) -> dict:
    """Return ok payload or raise ContractValidationError."""
```

Wire `check_parity_release_claims` from `parity_check.py` for a single import surface.

**Step 4: Tests PASS**

**Step 5: Commit**

```bash
git add omg_cli/parity_claim_gate.py omg_cli/parity_check.py tests/test_parity_claim_gate.py tests/fixtures/parity/
git commit -m "$(cat <<'EOF'
feat(parity): add release claim gate for drift, freshness, overclaim (#78-C)

EOF
)"
```

---

### Task 4: Script + CLI `--release` + simulate acceptance matrix

**Files:**
- Modify: `scripts/check_parity_inventory.py` — add `--release` flag
- Modify: `omg_cli/commands/inspect.py` — `parity check --release`
- Extend: `tests/test_parity_check.py` and/or `tests/test_parity_claim_gate.py`
- Create: `tests/test_parity_release_gate_acceptance.py` (end-to-end simulations)

**Step 1: Failing acceptance tests**

```python
# tests/test_parity_release_gate_acceptance.py

def test_simulate_upstream_add_makes_release_gate_nonzero(tmp_path):
    ...

def test_simulate_upstream_delete_makes_release_gate_nonzero(tmp_path):
    ...

def test_simulate_upstream_rename_makes_release_gate_nonzero(tmp_path):
    ...

def test_simulate_expired_live_evidence_makes_release_gate_nonzero(tmp_path):
    ...

def test_simulate_release_overclaim_makes_release_gate_nonzero(tmp_path):
    ...

def test_script_check_parity_inventory_release_flag(tmp_path):
    """subprocess: python scripts/check_parity_inventory.py --release → matches library."""
```

Each simulation:
1. Copy minimal inventory + catalog + fake README into `tmp_path`
2. Mutate (add/delete/rename catalog entry, expire live stamp, inject overclaim sentence)
3. Run `check_parity_release_claims(...)` and/or CLI/`scripts/check_parity_inventory.py --release`
4. Assert exit code `!= 0` and error mentions the class of failure

**Step 2: Run → FAIL**

**Step 3: Wire flags**

`scripts/check_parity_inventory.py`:

```python
parser.add_argument(
    "--release",
    action="store_true",
    help="fail closed on upstream drift, stale live evidence, and docs overclaim",
)
# when --release: imply strict path checks + claim gate
```

`omg parity check --release` → same library path; JSON envelope `parity.check` includes `"release": true`.

**Step 4: Acceptance tests PASS**

**Step 5: Commit**

```bash
git add scripts/check_parity_inventory.py omg_cli/commands/inspect.py \
  tests/test_parity_release_gate_acceptance.py tests/test_parity_check.py
git commit -m "$(cat <<'EOF'
feat(parity): expose --release claim gate via script and CLI (#78-C)

EOF
)"
```

---

### Task 5: Seed upstream snapshots + docs honesty + release.yml

**Files:**
- Create: `docs/parity/upstream-snapshots/OMC.json` (and OMX, OmO, Antigravity) — catalog derived from current inventory rows for that source at pinned revision (ids + source_paths + promise only)
- Modify: `docs/parity/README.md`, `docs/parity/schema-v2.md` — document refresh + release gate
- Modify: `.github/workflows/release.yml` — replace bare `check_parity_inventory.py` with `--release` (or add a dedicated step)
- Optionally keep PR CI on `--strict` only (network-free); `--release` is release-job + local acceptance
- Modify: `docs/parity/omg-parity.json` gap rows:
  - close or downgrade `gap.parity.governance.remaining` / `gap.parity.platform_live_evidence` **only if** this slice truly satisfies them; otherwise update summaries to “refresh+claim gate landed; completeness promotion still manual”
- Modify: `CHANGELOG.md` `[Unreleased]`
- Do **not** set `inventory_status` / `source_status` to `complete` in this PR (completeness still requires human review of full upstream surface; gate enables honest promotion later)

**Step 1: Failing tests**

```python
def test_upstream_snapshots_match_inventory_pins():
    """Each docs/parity/upstream-snapshots/*.json pin_revision equals upstream_pins[source].revision."""

def test_release_yml_invokes_parity_release_gate():
    """Workflow text includes check_parity_inventory.py --release (or omg parity check --release)."""
```

**Step 2: Implement snapshots + workflow + docs**

Release verify job snippet:

```yaml
- name: Frozen parity and generated checks
  run: |
    python scripts/check_parity_inventory.py --strict --release
    python scripts/check_traceability.py
    ...
```

**Step 3: Tests PASS + regen docs if needed**

```bash
python3 scripts/generate_parity_docs.py --check
python3 scripts/check_parity_inventory.py --strict
python3 scripts/check_parity_inventory.py --release
./bin/omg parity check --strict --json
./bin/omg parity refresh --source OMC --pin <current> --plan --catalog docs/parity/upstream-snapshots/OMC.json --json
```

Expected: refresh with identical catalog → empty `changes` (or no-op review); `--release` ok on clean tree.

**Step 4: Commit**

```bash
git add docs/parity/ .github/workflows/release.yml CHANGELOG.md tests/
git commit -m "$(cat <<'EOF'
feat(parity): seed upstream snapshots and gate release workflow (#78-C)

EOF
)"
```

---

### Task 6: Packaging ownership + full hermetic gate

**Files:**
- Modify: `omg_cli/contracts/parity_schema.py` `OMG_OWNER_PATTERNS` if new modules must be owned (`parity_refresh.py`, `parity_claim_gate.py`, upstream-snapshots)
- Modify: `tests/test_packaging.py` / `tests/test_install_cmd.py` only if shipping roots must include new docs paths (snapshots should ship under `docs/parity`)
- Run writer-ownership / static checks

**Step 1: Failing ownership test if patterns omit new files**

**Step 2: Add patterns + ensure snapshots ship**

**Step 3: Full gate**

```bash
PYTHONPATH=. .venv/bin/python -m pytest -q \
  tests/test_parity_refresh.py \
  tests/test_parity_claim_gate.py \
  tests/test_parity_release_gate_acceptance.py \
  tests/test_parity_check.py \
  tests/test_parity_inventory_v2.py \
  tests/test_parity_generation.py \
  tests/test_command_family_inspect.py \
  tests/test_docs_cli_drift.py \
  tests/test_packaging.py

python3 scripts/generate_parity_docs.py --check
python3 scripts/check_parity_inventory.py --strict --release
bash scripts/static_checks.sh
```

**Step 4: Commit**

```bash
git add omg_cli/contracts/parity_schema.py tests/
git commit -m "$(cat <<'EOF'
chore(parity): own refresh/claim-gate modules in shipping identity (#78-C)

EOF
)"
```

---

### Task 7: PR hygiene (no auto-close) + handoff

**Do not implement product features beyond the gate.**

PR title: `feat(parity): #78-C pin refresh plan + release claim gate`

PR body **must** include:
- Partial of #78 only — **do not** use `closes #78` / `fix #78` / `close #78`
- Explicit: refresh is `--plan` only; no auto maturity upgrade; no fake `live_verified`
- Out of scope: #67 / #68 / #69
- Acceptance checklist mirroring Pro:
  - [ ] simulate upstream add → `--release` non-zero
  - [ ] simulate upstream delete → non-zero
  - [ ] simulate upstream rename → non-zero
  - [ ] expired live evidence → non-zero
  - [ ] release overclaim → non-zero
  - [ ] honest bootstrapping tree → zero
- Note: #78 remains OPEN after merge until maintainers apply careful close rules (keyword-safe)

Merge only if GPT Pro review reports 「無 P2 以上問題」.

---

## Execution handoff

Plan complete and saved to `docs/plans/2026-08-05-parity-refresh-claim-gate-78c.md`.

**Two execution options:**

1. **Subagent-Driven (this session)** — REQUIRED SUB-SKILL: `superpowers:subagent-driven-development` — fresh subagent per task, review between tasks
2. **Parallel Session (separate)** — open a new session with `superpowers:executing-plans`, batch with checkpoints

**Which approach?**
