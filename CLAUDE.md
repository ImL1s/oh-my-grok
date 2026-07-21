# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

oh-my-grok (OMG) is an OMC/OMX-style multi-agent orchestration layer for the **Grok Build CLI** (`grok`). It is **not** a fork of grok-build — it is a Grok *plugin* (skills/agents/hooks) plus a Python `omg` CLI. Two surfaces, both required for the full product:

- **Grok plugin** — `skills/omg-*/SKILL.md`, `agents/omg-*.md`, `hooks/hooks.json` (+ `hooks/bin/*.py`). Installed via `grok plugin install`.
- **`omg` CLI** (`omg_cli/`, entry `bin/omg`) — the authoritative state machine: run state, evidence stamps, acceptance, integrate, and the ONLY writer of `passes`/`verified` under `.omg/state/`. Agents write *proposals* under `.omg/artifacts/` only.

## Commands

```bash
# Unit gate (fast, hermetic — the default). Use .venv/bin/python if present, else python3:
PYTHONPATH=. .venv/bin/python -m pytest -q -m "not live"
# Single test / file:
PYTHONPATH=. .venv/bin/python -m pytest -q tests/test_verdict.py::test_terminal_approve
grok plugin validate .                 # plugin manifest check
./bin/omg doctor                       # health + drift checks (add --strict to fail on soft/compat WARNs)
OMG_E2E=1 bash scripts/smoke.sh        # hermetic end-to-end (expects final line ALL_REAL_E2E_OK)
./scripts/live_suite.sh --quick|--full # REAL grok sessions (quota; evidence under docs/research/live/, gitignored)
python3 scripts/generate_capabilities_lock.py [--check]   # regen/verify omg_capabilities.lock.json
python3 scripts/check_docs_links.py    # docs internal-link + skill-count guard
./scripts/install-plugin.sh            # install/refresh plugin + global hook + omg symlink
```

Install the dev plugin from this checkout, then `omg setup` in a project scaffolds `.omg/` and writes the global guidance file.

## Architecture — the big picture

**Grok host contract shapes everything.** Only the `PreToolUse` hook can block or return a decision; every other Grok hook event is passive (its stdout is IGNORED — no context injection like Claude Code). Consequences that pervade the design:
- The operating contract is injected as a **rules file** (`~/.grok/rules/omg.md`), NOT via a hook. `omg_cli/guidance.py` + `templates/omg-rules.md` render it and reconcile it non-destructively (`<!-- OMG:START -->`…`<!-- OMG:END -->` markers, preserving a `USER:OMG:POLICY` block, with a source-hash). `omg setup` installs it; `omg doctor` reports its drift.
- **Keyword/workflow routing lives in that rules file's `<workflow_routing>` section**, not a `UserPromptSubmit` hook (which can't inject).
- Continuity is a rules-file/CLI concern: `hooks/bin/session_start.py` writes `.omg/state/RESUME.md`; the model is told to read it (nothing re-injects it).

**Isolation is `capability_mode`, not the hook.** The primary isolation is Grok's per-spawn `capability_mode` (read-only for explore/critic/verifier; read-write for implementers; never execute/all for default workers). `hooks/bin/pre_tool_use_deny.py` → `omg_cli/deny.py` is a **fail-open soft-gate** that blocks external-agent-CLI invocations (claude/codex/agy/cursor-agent/kimi, `omc team`) and enforces `capability_mode` on `spawn_subagent`. It is not a sandbox.

**Two security-critical, fail-closed modules — touch with adversarial probes + the full suite:**
- `omg_cli/verdict.py` — the strict verdict parser backing `dual_review` and `ralplan` gates. It must never false-green: document-level run_id **poison guard**, extract **ALL** top-level JSON objects (a UNION of quote-aware + quote-agnostic brace scans), **severity aggregation** (FAILED > REQUEST_CHANGES > APPROVE), fenced-example/negation stripping. Path-bound unbound artifacts (`## Verdict\nAPPROVE`) are still accepted. Regressions here are subtle and ordering-dependent — a green unit suite is not proof; probe both document orderings and break-glass shapes.
- `omg_cli/command_policy.py` — the acceptance-command floor. `python -c/-e` and `node -e/-p` are denied even under `--no-allowlist` break-glass (the grammar is skipped there, so the floor is the only guard). The interpreter-flag region uses a **fail-closed boundary**: a bare token ends the region only if it is a real `.py` script (or `-m`/`--`); any other bare token is treated as an option value and scanning continues, so an unknown/future option can't hide a `-c`/`-e`.

**Orchestration modes** (`omg_cli/modes.py`, `fanout.py`, `autopilot.py`, `ralplan.py`, `pipeline.py`) launch `grok` (dry-run writes argv/state without exec). `omg_cli/autopilot.py` is a strict phase FSM (`LEGAL_TRANSITIONS`); entering `implement`/`rework`/replan invalidates stale review/QA stamps so unreviewed code can't reach `verified`. `verified` is only set in-process after `omg accept` (CLI stamp + matching manifest sha + acceptance token; forged `{passed:true}` is rejected).

**ULW worker flow** (`omg_cli/workers.py`, `integrate.py`): the leader owns a CLI-written ownership manifest (`omg worker own`), workers edit isolated git worktrees (`.omg/worktrees/<run_id>/<task_id>`), the leader seals each (`omg worker seal [--all]` — `seal_task` computes `head_sha` from real `git rev-parse HEAD`, fail-closed: `ok` requires head!=base), `join` fails closed on ownership violations, and `integrate` cherry-picks (refuses a dirty leader tree — no auto-stash).

## Gotchas that require reading multiple files

- **Version bump**: edit `plugin.json`, regen `omg_capabilities.lock.json` (`generate_capabilities_lock.py`), regen the global-hook standalone (`generate_standalone_hook.py` — it embeds the plugin version, so `--check` fails CI otherwise), add a CHANGELOG section, and re-run `omg setup` to refresh the installed `~/.grok/rules/omg.md` version + global hook. The **installed plugin snapshot** does NOT refresh via `grok plugin update` for a local-path install (it's a no-op) — reliable refresh is `grok plugin uninstall oh-my-grok --confirm && ./scripts/install-plugin.sh`. `omg doctor`'s version-drift + installed-capabilities-lock checks catch the staleness.
- **Editing any `skills/omg-*/SKILL.md` or `agents/omg-*.md`** changes the capabilities-lock aggregate — regenerate the lock, or `omg doctor` warns.
- **Docs ↔ CLI drift** is guarded: `tests/test_docs_cli_drift.py` diffs documented `omg <cmd> <sub>` strings in `docs/skills.md` against the real argparse choices in `omg_cli/main.py`. Keep them in sync.
- **Hooks must fail open**: `hooks/bin/*.py` never crash a session and never set `verified` (`stop.py` is passive by design). Kill switches: `DISABLE_OMG=1` (all hooks off), `OMG_SKIP_HOOKS="name,…"` (per-hook).
- **`OMG_ALLOW_EXTERNAL_CLI`** is read only from the process environment, never parsed from command text; `deny.py` denies external agent CLIs at command position (including on their own line in a multi-line script).

## Scope honesty (do not overclaim in docs or commits)

Per `docs/security-model.md` and the parity matrix: `capability_mode` is the primary isolation; PreToolUse is a fail-open soft-gate (not a sandbox). There is no OMC-style Stop hard-pin (Grok's Stop is passive), no full LSP/AST MCP bridge (`omg lsp` is a local probe). An **experimental** tmux team plane (`omg team`, behind `OMG_EXPERIMENTAL_TMUX_TEAM=1`) exists: zero-config is grok-only; `--routing` enables multi-CLI executor panes (codex/agy/cursor/gemini) with role floors (reviewer/verifier cannot use cursor; posture from role). Isolation is **integration** isolation (worktree ownership + seal + integrate), **not** an execution sandbox — provider CLI read-only strength is uneven (gemini: none; agy: best-effort). `omg --madmax` is a break-glass host launcher, not a mode FSM.
