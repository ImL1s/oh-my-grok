# OMG OMX Team Mailbox / API Parity Implementation Plan

> **For Codex:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Bring `omg team` to honest OMX-shaped lifecycle + CLI-first `team api` interop (mailbox/task claim/status) without claiming native Grok team or full OMX parity overnight.

**Architecture:** Keep the experimental tmux plane (`OMG_EXPERIMENTAL_TMUX_TEAM=1`) as the control plane. Expose a new `omg team api <op> --input JSON [--json]` façade over existing `omg_cli/team/mailbox.py` + plane/worker state. Prefer durable file/state mutations over ad-hoc `tmux send-keys`. Host `--madmax`/`OMG_LAUNCH_POLICY` stays a separate host-launch concern.

**Tech Stack:** Python 3.11+, pytest, existing `omg_cli/team/*`, argparse in `omg_cli/main.py`, OMX reference at `.omx/tmp/upstreams-current/oh-my-codex/src/team/api-interop.ts` (`TEAM_API_OPERATIONS`, 33 ops).

**Reference (read-only):**
- OMX: `src/cli/team.ts`, `src/team/api-interop.ts`, `src/scripts/demo-team-e2e.sh`
- OMG today: `omg_cli/team/{plane,mailbox,tmux_adapter,worktree}.py`, `omg team start|run|status|resume|stop|collect|scale`
- Gate: `OMG_EXPERIMENTAL_TMUX_TEAM=1`

**Honesty / non-goals:**
- Do **not** remove the experimental gate in P0.
- Do **not** claim OMC/OMX Stop hard-pin or native Grok multi-agent team.
- Do **not** make host `--tmux` imply team plane.
- P0 implements a **subset** of OMX ops; document the rest as planned.

---

### Task 1: Freeze P0 API contract + failing tests

**Files:**
- Create: `omg_cli/team/api.py`
- Create: `tests/test_team_api.py`
- Modify: `docs/security-model.md` (short honesty note only if needed later)

**Step 1: Write failing tests for P0 ops**

P0 operations (must match OMX names):
- `send-message`, `mailbox-list`, `mailbox-mark-delivered`
- `create-task`, `list-tasks`, `claim-task`, `transition-task-status`, `release-task-claim`
- `get-summary`, `read-config`

```python
def test_team_api_unknown_op_fails_closed():
    ...

def test_send_message_and_mailbox_list_roundtrip(tmp_path):
    ...

def test_claim_task_requires_token_for_transition(tmp_path):
    ...
```

**Step 2: Run tests — expect FAIL (module missing)**

Run: `PYTHONPATH=. .venv/bin/python -m pytest -q tests/test_team_api.py --tb=line`
Expected: FAIL import / not implemented

**Step 3: Minimal `api.py` dispatch table**

Map op → handler; unknown op → exit 2 with stable error envelope:
`{"ok":false,"operation":"...","error":{"code":"E_TEAM_API_UNKNOWN","message":"..."}}`

**Step 4: Re-run tests until green for stubs that intentionally return E_TEAM_API_UNIMPLEMENTED only for non-P0**

**Step 5: Commit**

```bash
git add omg_cli/team/api.py tests/test_team_api.py
git commit -m "test(team): add failing OMX-shaped team api contract"
```

---

### Task 2: Wire mailbox ops onto existing mailbox.py

**Files:**
- Modify: `omg_cli/team/api.py`
- Modify: `omg_cli/team/mailbox.py` (only if missing list/ack helpers)
- Test: `tests/test_team_api.py`, reuse `tests/test_team_mailbox.py` if present

**Step 1: Failing test for send/list/ack with generation fence**

**Step 2: Implement handlers calling `send_message` / list / ack**

Workers still must not write mailbox files directly.

**Step 3: pytest green**

**Step 4: Commit** `feat(team): expose mailbox ops via omg team api`

---

### Task 3: Task claim / transition façade

**Files:**
- Modify: `omg_cli/team/api.py`, `omg_cli/team/plane.py` and/or `omg_cli/workers.py`
- Test: `tests/test_team_api.py`

**Step 1: Failing tests for claim + transition with claim_token**

**Step 2: Implement create/list/claim/transition/release against run team state**

Task id rule: file `task-<id>.json` vs API bare `"1"` (match OMX).

**Step 3: pytest green**

**Step 4: Commit** `feat(team): claim/transition task ops on team api`

---

### Task 4: CLI router `omg team api`

**Files:**
- Modify: `omg_cli/main.py`
- Modify: `tests/test_docs_cli_drift.py` / `docs/skills.md` if documented
- Test: `tests/test_cli_router.py` or new cases in `tests/test_team_api.py`

**Step 1: Failing CLI parse test**

`omg team api send-message --input '{"..." }' --json`

**Step 2: argparse subparser + `cmd_team_api`**

Require experimental gate same as other team cmds.

**Step 3: docs/skills drift green**

**Step 4: Commit** `feat(cli): add omg team api subcommand`

---

### Task 5: Worker inbox.md write path (leader-owned)

**Files:**
- Create or modify: helpers under `omg_cli/team/` for `workers/<id>/inbox.md`
- API op: `write-worker-inbox` (P0.5 — include if cheap; else P1)
- Test: unit covering path + no worker self-write

**Step 1–4:** TDD as above; commit `feat(team): leader-owned worker inbox writes`

---

### Task 6: Status/resume/shutdown honesty + README

**Files:**
- Modify: `README.md`, `docs/security-model.md`, `CHANGELOG.md`
- Ensure `omg team status|resume|stop` mention api mailbox path

**Step 1:** Doc that P0 ops are shipped; remaining OMX ops listed as P1
**Step 2:** Unit/docs drift checks
**Step 3:** Commit `docs(team): document OMX-shaped team api P0`

---

### Task 7: Hermetic smoke

**Files:**
- Optionally extend `scripts/smoke.sh` with dry-run api help only (no live tmux)

**Step 1:** `PYTHONPATH=. .venv/bin/python -m pytest -q -m "not live" tests/test_team_api.py tests/test_team_mailbox.py`
**Step 2:** `python scripts/check_writer_ownership.py` after adding owned paths for any new files under W3
**Step 3:** Commit ownership pattern updates if needed

---

## P1 / P2 (do not implement in first parallel pass unless P0 done)

- **P1:** Remaining OMX ops (broadcast, heartbeats, events, shutdown-request/ack, cleanup, monitor snapshots, approvals), `dispatch/requests.json`
- **P2:** Team Big Five coordination protocol, ultragoal bridge, multi-CLI routing polish

## Verification gate before claiming done

- `tests/test_team_api.py` green
- `omg team api --help` lists P0 ops
- Experimental gate still required
- No false claim of full 33-op OMX parity in README
