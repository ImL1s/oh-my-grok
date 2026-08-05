# Parity Full Upstream Inventory (#78-B) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

I'm using the writing-plans skill to create the implementation plan.

**Goal:** Ship #78-B — complete OMC / OMX / OmO / Antigravity capability inventory rows plus generated FEATURE-MATRIX, per-source matrices, GAPS, README summary, zh/zh-TW translations, and historical-doc banners — without overclaiming and without doing #78-C.

**Architecture:** Extend the #78-A v2 substrate already on main (`25cc265` / PR #85). Add `source_status` beside `category_status`, expand the category taxonomy to the issue #78 scoring set, seed honest user-observable capability rows (mostly `catalogued` / `configured`, never fake `live_verified`), and grow `scripts/generate_parity_docs.py` so every generated artifact is `--check`-drift-gated. Keep `inventory_status` honest: percentages and ✅/✓ remain forbidden while any category **or** source is `bootstrapping`.

**Tech Stack:** Python 3.11+, pytest, `omg_cli/contracts/parity_schema.py`, `scripts/generate_parity_docs.py`, `scripts/check_parity_inventory.py`, existing `omg parity check|gaps`.

**Source:** GPT Pro `/tmp/chatgpt-pro-issues-plan.txt` § `#78-B`; GitHub issue #78 inventory scope; style template `docs/plans/2026-08-05-parity-inventory-v2-78a.md`.

**Base:** `main` after #85 / `25cc265`. Release `0.7.5` may land on main first; this plan is independent of that tag — rebase onto latest main before implementing.

**Implementation branch (code PR):** `feat/parity-78b-full-inventory` (created later by the executor; this plan-only commit lives on `plan/parity-78b`).

**Out of scope (explicit):**
- #78-C — pin refresh, live-evidence freshness, release claim gate, `omg parity refresh`
- Closing #78 entirely (leave `gap.parity.governance.remaining` open for #78-C; comment/reopen issue if GitHub shows CLOSED)
- Closing #79 or any of #67–#77
- Fake `live_verified` / `healthy` promotions without real evidence
- Antigravity adapter runtime (#67), durable jobs (#68), Team v3 (#69), capability-lock catalog rewrite
- Scraping live upstream `main` in ordinary CI (pins stay frozen; path existence only when `upstream_roots` provided)
- Changing PreToolUse / Stop / capability_mode product contracts

---

## Current substrate (do not reinvent)

| Piece | Path | State after #78-A |
| --- | --- | --- |
| Schema v2 | `omg_cli/contracts/parity_schema.py` | maturity enum, classifications, pins, `category_status`, claim helpers |
| Canonical inventory | `docs/parity/omg-parity.json` | **4** seed rows; all categories `bootstrapping` |
| Generators | `scripts/generate_parity_docs.py` | FEATURE-MATRIX.md + GAPS.md only |
| Strict gate | `scripts/check_parity_inventory.py`, `omg_cli/parity_check.py` | schema + local paths + overclaim |
| Tests | `tests/test_parity_inventory*.py`, `test_parity_generation.py`, `test_parity_check.py` | v2 claimability locked |
| Historical (stale) | `docs/research/core-parity-matrix-2026-07-20.md`, `docs/research/omc-parity-council/` | still say HUD/wiki/Team NEVER |

---

## Target taxonomy

### Categories (`category_status` keys — every key MUST be `bootstrapping` \| `complete`)

| Category key | Covers (user-observable) |
| --- | --- |
| `runtime_orchestration` | CLI vs in-session launch, Ralph, autopilot, pipeline, ultrawork |
| `skills` | skill catalog, aliases, pipelines, resources |
| `agents_routing` | agent catalog / tier / discipline routing |
| `team` | Team / worktrees / mailbox / workers (incl. existing `team.plane_v3` seed) |
| `jobs` | durable background jobs (existing `jobs.durable_background` seed) |
| `hooks` | hooks / lifecycle / Stop soft-pin honesty |
| `tools_mcp` | LSP/AST/MCP / tool sidecar surfaces |
| `state_memory_observability` | session/state, memory, wiki, HUD, notifications, friction/replay |
| `install_update` | plugin/setup/update/migration / multi-runtime install |
| `quality_visual_edit_safety` | UltraQA, visual verdict, dual-review, hash-anchored edit, release |
| `antigravity` | Antigravity provider / headless / permissions (existing seed) |
| `platform_live_evidence` | platform/runtime version matrix + live evidence policy placeholders |
| `parity_governance` | inventory/governance (existing seed; stays open via #78-C gap) |

Keep existing four seed capability IDs; **move** them into the matching category above if needed (`team.plane_v3` → `team`, etc.). Update `category_status` accordingly.

### Sources (`source_status` keys — new header field)

Exactly: `OMC`, `OMX`, `OmO`, `Antigravity` (do **not** put `OMG` or `GROK_BUILD` in `source_status`; GROK_BUILD remains pin-only).

Each value: `bootstrapping` \| `complete`.

### Honesty rules (acceptance)

1. Every category and every source has an explicit `bootstrapping` \| `complete` mark.
2. While **any** category **or** source is `bootstrapping`, generators must not emit `%`, `✅`, or `✓`.
3. New rows default to maturity `catalogued` (or `configured` only when real config paths exist under this repo). Never invent `live_verified`.
4. Generated files carry `<!-- GENERATED by scripts/generate_parity_docs.py — do not edit by hand -->` and pass `--check`.
5. `#78` remains open for #78-C; do not close the issue from this PR.

### Minimum capability coverage (seed IDs — implementer may add more if discovered during cataloguing; do not delete these)

**OMC-shaped (upstream.source = OMC):**
- `omc.cli.session_surfaces` — CLI vs in-session surfaces
- `omc.agents.catalog_routing` — agent catalog / tier routing
- `omc.skills.catalog_aliases` — skill catalog / aliases / pipelines
- `omc.team.worktrees_mailbox` — Team / worktrees / mailbox (alias or sibling of `team.plane_v3` as needed)
- `omc.hooks.lifecycle` — hooks / lifecycle
- `omc.tools.lsp_ast` — LSP / AST tools
- `omc.session.search_replay` — session search / friction / replay / observatory
- `omc.memory.wiki_hud_notify` — memory / wiki / notifications / HUD
- `omc.goal.ralph_autopilot_ultra` — goal / Ralph / autopilot / UltraQA / Ultragoal
- `omc.quality.visual_release` — visual verdict / release / self-improve / project-session-manager

**OMX-shaped:**
- `omx.launch.worktree_tmux_hud`
- `omx.workflow.deep_interview_ralplan`
- `omx.research.modes`
- `omx.team.worker_mailbox_question`
- `omx.agents.reviewer_product_catalog`
- `omx.goal.stop_lock_recovery`
- `omx.plugin.setup_update_migrate`
- `omx.quality.visual_modes`

**OmO-shaped:**
- `omo.agents.discipline_routing`
- `omo.rules.intent_gate`
- `omo.agents.background`
- `omo.team.hyperplan_security`
- `omo.goal.todo_continuation`
- `omo.edit.hash_anchored`
- `omo.tools.lsp_ast_codegraph_mcp`
- `omo.quality.comment_hygiene`
- `omo.ulw.ultrawork_loop`
- `omo.compat.tmux_plugin`

**Antigravity-shaped:**
- keep `antigravity.provider.adapter`
- `antigravity.headless.structured_execution`
- `antigravity.agents.markdown_custom`
- `antigravity.skills.hooks_subagents_plugins_mcp`
- `antigravity.jobs.background_tasks`
- `antigravity.runtime.model_effort_mode_perms`
- `antigravity.session.history_resume`
- `antigravity.platform.version_matrix`

**Already seeded (retain):** `jobs.durable_background`, `team.plane_v3`, `parity.inventory.governance`.

Classification guidance: prefer `omg_native` / `antigravity_native` / `host_owned` / `host_impossible` / `optional_unclaimed` / `excluded` honestly. Use `faithful` only when behavior+evidence boundary is real. Use `alias` only with `alias_of` pointing at a canonical row.

`omg_paths` for claimable classes must be existing repo paths (strict gate). For pure-upstream catalogue rows not yet mapped, prefer `optional_unclaimed` or `host_owned` with empty/minimal `omg_paths`, **or** `omg_native` + real paths when OMG already has a substitute (e.g. `omg_cli/autopilot.py`).

---

### Task 1: Branch + failing `source_status` / taxonomy tests

**Files:**
- Create branch: `feat/parity-78b-full-inventory` from latest `main`
- Modify: `tests/test_parity_inventory_v2.py`
- Modify: `omg_cli/contracts/parity_schema.py`
- Modify: `docs/parity/schema-v2.md`

**Step 1: Write the failing tests** (exact names):

```python
def test_source_status_required_for_upstream_inventory_sources() -> None:
    ...


def test_source_status_rejects_unknown_or_omg_keys() -> None:
    ...


def test_claims_forbidden_when_any_source_bootstrapping() -> None:
    ...


def test_claims_forbidden_when_any_category_bootstrapping() -> None:
    ...


def test_required_category_taxonomy_present_in_canonical_inventory() -> None:
    """Canonical docs/parity/omg-parity.json must declare the #78-B category set."""
    ...
```

Also extend `_base_v2_inventory` fixture helper to include a valid `source_status` map so older Task-1-era tests keep compiling once schema requires it.

**Step 2: Run to verify fail**

```bash
PYTHONPATH=. .venv/bin/python -m pytest -q \
  tests/test_parity_inventory_v2.py::test_source_status_required_for_upstream_inventory_sources \
  tests/test_parity_inventory_v2.py::test_source_status_rejects_unknown_or_omg_keys \
  tests/test_parity_inventory_v2.py::test_claims_forbidden_when_any_source_bootstrapping \
  tests/test_parity_inventory_v2.py::test_claims_forbidden_when_any_category_bootstrapping \
  tests/test_parity_inventory_v2.py::test_required_category_taxonomy_present_in_canonical_inventory
```

Expected: FAIL (missing field / missing categories).

**Step 3: Minimal schema implementation**

- Add `SOURCE_STATUS_IDS = ("OMC", "OMX", "OmO", "Antigravity")`
- Add `PARITY_CATEGORY_TAXONOMY` frozenset/tuple of the 13 category keys above
- Require `source_status` in v2 `require_exact_keys`
- Validate each source status ∈ `CATEGORY_STATUS_VALUES`; exact key set == `SOURCE_STATUS_IDS`
- Update `inventory_is_complete` / `inventory_completion_claims_allowed` to also require every `source_status` value == `complete`
- Canonical inventory may still be bootstrapping; taxonomy check only asserts keys exist (values may be bootstrapping)
- Document in `docs/parity/schema-v2.md`

**Step 4: Tests pass** for the five names above (canonical taxonomy test may still fail until Task 4+ seeds categories — if so, temporarily assert taxonomy constant only in unit test, and move canonical-file assertion to Task 4). Prefer: Task 1 unit-tests the schema constant + validator; Task 4 owns the canonical JSON assertion.

Recommended split:
- Task 1: `test_required_category_taxonomy_constant_matches_issue_78b` (constant only)
- Task 4+: `test_required_category_taxonomy_present_in_canonical_inventory`

**Step 5: Commit**

```bash
git add omg_cli/contracts/parity_schema.py docs/parity/schema-v2.md tests/test_parity_inventory_v2.py
git commit -m "$(cat <<'EOF'
feat(parity): require source_status and category taxonomy

EOF
)"
```

---

### Task 2: Generator — per-source matrices + README summary + no overclaim

**Files:**
- Modify: `scripts/generate_parity_docs.py`
- Modify: `tests/test_parity_generation.py`
- Create (generated later): `docs/parity/MATRIX-OMC.md`, `MATRIX-OMX.md`, `MATRIX-OmO.md`, `MATRIX-Antigravity.md`
- Create (generated): `docs/parity/SUMMARY.md` (English README summary fragment)

**Step 1: Failing tests**

```python
def test_generated_per_source_matrices_are_current() -> None:
    ...


def test_generated_summary_omits_percent_while_bootstrapping() -> None:
    ...


def test_generate_check_covers_all_parity_artifacts() -> None:
    ...
```

**Step 2:** `pytest tests/test_parity_generation.py::test_generated_per_source_matrices_are_current -q` → FAIL

**Step 3: Implement renderers**

Extend `generate_parity_docs.py`:

- `render_source_matrix(inventory, source: str) -> str` — filter `capabilities` where `upstream.source == source`; include category/status/maturity/marker/gap; banner when bootstrapping
- `render_summary(inventory) -> str` — short counts by category/source/maturity; **no** parity % while incomplete; link to FEATURE-MATRIX / GAPS / per-source pages
- `GENERATED_PATHS` list used by both write and `--check`
- Keep FEATURE-MATRIX + GAPS behavior; harden: if any category **or** source bootstrapping → reject `%`/`✅`/`✓` in **all** rendered texts

**Step 4:** Tests pass (may write empty/minimal matrices until inventory expands — that's OK if generator runs).

**Step 5: Commit**

```bash
git add scripts/generate_parity_docs.py tests/test_parity_generation.py \
  docs/parity/MATRIX-*.md docs/parity/SUMMARY.md docs/parity/FEATURE-MATRIX.md docs/parity/GAPS.md
git commit -m "$(cat <<'EOF'
feat(parity): generate per-source matrices and summary

EOF
)"
```

---

### Task 3: Translations + historical banners

**Files:**
- Modify: `scripts/generate_parity_docs.py` (or small helper module `omg_cli/parity_docs.py` if file grows too large — prefer keep in script unless reuse needed)
- Create generated: `docs/parity/SUMMARY.zh.md`, `docs/parity/SUMMARY.zh-TW.md`
- Modify: `docs/research/core-parity-matrix-2026-07-20.md`
- Modify: `docs/research/omc-parity-council/README.md` (and optionally `STATUS.md`)
- Create: `tests/test_parity_historical_banner.py`

**Step 1: Failing tests**

```python
def test_parity_summary_translations_are_current() -> None:
    ...


def test_historical_parity_docs_carry_non_authoritative_banner() -> None:
    ...
```

Banner text (exact, English; translations may wrap):

```markdown
> **HISTORICAL / NON-AUTHORITATIVE.** This document predates the v2 parity inventory.
> Current claimability truth: [`docs/parity/omg-parity.json`](../parity/omg-parity.json)
> and generated [`docs/parity/FEATURE-MATRIX.md`](../parity/FEATURE-MATRIX.md).
```

(Adjust relative links per file depth; council README uses `../../parity/...`.)

Translations: generate concise zh / zh-TW SUMMARY pages from inventory counts + fixed gloss strings in the generator (do **not** hand-maintain tables). Full FEATURE-MATRIX translation is optional YAGNI — SUMMARY + EN matrix is enough for #78-B.

**Step 2:** pytest → FAIL

**Step 3:** Implement banner applicator (idempotent; detect existing `HISTORICAL / NON-AUTHORITATIVE` and skip duplicate) + translation renderers + `--check` coverage.

**Step 4:** Tests pass.

**Step 5: Commit**

```bash
git add scripts/generate_parity_docs.py tests/test_parity_historical_banner.py \
  tests/test_parity_generation.py docs/parity/SUMMARY.zh.md docs/parity/SUMMARY.zh-TW.md \
  docs/research/core-parity-matrix-2026-07-20.md \
  docs/research/omc-parity-council/README.md
git commit -m "$(cat <<'EOF'
docs(parity): add summary translations and historical banners

EOF
)"
```

---

### Task 4: Seed full category_status + OMC capability rows

**Files:**
- Modify: `docs/parity/omg-parity.json`
- Modify: `tests/test_parity_inventory_v2.py` (canonical taxonomy presence)
- Modify: `docs/parity/README.md` (point at SUMMARY + taxonomy; keep short)

**Step 1: Failing test**

```python
def test_required_category_taxonomy_present_in_canonical_inventory() -> None:
    inv = load_json_object(ROOT / "docs/parity/omg-parity.json")
    assert set(inv["category_status"]) >= set(PARITY_CATEGORY_TAXONOMY)


def test_omc_inventory_rows_cover_issue_78_minimum_ids() -> None:
    ...
```

**Step 2:** pytest → FAIL

**Step 3:** Expand `category_status` to full taxonomy (all `bootstrapping` initially, or mark a category `complete` only after its minimum IDs exist). Add all OMC minimum capability rows with:

- `maturity.*. = "catalogued"` (or `configured` only with real evidence paths)
- honest `gap` text linking #67–#78 where relevant
- `upstream.revision` matching pin
- `source_paths` as best-effort relative paths from known pin docs (README.md / skill paths are OK; do not require upstream_roots in CI)

Also add `source_status` with all four sources `bootstrapping` until Task 7 finishes.

**Step 4:** `python3 scripts/check_parity_inventory.py --strict` + targeted pytest PASS.

**Step 5: Commit**

```bash
git add docs/parity/omg-parity.json docs/parity/README.md tests/test_parity_inventory_v2.py
git commit -m "$(cat <<'EOF'
feat(parity): seed OMC capability inventory rows

EOF
)"
```

---

### Task 5: Seed OMX + OmO capability rows

**Files:**
- Modify: `docs/parity/omg-parity.json`
- Modify: `tests/test_parity_inventory_v2.py`

**Step 1: Failing tests**

```python
def test_omx_inventory_rows_cover_issue_78_minimum_ids() -> None:
    ...


def test_omo_inventory_rows_cover_issue_78_minimum_ids() -> None:
    ...
```

**Step 2:** FAIL → **Step 3:** add OMX + OmO minimum IDs with honest classifications/gaps → **Step 4:** strict check PASS → **Step 5: Commit**

```bash
git add docs/parity/omg-parity.json tests/test_parity_inventory_v2.py
git commit -m "$(cat <<'EOF'
feat(parity): seed OMX and OmO capability inventory rows

EOF
)"
```

---

### Task 6: Seed Antigravity rows + gaps refresh

**Files:**
- Modify: `docs/parity/omg-parity.json`
- Modify: `tests/test_parity_inventory_v2.py`
- Modify: `tests/test_parity_generation.py` (open P0 still includes #67–#69/#78)

**Step 1: Failing test**

```python
def test_antigravity_inventory_rows_cover_issue_78_minimum_ids() -> None:
    ...


def test_no_capability_row_is_fake_live_verified() -> None:
    inv = validate_parity_inventory(load_json_object(INVENTORY), repo_root=ROOT)
    for row in inv["capabilities"]:
        for level in row["maturity"].values():
            assert level != "live_verified"
```

(Allow `healthy` only if real `healthy_evidence` paths already exist — prefer none in #78-B.)

**Step 2–4:** Add Antigravity minimum IDs; refresh `gaps[]`:

- Keep open P0s for #67, #68, #69
- Split or reword `gap.parity.governance.remaining` → summary states **#78-B inventory landed; #78-C freshness/refresh remains**
- Add lower-priority gaps for large unmapped host surfaces as needed (`P1`/`P2`), still honest

Optionally mark individual `source_status` / `category_status` entries `complete` **only** when that source/category's minimum ID set is present. If any remain unfinished, leave `bootstrapping`. Prefer: after Tasks 4–6, mark all four sources `complete` (catalogue pass done) while leaving `inventory_status` as `bootstrapping` if any category still bootstrapping — **or** mark catalogue-complete categories `complete` and keep governance/platform `bootstrapping` until #78-C. Do **not** set global `inventory_status: complete` unless every category **and** source is complete **and** you accept claim markers unlocking for healthy+ rows.

Recommended honesty for #78-B exit state:

```json
"inventory_status": "bootstrapping",
"source_status": {
  "OMC": "complete",
  "OMX": "complete",
  "OmO": "complete",
  "Antigravity": "complete"
},
"category_status": {
  "...catalogue categories...": "complete",
  "parity_governance": "bootstrapping",
  "platform_live_evidence": "bootstrapping"
}
```

This satisfies “all category/source marked bootstrapping|complete”, keeps %/✅ suppressed, and leaves room for #78-C.

**Step 5: Commit**

```bash
git add docs/parity/omg-parity.json tests/test_parity_inventory_v2.py tests/test_parity_generation.py
git commit -m "$(cat <<'EOF'
feat(parity): seed Antigravity rows and refresh open gaps

EOF
)"
```

---

### Task 7: Regenerate all docs + README index links + packaging sanity

**Files:**
- Regenerate via script: all `docs/parity/FEATURE-MATRIX.md`, `GAPS.md`, `SUMMARY*.md`, `MATRIX-*.md`
- Modify: `docs/parity/README.md` — link SUMMARY (EN/zh/zh-TW), matrices, schema-v2, honesty rules
- Modify: root `README.md` research table — point historical matrix at banner + canonical inventory (no new checkmarks)
- Modify: `docs/readme/README.zh.md`, `docs/readme/README.zh-TW.md` only if they mirror the research table (keep claim language honest)
- Tests: existing packaging / `test_packaged_install_contains_canonical_parity_inventory` still green

**Step 1: Failing test (if needed)**

```python
def test_parity_readme_links_generated_artifacts() -> None:
    text = (ROOT / "docs/parity/README.md").read_text(encoding="utf-8")
    for needle in (
        "FEATURE-MATRIX.md",
        "GAPS.md",
        "SUMMARY.md",
        "MATRIX-OMC.md",
        "omg-parity.json",
    ):
        assert needle in text
```

**Step 2–4:**

```bash
python3 scripts/generate_parity_docs.py
python3 scripts/generate_parity_docs.py --check
python3 scripts/check_parity_inventory.py --strict
./bin/omg parity check --strict --json
./bin/omg parity gaps --priority P0 --json
PYTHONPATH=. .venv/bin/python -m pytest -q -m "not live" \
  tests/test_parity_inventory.py \
  tests/test_parity_inventory_v2.py \
  tests/test_parity_generation.py \
  tests/test_parity_check.py \
  tests/test_parity_historical_banner.py \
  tests/test_command_family_inspect.py
```

Expected: all PASS; generated artifacts contain bootstrapping honesty; no `%`/`✅`/`✓`.

**Step 5: Commit**

```bash
git add docs/parity docs/research/core-parity-matrix-2026-07-20.md \
  docs/research/omc-parity-council/README.md README.md \
  docs/readme/README.zh.md docs/readme/README.zh-TW.md \
  tests/test_parity_generation.py
git commit -m "$(cat <<'EOF'
docs(parity): regenerate full inventory artifacts and index links

EOF
)"
```

---

### Task 8: CHANGELOG + PR (do not close #78)

**Files:**
- Modify: `CHANGELOG.md` under Unreleased
- Open PR from `feat/parity-78b-full-inventory`

**CHANGELOG bullets (suggested):**
- Expand parity inventory to full OMC/OMX/OmO/Antigravity catalogue rows
- Add `source_status`, per-source matrices, SUMMARY (+ zh/zh-TW), historical banners
- #78 remains open for #78-C (refresh / live-evidence / release claim gate)

**PR body must state:**
- Partial of #78 (#78-B only)
- Does **not** close #78
- Does **not** claim live_verified parity
- Wait CI + Codex + GPT Pro; merge only if 「無 P2 以上問題」

If GitHub issue #78 is already CLOSED, leave a comment that #78-B lands under the still-open governance gap and #78-C remains; reopen only if the maintainer wants the issue state to match reality — do **not** silently treat CLOSED as “all slices done”.

**Commit:**

```bash
git add CHANGELOG.md
git commit -m "$(cat <<'EOF'
docs(changelog): note #78-B full upstream inventory

EOF
)"
```

---

## Acceptance checklist (copy into PR)

- [ ] `source_status` present for OMC/OMX/OmO/Antigravity; every category/source ∈ {`bootstrapping`,`complete`}
- [ ] Minimum capability IDs for all four upstreams present in `docs/parity/omg-parity.json`
- [ ] No row uses fake `live_verified` (and no unjustified `healthy`)
- [ ] Generated: FEATURE-MATRIX, GAPS, SUMMARY(+zh/zh-TW), MATRIX-{OMC,OMX,OmO,Antigravity}
- [ ] `python3 scripts/generate_parity_docs.py --check` exit 0
- [ ] `python3 scripts/check_parity_inventory.py --strict` exit 0
- [ ] Incomplete inventory emits no `%` / `✅` / `✓`
- [ ] Historical research docs carry NON-AUTHORITATIVE banner linking to canonical inventory
- [ ] #78 not closed; #78-C explicitly still open in gaps
- [ ] Unit gate green: parity + inspect tests above

---

## Execution handoff

Plan complete and saved to `docs/plans/2026-08-05-parity-full-inventory-78b.md`.

**Required execution mode:** superpowers:subagent-driven-development — fresh subagent per Task 1…8 on branch `feat/parity-78b-full-inventory`, code review between tasks.

**Two options for the parent session:**

1. **Subagent-Driven (this session)** — dispatch fresh subagent per task, review between tasks
2. **Parallel Session (separate)** — open a new session with executing-plans in a worktree

Do not implement from the plan-only branch `plan/parity-78b`; cut `feat/parity-78b-full-inventory` from latest `main` when starting Task 1.
