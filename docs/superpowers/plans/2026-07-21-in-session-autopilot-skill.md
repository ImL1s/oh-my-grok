# In-Session Autopilot Skill Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make **in-session** `omg-autopilot` the primary path power users expect: thick Grok skill playbook that drives the existing CLI phase machine via tools, with correct keyword routing — without claiming OMC Stop-hard-pin continuation.

**Architecture:** CLI (`omg_cli/autopilot.py`) remains single-writer for phases/verified. Session skill is the **operator playbook** (like OMC skill-bodies/autopilot but mapped to Grok `spawn_subagent` + `run_terminal_command` → `omg …`). Persistence across chat turns relies on CLI state under `.omg/state/` and re-invoking the skill / user saying “continue”, **not** Stop hooks.

**Tech Stack:** Markdown skills, existing `omg autopilot` / interview / ralplan / review / qa / accept CLI, Grok-native agents.

**Research summary (2026-07-21):**

| Surface | OMC | OMG today |
|---------|-----|-----------|
| Skill size | ~225-line full body (+ compact shim) | **33-line** stub |
| Authority | In-session agents + state | **CLI FSM** + thin skill |
| Continue | Stop / persistent modes | **No Stop hard-pin** (host) |
| Router | Rich triggers (“build me”, “full auto”) | **`omg-using` omits autopilot** |
| Phases | 0 expand → 1 plan → 2 exec → 3 QA → 4 validate → 5 cleanup | CLI: interview → ralplan → implement → review → qa → acceptance → verified |

**Out of scope:** Implementing Stop continuation; full OMC team/tmux workers; changing LEGAL_TRANSITIONS semantics (document only).

---

## File map

| File | Change |
|------|--------|
| `skills/omg-autopilot/SKILL.md` | Replace with full in-session playbook |
| `skills/omg-using/SKILL.md` | Route autopilot keywords; mention persistence honesty |
| `README.md` | Short “In-session autopilot” note under skills / mental model |
| `tests/test_skill_inventory.py` | **Create** — assert skill files exist + required sections/keywords |

---

### Task 1: Skill inventory test (TDD)

**Files:**
- Create: `tests/test_skill_inventory.py`

- [ ] **Step 1: Write failing tests**

```python
"""Inventory checks for plugin skills (session playbooks)."""
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SKILLS = REPO / "skills"


def _skill(name: str) -> str:
    return (SKILLS / name / "SKILL.md").read_text(encoding="utf-8")


def test_required_skills_exist():
    for name in (
        "omg-autopilot",
        "omg-using",
        "omg-ralph",
        "omg-ultrawork",
        "omg-ralplan",
    ):
        assert (SKILLS / name / "SKILL.md").is_file(), name


def test_omg_autopilot_is_session_playbook_not_stub():
    text = _skill("omg-autopilot")
    # frontmatter
    assert "name: omg-autopilot" in text
    assert "description:" in text
    # body must be substantial (was ~33 lines)
    assert text.count("\n") >= 120, "autopilot skill still too thin for in-session use"
    # required sections / contracts
    for needle in (
        "HARD RULES",
        "Use when",
        "Do not use when",
        "interview",
        "ralplan",
        "implement",
        "review",
        "qa",
        "acceptance",
        "spawn_subagent",
        "capability_mode",
        "omg autopilot start",
        "omg autopilot transition",
        "omg accept",
        "omg autopilot complete",
        "verified",
        "Stop",  # honesty: no Stop hard-pin
    ):
        assert needle in text, f"missing {needle!r}"


def test_omg_using_routes_autopilot():
    text = _skill("omg-using")
    assert "omg-autopilot" in text
    assert "autopilot" in text.lower()
    # trigger words power users type in session
    for needle in ("build me", "full auto", "autonomous"):
        assert needle in text.lower() or "autopilot" in text
```

- [ ] **Step 2: Run tests — expect FAIL**

```bash
.venv/bin/python -m pytest tests/test_skill_inventory.py -v --tb=short
```

Expected: `test_omg_autopilot_is_session_playbook_not_stub` FAIL (line count / missing sections); `test_omg_using_routes_autopilot` FAIL.

- [ ] **Step 3: Commit test only**

```bash
git add tests/test_skill_inventory.py
git commit -m "test: skill inventory gates for in-session autopilot playbook"
```

---

### Task 2: Full `skills/omg-autopilot/SKILL.md`

**Files:**
- Modify: `skills/omg-autopilot/SKILL.md` (replace entire file)

- [ ] **Step 1: Write the full skill body**

Use this complete content (engineers: write the file as-is):

```markdown
---
name: omg-autopilot
description: >
  In-session end-to-end coordinator for oh-my-grok. Use when the user says
  autopilot, auto pilot, full auto, autonomous, build me, create me, make me,
  handle it all, or wants idea→working code with interview/plan/implement/review/QA/accept.
  CLI owns phase state and verified; this skill is the session playbook.
---

# omg-autopilot — In-session end-to-end coordinator

You are running **inside a Grok Build session**. Autopilot means **you** drive the
strict CLI phase machine and Grok-native workers until acceptance — not that the
host Stop-hook forces the chat to continue (it cannot).

**Authority split**

| Concern | Owner |
|---------|--------|
| Phase legality, stamps, `verified` | **`omg` CLI only** |
| Spec / plan / code proposals | Session + `spawn_subagent` |
| Outer “don’t stop” across many turns | Re-invoke this skill / user “continue” / optional `omg ralph` outer loop |

## HARD RULES (non-negotiable)

1. Fan-out **only** via Grok `spawn_subagent` (depth = 1; children must **not** spawn).
2. **Always** set `capability_mode` on spawn:
   - implementers (`omg-executor`, write `general-purpose`): **`read-write`** (no Execute)
   - critic / verifier / explore / plan: **`read-only`**
3. If spawn is **DENIED** for capability_mode: **RETRY IMMEDIATELY** same turn with the correct mode. Do **not** abandon multi-agent; do **not** solo-fallback after one deny.
4. **Never** invoke `claude` / `codex` / `omc team` / `agy` / `cursor-agent` as default workers.
5. Grok tool names: `read_file`, `search_replace`, `run_terminal_command`, `spawn_subagent`, `grep`, `list_dir`.
6. **Never** write `passes` / `verified` under `.omg/state/`. Only CLI after acceptance.
7. **No Stop hard-pin:** PreToolUse is fail-open soft-guard. Do not claim OMC-style “chat cannot end until done.”
8. Cancel with `omg cancel` — never self-matching `pkill -f`.

## Use when

- User says: `autopilot`, `auto pilot`, `full auto`, `autonomous`, `build me`, `create me`, `make me`, `handle it all`, `end to end`, `from idea to working code`.
- Multi-phase work: requirements → plan consensus → implement → review → QA → accept.
- User wants hands-off orchestration **inside this session** and will re-prompt “continue” if the turn ends.

## Do not use when

- Single tiny fix → work directly or `omg-ralph` one story.
- Plan-only / critique-only → `omg-ralplan`.
- Parallel burst only → `omg-ultrawork`.
- Abort → `omg-cancel`.
- User wants brainstorm without shipping → answer conversationally; do not start autopilot state.

## Persistence honesty (read this)

| Want | How on Grok / OMG |
|------|-------------------|
| Strict phases + destination gates | This skill + `omg autopilot *` |
| Outer retry until verified | `omg ralph "…"` **or** user re-invokes this skill after each turn |
| Host-forced continuation on Stop | **Not available** — see `docs/research/stop-continuation/` |

If the session ends mid-phase: run `omg autopilot status --run RUN` and resume the playbook from the current phase.

## CLI phase machine (normative)

```text
interview → ralplan → implement → review → (rework) → qa → acceptance → verified
```

Illegal transitions fail closed. Destination gates (CLI-enforced):

| Enter phase | Required evidence / stamp |
|-------------|---------------------------|
| `ralplan` from `interview` | `interview_complete: true` |
| `implement` | `consensus: true` |
| `qa` | CLI `stages/structured_review.json` clean |
| `acceptance` | CLI `stages/ultraqa.json` status clean |
| `verified` | **Only** `omg autopilot complete` after same-process accept — never `transition … verified` |

## Session playbook

### 0. Bootstrap

```bash
omg doctor          # fix FAILs first
omg setup           # if .omg/ missing
omg autopilot status --run RUN   # if resuming
```

If no run yet:

```bash
omg autopilot start "GOAL TEXT"
# skip interview only when requirements already closed:
# omg autopilot start "GOAL" --skip-interview
```

Record `run_id` from output. Prefer `run_terminal_command` for all `omg` invocations.

### 1. Phase `interview` (unless skip)

- Follow **omg-deep-interview** / CLI:
  - `omg interview start "…"` or continue with printed `resume_command`
  - `omg interview answer …` / `pressure-pass` / `close`
- When complete:

```bash
omg autopilot transition --run RUN --phase ralplan \
  --evidence-json '{"interview_complete":true}' \
  --reason "interview closed"
```

### 2. Phase `ralplan`

- Follow **omg-ralplan** playbook **without product code**:
  - draft plan under run `stages/` + `.omg/artifacts/`
  - `spawn_subagent` critic **read-only**
  - revise
  - `spawn_subagent` verifier **read-only** → stage artifact must contain whole-word **APPROVE**
- Prefer CLI when available: `omg ralplan "…" --run RUN` if the host wires the same run; otherwise produce stage artifacts then transition with evidence.

```bash
omg autopilot transition --run RUN --phase implement \
  --evidence-json '{"consensus":true}' \
  --reason "ralplan APPROVE"
```

### 3. Phase `implement`

- Decompose into stories / independent slices.
- Prefer **omg-ultrawork** patterns for parallel slices; **omg-ralph** one-story discipline for sequential must-finish.
- Spawn implementers with `capability_mode=read-write`; worktrees for write-heavy work.
- Write notes under `.omg/artifacts/`. Do **not** claim verified.

```bash
omg autopilot transition --run RUN --phase review --reason "implementation ready for review"
```

### 4. Phase `review`

- Prefer **omg-dual-review** / native critic→verifier (read-only).
- Or CLI: `omg review --run RUN --diff-text "…" --code-reviewer-json '…' --architect-json '…'`
- On REQUEST CHANGES:

```bash
omg autopilot transition --run RUN --phase rework --reason "review findings"
# fix, then:
omg autopilot transition --run RUN --phase review --reason "rework done"
```

When CLI stamps review clean:

```bash
omg autopilot transition --run RUN --phase qa --reason "structured review clean"
```

### 5. Phase `qa`

- Follow **omg-ultraqa**:

```bash
omg qa freeze --run RUN --scenarios-json '[{"id":"t1","command":"python3 -m pytest -q -m not live"}]'
omg qa run --run RUN
omg qa status --run RUN
```

- QA clean **≠** verified.

```bash
omg autopilot transition --run RUN --phase acceptance --reason "ultraqa clean"
```

### 6. Phase `acceptance` → `verified`

```bash
omg accept --run RUN --yes
# same process / same shell turn chain when possible:
omg autopilot complete --run RUN
omg autopilot status --run RUN
```

Only then report success with evidence (commands + outputs).

### 7. Blocked / cancel

```bash
omg autopilot transition --run RUN --phase blocked --reason "…"
omg cancel
```

## Capability cheat sheet

| Role | `capability_mode` | Notes |
|------|-------------------|--------|
| Implementer | `read-write` | No shell/Execute |
| Critic / verifier / explore | `read-only` | No product edits |
| Shell / tests for verified | **CLI** `omg accept` / `omg qa` | Never child self-verify run |

## Anti-patterns

- Thin “done” prose without CLI stamps
- `transition --phase verified`
- Skipping interview/ralplan gates by lying in evidence-json
- Self-approve after implement (skip dual-review)
- Infinite self-loop without CLI status (burn tokens; prefer status + continue)
- External agent CLIs as workers
- Claiming Stop hooks keep the session alive

## Related skills

- `omg-using` — router
- `omg-deep-interview`, `omg-ralplan`, `omg-ultrawork`, `omg-ralph`
- `omg-dual-review`, `omg-ultraqa`, `omg-cancel`
- Security: `docs/security-model.md`

## CLI quick reference

```bash
omg autopilot start "goal"
omg autopilot start "goal" --skip-interview
omg autopilot transition --run RUN --phase PHASE --evidence-json '{…}' --reason "…"
omg autopilot status --run RUN
omg accept --run RUN --yes
omg autopilot complete --run RUN
omg cancel
```
```

- [ ] **Step 2: Re-run inventory tests**

```bash
.venv/bin/python -m pytest tests/test_skill_inventory.py::test_omg_autopilot_is_session_playbook_not_stub -v
```

Expected: PASS (using still fails until Task 3)

- [ ] **Step 3: Commit**

```bash
git add skills/omg-autopilot/SKILL.md
git commit -m "feat(skills): thick in-session omg-autopilot playbook"
```

---

### Task 3: Route autopilot from `omg-using`

**Files:**
- Modify: `skills/omg-using/SKILL.md`

- [ ] **Step 1: Extend keyword table and bootstrap list**

In the “When to load which skill” table, **insert** a row (after cancel priority note, before or after ralph):

```markdown
| `autopilot`, `auto pilot`, `full auto`, `autonomous`, `build me`, `create me`, `make me`, `handle it all`, end-to-end lifecycle | `omg-autopilot` | Session playbook driving CLI phases interview→…→verified |
```

Update priority sentence to:

```markdown
If multiple keywords appear, prefer: **cancel** > **ralplan** (planning not done) > **autopilot** (full lifecycle) > **ralph** (durable) > **ulw** (parallel one-shot).
```

In “Bootstrap steps” item 1, include `skills/omg-autopilot`.

In “Persistence model” table, add:

```markdown
| Full phase coordinator in-session | **`omg-autopilot` skill** + `omg autopilot *` CLI |
```

- [ ] **Step 2: Run full inventory tests**

```bash
.venv/bin/python -m pytest tests/test_skill_inventory.py -v
```

Expected: all PASS

- [ ] **Step 3: Commit**

```bash
git add skills/omg-using/SKILL.md
git commit -m "feat(skills): route session autopilot keywords in omg-using"
```

---

### Task 4: README + verify + push

**Files:**
- Modify: `README.md` (skills table / mental model — short)

- [ ] **Step 1: Add in-session note**

Near skills table (`omg-autopilot` row), ensure description says **In-session playbook** (CLI phase machine). Add one line under CLI vs skills:

```markdown
**In-session autopilot:** load skill `omg-autopilot` (triggers: autopilot / build me / full auto). Host cannot Stop-pin the chat; re-invoke skill or say “continue” and read `omg autopilot status`.
```

- [ ] **Step 2: Full hermetic tests**

```bash
.venv/bin/python -m pytest -q -m "not live" --tb=line
```

Expected: all green (inventory + prior suite)

- [ ] **Step 3: Commit + push**

```bash
git add README.md
git commit -m "docs: document in-session omg-autopilot usage"
git push origin HEAD
```

---

## Self-review

| Requirement | Task |
|-------------|------|
| Thick in-session playbook | Task 2 |
| Keyword routing | Task 3 |
| Honesty: no Stop hard-pin | Task 2 HARD RULES |
| CLI remains authority | Task 2 authority table |
| Regression gate | Task 1 inventory tests |
| User-facing docs | Task 4 |

No PyPI / madmax changes / autopilot.py LEGAL_TRANSITIONS changes.
