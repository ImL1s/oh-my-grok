# OMG Team Live Promotion Implementation Plan

> **For Codex:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Promote experimental `omg team [N[:role]] "<goal>"` from dry-run/state seed to a single non-dry-run live path that proves real split panes, worker ACK/claim/work/commit, status/resume/stop — without claiming full OMX 33-op parity.

**Architecture:** Keep CLI-stamped run-scoped authority under `.omg/state/runs/<run>/team/` plus non-authoritative `ref.json` lookup. Harden `launch_team` / `create_split_team_session` / identity-bound `execute_team_api`, then gate promotion on `scripts/live_team_smoke.py --live` evidence. Experimental env stays until that smoke passes.

**Tech Stack:** Python 3.11+, pytest, tmux, grok CLI, existing `omg_cli/team/*`, hermetic fixtures then live smoke.

**Baseline (already on main `ca1870a`):** shorthand → `launch`, split topology, decomposition, api board seed, worker identity matrix, `skills/omg-team`, dry smoke `DRY_TEAM_SMOKE_OK`. Still experimental; plan stop condition in `docs/plans/2026-07-25-omx-team-launch-ux-p0.md` not yet met.

**Related:** @docs/plans/2026-07-25-omx-team-launch-ux-p0.md · @skills/omg-team/SKILL.md

---

### Task 1: Bind worker env identity into team api payload

**Files:**
- Modify: `omg_cli/team/api.py` (`execute_team_api` / `_apply_worker_identity_matrix`)
- Modify: `omg_cli/team/plane.py` (`TEAM_RUN_ID_ENV`, `TEAM_ID_ENV`, `TEAM_OWNER_TOKEN_ENV`)
- Test: `tests/test_team_api.py`

**Step 1: Write the failing test**

```python
def test_team_worker_payload_bound_to_env_run_and_team(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _seed_control_plane(tmp_path, monkeypatch)
    _exec(
        tmp_path,
        "create-task",
        {"subject": "seed", "description": "seed", "workers": ["worker-1"]},
        run_id=run_id,
        monkeypatch=monkeypatch,
    )
    monkeypatch.setenv("OMG_TEAM_WORKER", "1")
    monkeypatch.setenv("OMG_TEAM_WORKER_ID", "worker-1")
    monkeypatch.setenv("OMG_TEAM_RUN_ID", run_id)
    monkeypatch.setenv("OMG_TEAM_ID", TEAM)
    # Wrong run_id in payload must be overwritten or rejected
    code, envelope = execute_team_api(
        "send-message",
        {
            "run_id": "other-run",
            "team_id": TEAM,
            "from_worker": "worker-1",
            "to_worker": "leader-fixed",
            "body": "ACK",
        },
        root=tmp_path,
    )
    assert code != 0 or envelope["data"]["message"]["sender_id"] == "worker-1"
    # Prefer fail-closed on mismatch:
    assert code == 2
    assert envelope["error"]["code"] == "E_TEAM_API_GATE"
```

**Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. .venv/bin/python -m pytest -q tests/test_team_api.py::test_team_worker_payload_bound_to_env_run_and_team -v`  
Expected: FAIL (missing test or still accepts forged run_id)

**Step 3: Write minimal implementation**

In `_apply_worker_identity_matrix` / `execute_team_api`:
- Read `OMG_TEAM_RUN_ID` / `OMG_TEAM_ID` / `OMG_TEAM_OWNER_TOKEN` from env when `OMG_TEAM_WORKER=1`
- If payload `run_id`/`team_id` present and differ → `E_TEAM_API_GATE`
- Else inject env values into payload before handlers
- Do **not** trust client-supplied `from_worker` (already forced)

**Step 4: Run test to verify it passes**

Run: `PYTHONPATH=. .venv/bin/python -m pytest -q tests/test_team_api.py -k worker -v`  
Expected: PASS

**Step 5: Commit**

```bash
git add omg_cli/team/api.py omg_cli/team/plane.py tests/test_team_api.py
git commit -m "fix(team): bind worker api payload to immutable env identity"
```

---

### Task 2: Hermetic fixture transport smoke (split panes + ACK, no grok)

**Files:**
- Create: `tests/fixtures/team_worker_fixture.py` (or `scripts/fixtures/team_worker.sh`)
- Create: `tests/test_team_tmux_transport.py`
- Modify: `omg_cli/team/tmux.py` / `omg_cli/team/runtime.py` only if needed for injectability
- Modify: `scripts/live_team_smoke.py` to accept `--fixture-executor`

**Step 1: Write the failing test**

```python
def test_split_transport_two_panes_and_acks(tmp_path, monkeypatch):
    # start_team/launch with topology=split but pane_command = fixture script
    # that writes ACK via omg team api then exits
    ...
    assert pane_count == 2
    assert ack_count == 2
```

**Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. .venv/bin/python -m pytest -q tests/test_team_tmux_transport.py -v`  
Expected: FAIL (module/fixture missing)

**Step 3: Write minimal implementation**

- Fixture binary/script: read `OMG_TEAM_*` env, call `omg team api send-message` with ACK, optional claim, exit 0
- Test doubles: real tmux required (`pytest.importorskip` / skip if no tmux); never claim this is Grok parity
- `launch_team(..., executor="fixture")` or monkeypatch pane command builder for tests only

**Step 4: Run test to verify it passes**

Run: `PYTHONPATH=. .venv/bin/python -m pytest -q tests/test_team_tmux_transport.py -v`  
Expected: PASS (or skip if no tmux in CI — document marker `tmux`)

**Step 5: Commit**

```bash
git add tests/fixtures/team_worker_fixture.py tests/test_team_tmux_transport.py \
  omg_cli/team/runtime.py scripts/live_team_smoke.py
git commit -m "test(team): hermetic split-pane transport + ACK fixture"
```

---

### Task 3: Bounded ACK wait before `Team started` / running status

**Files:**
- Modify: `omg_cli/team/runtime.py` (`launch_team`)
- Modify: `omg_cli/team/plane.py` (live meta status)
- Test: `tests/test_team_runtime.py` / `tests/test_team_tmux_transport.py`

**Step 1: Write the failing test**

```python
def test_launch_live_fixture_waits_for_acks(tmp_path, monkeypatch):
    meta = launch_team(..., dry_run=False, check_binary=False, env={...})
    assert meta.get("startup_acks") == 2
    assert meta.get("status") in ("running", None)  # or team.json note
    # delayed fixture: if ACK timeout, status failed_start / non-zero path
```

**Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. .venv/bin/python -m pytest -q tests/test_team_runtime.py -k ack -v`  
Expected: FAIL (`startup_acks` absent)

**Step 3: Write minimal implementation**

- After tmux create, poll mailbox for `leader-fixed` messages with body `ACK` from each worker id (timeout env `OMG_TEAM_READY_TIMEOUT_MS`, default 45000)
- dry_run: skip wait, set `startup_acks=null` / note
- On timeout: leave state for diagnosis; return non-zero from CLI (`cmd_team` launch); do **not** silently fall back to dry-run or ULW

**Step 4: Run test to verify it passes**

Run: `PYTHONPATH=. .venv/bin/python -m pytest -q tests/test_team_runtime.py tests/test_team_tmux_transport.py -v`  
Expected: PASS

**Step 5: Commit**

```bash
git add omg_cli/team/runtime.py omg_cli/team/plane.py tests/test_team_runtime.py
git commit -m "feat(team): wait for worker ACKs before reporting launch running"
```

---

### Task 4: Leader-window split when already inside tmux

**Files:**
- Modify: `omg_cli/team/tmux.py`
- Modify: `omg_cli/team/runtime.py` / `omg_cli/team/plane.py` (pass `inside_tmux` / attach policy)
- Test: `tests/test_team_tmux_transport.py`

**Step 1: Write the failing test**

```python
def test_inside_tmux_splits_current_window(monkeypatch, tmp_path):
    monkeypatch.setenv("TMUX", "...")
    # create_split_team_session should use split-window -t current, not only new-session
    ...
```

**Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. .venv/bin/python -m pytest -q tests/test_team_tmux_transport.py::test_inside_tmux_splits_current_window -v`  
Expected: FAIL

**Step 3: Write minimal implementation**

- If `TMUX` set: split current window; record exact `pane_id`s; never kill leader pane
- If interactive TTY and not in tmux: `new-session` + split + print `tmux attach -t …`
- If non-interactive without `--detach`: fail closed with attach instructions (already sketched in P0 plan)

**Step 4: Run test to verify it passes**

Run: `PYTHONPATH=. .venv/bin/python -m pytest -q tests/test_team_tmux_transport.py -v`  
Expected: PASS

**Step 5: Commit**

```bash
git add omg_cli/team/tmux.py omg_cli/team/runtime.py omg_cli/team/plane.py tests/test_team_tmux_transport.py
git commit -m "feat(team): split current tmux window when leader already attached"
```

---

### Task 5: status snapshot aggregates pane + mailbox + worktree

**Files:**
- Modify: `omg_cli/team/runtime.py` (`status_for_identity`)
- Modify: `omg_cli/team/plane.py` (`team_status` / `status_locked_view`) optional
- Test: `tests/test_team_lifecycle.py` (create)

**Step 1: Write the failing test**

```python
def test_status_includes_acks_and_topology(tmp_path, monkeypatch):
    meta = launch_team(..., dry_run=True, ...)
    st = status_for_identity(tmp_path, meta["team_name"])
    assert st["topology"] == "split"
    assert "mailbox" in st or "api_summary" in st
```

**Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. .venv/bin/python -m pytest -q tests/test_team_lifecycle.py -v`  
Expected: FAIL

**Step 3: Write minimal implementation**

- Merge `team_status` + `get-summary` (leader) + worktree paths from meta
- Keep `status_locked_view` stable; add new fields under explicit keys, document in `docs/skills.md`

**Step 4: Run test to verify it passes**

Run: `PYTHONPATH=. .venv/bin/python -m pytest -q tests/test_team_lifecycle.py -v`  
Expected: PASS

**Step 5: Commit**

```bash
git add omg_cli/team/runtime.py omg_cli/team/plane.py tests/test_team_lifecycle.py docs/skills.md
git commit -m "feat(team): richer status snapshot for shorthand launches"
```

---

### Task 6: resume dead incomplete worker (generation +1)

**Files:**
- Modify: `omg_cli/team/scaling.py` or `omg_cli/team/runtime.py` (prefer thin wrapper calling existing `resume_team` + relaunch helper)
- Modify: `omg_cli/team/tmux.py`
- Test: `tests/test_team_lifecycle.py`

**Step 1: Write the failing test**

```python
def test_resume_restarts_dead_fixture_worker(tmp_path, monkeypatch):
    # launch fixture team; kill one pane; resume; generation increments; task still non-terminal
    ...
```

**Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. .venv/bin/python -m pytest -q tests/test_team_lifecycle.py::test_resume_restarts_dead_fixture_worker -v`  
Expected: FAIL

**Step 3: Write minimal implementation**

- Detect dead pane for non-terminal task
- If worktree clean enough / receipt matches: respawn pane with generation+1
- If dirty/identity drift: report `blocked` in status; do not overwrite worktree

**Step 4: Run test to verify it passes**

Run: `PYTHONPATH=. .venv/bin/python -m pytest -q tests/test_team_lifecycle.py -v`  
Expected: PASS

**Step 5: Commit**

```bash
git add omg_cli/team/runtime.py omg_cli/team/tmux.py omg_cli/team/scaling.py tests/test_team_lifecycle.py
git commit -m "feat(team): resume relaunches dead incomplete workers safely"
```

---

### Task 7: stop/shutdown graceful gate for in_progress claims

**Files:**
- Modify: `omg_cli/team/plane.py` (`stop_team`) and/or `omg_cli/team/runtime.py`
- Modify: `omg_cli/main.py` (`--force` on stop if missing)
- Test: `tests/test_team_lifecycle.py`

**Step 1: Write the failing test**

```python
def test_stop_without_force_fails_on_in_progress(tmp_path, monkeypatch):
    ...
    with pytest.raises(TeamError) or assert cli exit == 1:
        stop_team(...)
```

**Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. .venv/bin/python -m pytest -q tests/test_team_lifecycle.py::test_stop_without_force_fails_on_in_progress -v`  
Expected: FAIL

**Step 3: Write minimal implementation**

- Write durable shutdown request file under team dir
- Non-force + active claims → non-zero + reasons
- Force → exact pane/session teardown only (existing hardened kill); preserve state/worktrees
- Never `pkill -f`

**Step 4: Run test to verify it passes**

Run: `PYTHONPATH=. .venv/bin/python -m pytest -q tests/test_team_lifecycle.py -v`  
Expected: PASS

**Step 5: Commit**

```bash
git add omg_cli/team/plane.py omg_cli/team/runtime.py omg_cli/main.py tests/test_team_lifecycle.py
git commit -m "feat(team): fail-closed stop when claims active unless --force"
```

---

### Task 8: Live Grok smoke script promotion gate

**Files:**
- Modify: `scripts/live_team_smoke.py`
- Modify: `scripts/live_suite.sh` (optional wire)
- Modify: `docs/RELEASE.md` (+ zh / zh-TW one paragraph)
- Modify: `CHANGELOG.md`

**Step 1: Write failing assertions in script (run --live expecting fail until ready)**

Extend `live_team_smoke.py --live` to assert:
- `dry_run == false`
- pane count == workers
- pane command contains `grok` (not fixture)
- 2 worktrees in `git worktree list`
- ≥N ACK messages
- claim → completed transitions present
- `stop` clears owned session; other tmux sessions untouched
- print final line `LIVE_TEAM_SMOKE_OK` only if all pass

**Step 2: Run dry path still green**

Run: `python3 scripts/live_team_smoke.py --workers 2 --goal $'1. a\n2. b'`  
Expected: `DRY_TEAM_SMOKE_OK`

**Step 3: Run live when credentials/quota allow**

Run:
```bash
OMG_EXPERIMENTAL_TMUX_TEAM=1 python3 scripts/live_team_smoke.py --live \
  --workers 2 --role executor \
  --goal "worker 1 and worker 2 each complete an assigned marker change and test"
```
Expected: `LIVE_TEAM_SMOKE_OK` (or honest non-zero with reason)

**Step 4: Only after LIVE_TEAM_SMOKE_OK — docs honesty**

- CHANGELOG: “live shorthand smoke passed” evidence path under `docs/research/live/` (gitignored ok; summarize in RELEASE)
- Do **not** remove `OMG_EXPERIMENTAL_TMUX_TEAM` gate in this task unless smoke is green and review agrees

**Step 5: Commit**

```bash
git add scripts/live_team_smoke.py scripts/live_suite.sh docs/RELEASE.md docs/RELEASE.zh.md docs/RELEASE.zh-TW.md CHANGELOG.md
git commit -m "test(team): harden live team smoke gate for promotion evidence"
```

---

### Task 9: Optional Grok `/team` alias honesty check

**Files:**
- Modify: `skills/omg-team/SKILL.md`
- Modify: `docs/skills.md` (+ zh)
- Test: `tests/test_plugin_session_discovery.py` if discovery surface changes

**Step 1: Probe host**

Run installed plugin discovery / `grok` help for slash aliases.  
If unsupported: document namespaced `/oh-my-grok:omg-team` only — **do not** claim `/team`.

**Step 2: If supported, register alias + test**

Add failing discovery test → implement → pass.

**Step 3: Commit**

```bash
git add skills/omg-team/SKILL.md docs/skills.md docs/skills.zh.md docs/skills.zh-TW.md tests/test_plugin_session_discovery.py
git commit -m "docs(team): record host slash alias proof or namespaced-only honesty"
```

---

### Task 10: Full hermetic gate + Sol review before claiming promotion

**Files:** none new (verification)

**Step 1: Run unit gate**

```bash
PYTHONPATH=. .venv/bin/python -m pytest -q -m "not live" --tb=line
python3 scripts/generate_capabilities_lock.py --check
python3 scripts/check_docs_links.py
python3 scripts/live_team_smoke.py --workers 2 --goal $'1. a\n2. b'
```
Expected: all green + `DRY_TEAM_SMOKE_OK`

**Step 2: Codex Sol max review (read-only)**

```bash
codex exec --ephemeral -m gpt-5.6-sol -c 'model_reasoning_effort="max"' \
  "Review OMG team live-promotion diff vs docs/plans/2026-07-25-omg-team-live-promotion.md stop condition. Verdict APPROVE/REQUEST_CHANGES for promotion claim only."
```

**Step 3: Fix any Sol must-fix; re-run Step 1**

**Step 4: Commit only if fixes landed**

```bash
git commit -m "fix(team): address Sol review before live promotion claim"
```

---

## Stop condition (this plan)

Promotion / “OMX-like launch works” may be claimed only when **one** non-dry-run run proves:

1. `omg team 2:executor "<goal>"` (or `launch`)  
2. Visible split panes with real `grok` processes  
3. Dedicated git worktrees  
4. Durable ACKs + claim → completed  
5. Worker commits/files/tests evidence  
6. `status` / `resume` / `stop` behave as specified  
7. Sol review APPROVE for the **promotion claim** (experimental gate removal is a separate explicit decision)

Until then: keep `OMG_EXPERIMENTAL_TMUX_TEAM=1` and docs honesty (“experimental; dry-run proven”).

## Out of scope (YAGNI here)

- Full OMX 33-op API  
- OMA/OMCU shorthand parity  
- Removing madmax≠team confusion in host launcher  
- Replacing `spawn_subagent` ULW with team  
