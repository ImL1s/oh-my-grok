# Autopilot Hardening Round 3 (fingerprint entrypoints) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development.

**Goal:** Close remaining Codex P2 on PR #84: include `scripts/` and `bin/` in the implement-workspace fingerprint so install/CLI surface edits register as work and bind QA/review freshness.

**Baseline:** `fix/autopilot-pro-hardening-r1` after Round 2 (`eb200e2`).

**Architecture:** Extend `_IMPLEMENT_FINGERPRINT_ROOTS` only (do not change `qa.product_hash`). QA/review already recheck `_implement_workspace_fingerprint`, so expanding the tuple closes both “entrypoints omitted” and “QA full surface” Codex findings for those paths.

**Tech Stack:** Python 3.11+, pytest, existing `omg_cli/autopilot.py`.

---

### Task R3-1: Include scripts/ and bin/ in implement fingerprint

**Files:**
- Modify: `omg_cli/autopilot.py` (`_IMPLEMENT_FINGERPRINT_ROOTS`)
- Test: `tests/test_autopilot.py`

**Step 1: Write the failing test**

```python
def test_implement_fingerprint_includes_scripts_and_bin(tmp_path):
    from omg_cli.autopilot import _implement_workspace_fingerprint

    (tmp_path / "omg_cli").mkdir()
    (tmp_path / "omg_cli" / "x.py").write_text("a\n", encoding="utf-8")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "bin").mkdir()
    base = _implement_workspace_fingerprint(tmp_path)
    (tmp_path / "scripts" / "install-plugin.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    assert _implement_workspace_fingerprint(tmp_path) != base
    mid = _implement_workspace_fingerprint(tmp_path)
    (tmp_path / "bin" / "omg").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    assert _implement_workspace_fingerprint(tmp_path) != mid
```

**Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. .venv/bin/python -m pytest -q tests/test_autopilot.py::test_implement_fingerprint_includes_scripts_and_bin`
Expected: FAIL (hash unchanged)

**Step 3: Minimal implementation**

Add `"scripts"` and `"bin"` to `_IMPLEMENT_FINGERPRINT_ROOTS`. Update the docstring/comment that lists curated surfaces.

**Step 4: Run test to verify it passes**

Same pytest command → PASS. Also run related implement-gate tests.

**Step 5: Commit**

```bash
git add omg_cli/autopilot.py tests/test_autopilot.py
git commit -m "fix(autopilot): include scripts/ and bin/ in implement fingerprint"
```

### Task R3-2: Docs + CHANGELOG + push + re-review

**Files:** `docs/autopilot.md`, `CHANGELOG.md`

Document that implement fingerprint covers `scripts/` + `bin/`. Changelog under Unreleased. Push. `gh pr comment 84` with `@codex review`. Do not merge.

---
