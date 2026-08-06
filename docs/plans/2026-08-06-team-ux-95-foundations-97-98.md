# Team UX Foundations (#97 + #98) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Bind Team launch to the invoking `TMUX_PANE`, restore leader focus after worker layout, and probe worker liveness via exact `pane_id` (not logical `window_index`).

**Architecture:** Capture the invoking pane once (env / explicit), thread `-t <pane>` through session probes, create workers with focus-detached tmux flags (`-d`), then `select-pane` back to the leader. Status prefers `pane_alive(pane_id)` with legacy `_window_alive` fallback.

**Tech Stack:** Python 3.11+, tmux CLI via `_tmux_run`, hermetic monkeypatched tests.

---

### Task 1: Invoking pane + focus restore (#97)

**Files:**
- Modify: `omg_cli/team/tmux.py`
- Test: `tests/test_team_tmux_transport.py`

**Steps:** Add `resolve_invoking_pane`, target session/pane probes with `-t`, use `new-window -d` / `split-window -d`, end with `select-pane -t <leader>`.

### Task 2: Exact pane status (#98)

**Files:**
- Modify: `omg_cli/team/plane.py` (`team_status`)
- Test: `tests/test_team_lifecycle.py` or new focused test in `tests/test_team_tmux_transport.py`

**Steps:** When task has valid `pane_id`, `alive = pane_alive(...)`; else `_window_alive`. Keep `STATUS_TASK_KEYS` locked set unchanged.
