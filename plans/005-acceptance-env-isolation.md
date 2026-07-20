# Plan 005: Harden acceptance (and QA) child environment against hijack vars

> **Drift check**: `git diff --stat 997bcce..HEAD -- omg_cli/acceptance.py omg_cli/qa.py omg_cli/command_policy.py docs/security-model.md tests/test_acceptance.py tests/test_qa.py`

## Status

- **Priority**: P1
- **Effort**: M
- **Risk**: MED
- **Depends on**: none (004 is related but independent)
- **Category**: security
- **Planned at**: commit `997bcce`, 2026-07-21

## Why this matters

`omg accept` runs frozen runners (`pytest`, `python -m pytest`, project `.py`, etc.) with an env that only strips `OMG_ALLOW_*`. Ambient `PYTHONPATH`, `PYTHONSTARTUP`, `GIT_DIR`, `GIT_WORK_TREE`, `LD_PRELOAD`/`DYLD_*`, `NODE_OPTIONS`, and npm config vars can redirect “allowed” argv into attacker-controlled code. Acceptance is documented as operator-intent, not OS sandbox — but silent env hijack is still an implementation shortfall relative to argv floors.

## Current state

- `omg_cli/acceptance.py` `sanitized_env` (~409–415): drops `OMG_ALLOW_*` only; used in `run_acceptance` subprocess.run.
- `omg_cli/qa.py` ~178–191: full env copy + may prepend project to `PYTHONPATH`.
- `command_policy.py` allows optional `-I`/`-E` for python but does not inject them.
- `docs/security-model.md` — update residual if behavior changes.

## Commands

| Purpose | Command | Expected |
|---------|---------|----------|
| Acceptance | `python -m pytest -q tests/test_acceptance.py tests/test_command_policy.py --tb=short` | exit 0 |
| QA | `python -m pytest -q tests/test_qa.py --tb=short` | exit 0 |
| Full | `python -m pytest -q -m "not live" --tb=short` | exit 0 |

## Scope

**In scope**:
- `omg_cli/acceptance.py` (`sanitized_env` + possibly python argv normalization)
- `omg_cli/qa.py` (align env builder)
- `tests/test_acceptance.py` (and qa if touched)
- `docs/security-model.md` residual table row (short)

**Out of scope**:
- OS sandbox / seccomp
- Blocking all site-packages (`python -I` may be too strong for some monorepos — see steps)
- Changing allowlist basenames

## Steps

### Step 1: Expand scrub list

In `sanitized_env`, pop (when present) at least:

- `PYTHONSTARTUP`, `PYTHONPATH` (see step 2 for controlled re-add), `PERL5OPT`, `RUBYOPT`
- `GIT_DIR`, `GIT_WORK_TREE`, `GIT_COMMON_DIR`, `GIT_OBJECT_DIRECTORY`
- `LD_PRELOAD`, `DYLD_INSERT_LIBRARIES`, `DYLD_LIBRARY_PATH` (document macOS residual if SIP still allows some)
- `NODE_OPTIONS`, `NODE_PATH`
- `npm_config_*` keys (iterate env keys with prefix)
- Keep existing `OMG_ALLOW_*` strip
- Do **not** strip `PATH`, `HOME`, `USER`, `LANG`, `TMPDIR`, `VIRTUAL_ENV` without a reason (breaks pytest)

### Step 2: Python isolation strategy (pick one; document)

**Preferred**: When executing python family acceptance commands, if argv does not already contain `-I` or `-E`, inject `-E` (ignore PYTHON* env) after the interpreter token **or** rely on scrubbed env + optional `-I`.

If injecting `-I` breaks tests that need site packages from venv: prefer `-E` only + scrub PYTHONPATH, keep `VIRTUAL_ENV` so venv site still works.

Provide escape hatch env e.g. `OMG_ACCEPT_KEEP_PYTHONPATH=1` only if needed — must be documented in security-model as weaken.

### Step 3: QA alignment

QA child env must call the same scrubber; do not silently prepend arbitrary PYTHONPATH without scrubbing hijack keys first. If QA needs project root on path, set a **single** controlled `PYTHONPATH=str(root)` after scrub.

### Step 4: Tests

- `sanitized_env` drops each hijack key
- Acceptance subprocess does not see `PYTHONSTARTUP` (can set toxic startup that writes a marker file — assert marker absent after `run_acceptance` with `true` or a tiny project `.py` script)
- Existing acceptance policy tests still pass

### Step 5: Docs

One short residual note in `docs/security-model.md` Acceptance section: env scrub + what is still inherited (PATH, venv).

## Done criteria

- [ ] Hijack keys stripped from acceptance env
- [ ] Regression test with marker-file attack via PYTHONSTARTUP fails to run marker
- [ ] Hermetic suite green
- [ ] security-model updated
- [ ] README DONE

## STOP conditions

- Injecting `-I` breaks majority of acceptance tests in-repo — fall back to scrub-only + `-E`, document.
- Need to strip `PATH` to be “safe” — STOP; that is wrong layer for this product.

## Maintenance notes

- New interpreter families in command_policy need matching env notes.
- Reviewer: ensure escape hatch is not default-on.
