# Research-Driven P0 Verdict Hardening + In-Session Ultragoal Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close remaining **false-APPROVE** gaps called out in `omc_omx_mechanism_research.md` (R3), and make **session-facing ultragoal** usable like post-autopilot skills (R1/R4 lifestyle + durable completion path).

**Architecture:** Keep prose parser as legacy fallback but harden it (negation expansion + fence strip). Prefer JSON `verdict` field (already first-class). CLI remains sole writer for goal ledger and verified. Session skill playbook drives `omg goal *` without inventing host `/goal` (Grok has none).

**Tech Stack:** Python `omg_cli/verdict.py`, tests, Markdown skills.

**Research source:** `~/teamwork_projects/omc_omx_research/omc_omx_mechanism_research.md`  
**Copy/link note:** Summarize under `docs/research/omc-omx-mechanism-research-pointer.md` (path only, no huge copy).

**Out of scope this plan:** Full `omg resume` + RESUME.md injection (R2) — design pointer only; wiki/HUD/LSP (P2).

---

## Research → task mapping

| Research | This plan |
|----------|-----------|
| R3 can't/unable APPROVE + fence false green | Task 1–2 verdict.py + tests |
| R3 Exit Code Override | Already HAVE (`apply_stage_exit_codes`); lock with regression test only |
| R1 ultragoal / session | Task 3–4 thick skill + using router |
| R4 P0 resume | Task 5 pointer doc only |
| R2 don't-stop three pillars | Document in ultragoal skill honesty section |

**Probed gaps (2026-07-21 on tree):**

```text
"I can't APPROVE this plan.\n\nAPPROVE\n"     → APPROVE  # BAD
"Unable to APPROVE.\n\n## Verdict\nAPPROVE\n" → APPROVE  # BAD
"```\nAPPROVE\n```\n"                         → APPROVE  # BAD
apply_stage_exit_codes APPROVE+rc≠0           → FAILED  # GOOD
```

---

### Task 1: Failing tests for false-green

**Files:**
- Modify: `tests/test_verdict.py`

- [ ] **Step 1: Append tests**

```python
def test_cant_cannot_unable_negation_blocks_terminal_approve():
    # Negated language in body must neutralize a later terminal APPROVE line
    # (research R3: can't / unable / cannot)
    assert (
        parse_verdict("I can't APPROVE this plan.\n\nAPPROVE\n") != "APPROVE"
    )
    assert parse_verdict("Cannot APPROVE.\n\nVerdict: APPROVE\n") != "APPROVE"
    assert parse_verdict("Unable to APPROVE this.\n\nAPPROVE\n") != "APPROVE"
    assert (
        parse_verdict("We refuse to APPROVE.\n\n## Verdict\nAPPROVE\n")
        != "APPROVE"
    )


def test_fenced_approve_alone_is_not_acceptance():
    assert parse_verdict("```\nAPPROVE\n```\n") != "APPROVE"
    assert (
        parse_verdict("Example stub:\n```md\n## Verdict\nAPPROVE\n```\nNeeds work.\n")
        != "APPROVE"
    )
    # Real terminal outside fence still works
    assert (
        parse_verdict("See example:\n```\nAPPROVE\n```\n\nVerdict: APPROVE\n")
        == "APPROVE"
    )


def test_exit_code_override_law_documented():
    # Regression lock for dual_review apply path (research Exit Code Override Law)
    assert apply_stage_exit_codes("APPROVE", critic_rc=0, verifier_rc=1) == "FAILED"
    assert apply_stage_exit_codes("APPROVE", critic_rc=2, verifier_rc=0) == "FAILED"
```

- [ ] **Step 2: Run — expect FAIL on negation/fence cases**

```bash
.venv/bin/python -m pytest tests/test_verdict.py -v --tb=short
```

- [ ] **Step 3: Commit tests**

```bash
git add tests/test_verdict.py
git commit -m "test: lock R3 false-APPROVE cases (negation + fences)"
```

---

### Task 2: Harden `omg_cli/verdict.py`

**Files:**
- Modify: `omg_cli/verdict.py`

- [ ] **Step 1: Expand negation + strip fences**

Replace `_NEGATED_APPROVE_RE` with broader pattern:

```python
_NEGATED_APPROVE_RE = re.compile(
    r"(?i)"
    r"(?:"
    r"do\s+not|don'?t|does\s+not|did\s+not|"
    r"never|not|"
    r"can'?t|cannot|could\s+not|couldn'?t|"
    r"will\s+not|won'?t|would\s+not|wouldn'?t|"
    r"should\s+not|shouldn'?t|"
    r"unable\s+to|refuse\s+to|declin(?:e|es|ed|ing)\s+to|"
    r"not\s+(?:going\s+to|able\s+to)"
    r")\s+APPROVE"
    r"|APPROVE\s+(?:yet|lightly|blindly|to\s+be\s+helpful)"
)
```

Add fence stripper used **before** terminal APPROVE detection (and before negation strip on the same cleaned text):

```python
_FENCE_RE = re.compile(r"```[\w+-]*\n.*?```", re.DOTALL)


def _strip_fenced_blocks(text: str) -> str:
    return _FENCE_RE.sub("\n", text or "")
```

In `prose_has_terminal_approve`:

```python
def prose_has_terminal_approve(text: str) -> bool:
    if not text or not text.strip():
        return False
    if is_stub_artifact_text(text):
        return False
    body = _strip_fenced_blocks(text)
    cleaned = _NEGATED_APPROVE_RE.sub(" ", body)
    # If any negation of APPROVE remains as a policy statement in body,
    # require that terminal APPROVE is not the only signal — already cleaned.
    # Additional hard rule: if original body had a negation phrase, refuse
    # APPROVE unless JSON path already handled (prose path stays fail-closed
    # when *any* negation of APPROVE appears in unfenced body).
    if _NEGATED_APPROVE_RE.search(body):
        return False
    if not _TERMINAL_APPROVE_LINE_RE.search(cleaned):
        return False
    return True
```

**Rationale (strict):** Research wants fail-closed. If the unfenced body ever says "can't APPROVE", do not accept a later terminal APPROVE line in prose — force JSON verdict or REQUEST_CHANGES/FAILED. JSON path in `parse_verdict` still allows clean `{"verdict":"APPROVE"}`.

- [ ] **Step 2: Tests green**

```bash
.venv/bin/python -m pytest tests/test_verdict.py tests/test_ralplan.py tests/test_dual_review.py -q --tb=line
```

- [ ] **Step 3: Commit**

```bash
git add omg_cli/verdict.py tests/test_verdict.py
git commit -m "fix(verdict): fence-strip and expand APPROVE negation (R3)"
```

---

### Task 3: Skill inventory extensions (TDD)

**Files:**
- Modify: `tests/test_skill_inventory.py`

- [ ] **Step 1: Add ultragoal inventory tests**

```python
def test_required_skills_exist():
    for name in (
        "omg-autopilot",
        "omg-using",
        "omg-ralph",
        "omg-ultrawork",
        "omg-ralplan",
        "omg-ultragoal",  # add
    ):
        ...

def test_omg_ultragoal_is_session_playbook_not_stub():
    text = _skill("omg-ultragoal")
    assert "name: omg-ultragoal" in text
    assert text.count("\n") >= 100
    for needle in (
        "HARD RULES",
        "Use when",
        "Do not use when",
        "omg goal init",
        "checkpoint",
        "link-run",
        "verify",
        "spawn_subagent",
        "no host",  # honesty: no Claude/Codex /goal
        "/goal",
    ):
        assert needle.lower() in text.lower() or needle in text


def test_omg_using_routes_ultragoal():
    text = _skill("omg-using")
    assert "omg-ultragoal" in text
    low = text.lower()
    assert "ultragoal" in low or "omg goal" in low
```

- [ ] **Step 2: Run — FAIL until Task 4**

```bash
.venv/bin/python -m pytest tests/test_skill_inventory.py -v --tb=line
```

---

### Task 4: Thick `skills/omg-ultragoal/SKILL.md` + `omg-using` route

**Files:**
- Replace: `skills/omg-ultragoal/SKILL.md`
- Modify: `skills/omg-using/SKILL.md`
- Modify: `skills/omg-autopilot/SKILL.md` (short optional ultragoal hook section)
- Create: `docs/research/omc-omx-mechanism-research-pointer.md`

**Ultragoal skill requirements (must include):**

1. Frontmatter description with triggers: `ultragoal`, `goal ledger`, multi-story durable, resume goal.
2. HARD RULES (same as other omg skills + never write ledger by hand).
3. **Honesty block:** OMC/OMX bind host `/goal` or `get_goal`/`create_goal`; **Grok has no equivalent** — OMG uses **repo ledger only** (`.omg/ultragoal/`). Session continues by re-invoking skill + `omg goal status`.
4. Session playbook:
   - init stories JSON
   - for each ready story: start-story → implement (spawn) → write evidence file → checkpoint → complete-story
   - link-run to CLI-verified run before `omg goal verify`
5. Full CLI reference (existing commands).
6. Anti-patterns: fake verified, mid-chain edit ledger, claim host `/goal`.

**omg-using:** add table row + priority: cancel > ralplan > autopilot > **ultragoal** (durable multi-story) > ralph > ulw.

**autopilot skill:** add short section "Optional durable multi-story" pointing to `omg-ultragoal` when >1 story needs ledger.

**pointer doc:**

```markdown
# OMC/OMX mechanism research (pointer)

Canonical deep research (external teamwork artifact):

`~/teamwork_projects/omc_omx_research/omc_omx_mechanism_research.md`

OMG actions derived from R3/R4 P0:
- Verdict false-green hardening → `omg_cli/verdict.py` (this plan)
- Session ultragoal playbook → `skills/omg-ultragoal`
- Resume/SessionStart (R2) → deferred to 0.3.x plan
```

- [ ] **Step 1–2:** Write files, run inventory tests green
- [ ] **Step 3:** Commit

```bash
git commit -m "feat(skills): in-session ultragoal playbook + research pointer"
```

---

### Task 5: README note + full verify + push

**Files:**
- Modify: `README.md` — skills row for ultragoal; one line under goal about no host `/goal`

```bash
.venv/bin/python -m pytest -q -m "not live" --tb=line
git push origin main
```

---

## Self-review

| Research item | Covered? |
|---------------|----------|
| R3 fence + expanded negation | Task 1–2 |
| R3 exit code law | Locked in tests (already implemented) |
| R1 ultragoal session | Task 3–4 |
| R2 resume | Pointer only (honest defer) |
| No fake host /goal | Ultragoal honesty section |

No placeholders. No LEGAL_TRANSITIONS change.
