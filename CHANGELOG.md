# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Product version source of truth: [`plugin.json`](./plugin.json).

## [Unreleased]

### Planned
- Optional PyPI/`pipx` CLI track (deferred).
- Optional PR to xAI plugin-marketplace (sha-pinned).
- Host Stop veto (not feasible on Grok today).
- Full OMC LSP/AST MCP bridge (local pyright probe only in 0.3.0).

## [0.4.1] - 2026-07-21

Backlog polish + a security-floor hardening pass, all reviewer-driven (Fable 5
full-branch GO). Merged via PR #2. 528 → 547 unit tests.

### Fixed (security)
- **command_policy break-glass floor:** a v0.4.0-round attempt to fix a
  false-positive (`python3 -m pytest -rc` wrongly denied) narrowed the `-c`/`-e`
  floor scan and reopened a real code-exec bypass under `--no-allowlist`. Fixed
  in layers, ending in a **fail-closed region boundary**: a bare token ends the
  interpreter region only if it is a real `.py` script (or `-m`/`--`); any other
  bare token is treated as a (possibly unknown) option's value, so a following
  `-c`/`-e`/`-p`/`--eval`/`--print` stays caught. Unknown/future interpreter
  options can no longer hide an eval flag (verified with fuzz + break-glass
  probes). Trade-off: an extensionless positional script fails closed under
  break-glass (intentional; normal mode requires `.py`).

### Fixed
- **workers.py:** ownership path normalization used `.lstrip("./")`, collapsing a
  dotfile `.config` to `config`; now `_norm_relpath` keeps dotfiles intact.
- **autopilot.py:** invalidate review/QA stamps on `review` entry from `blocked`
  too, closing the `qa→blocked→review→qa` stale-review-stamp reuse.

### Added
- **`omg note --prune`:** the `[7d]` tag is now a real TTL (drops entries older
  than 7 days; keeps `[permanent]` + unparseable-timestamp lines).
- **`doctor` installed-snapshot capabilities lock:** hashes the installed frozen
  snapshot's skills/agents against the committed lock (true OMX installed-drift;
  complements the local-checkout guard).
- **Docs-drift guard** extended from `omg goal` to every sub-actioned command.

### Notes
- Deliberately unchanged: the `deny.py` quoted/heredoc-line false-positive stays
  fail-closed (fixing it reopens the heredoc-body bypass).
- Backlog: ULW leader-side auto-seal (`omg worker seal --all`) is a designed
  feature for a later release; an unknown future interpreter option taking a
  separate `.py`-suffixed value would need adding to the arg-consuming set (no
  such real option exists today).

## [0.4.0] - 2026-07-21

OMC/OMX parity upgrade — global guidance injection, install lifecycle, and a
verdict-gate hardening pass. All work was executor-written under orchestrator
briefs and gated by an independent model-diverse standing reviewer (Fable 5,
full-branch GO). 468 → 528 unit tests; live-verified on the real Grok host.

### Added
- **Global guidance injection (`~/.grok/rules/omg.md`):** the Grok-native OMC
  `CLAUDE.md` / OMX `AGENTS.md` equivalent. `omg setup` writes an always-loaded
  operating contract (tuned to Grok 4.5) via a non-destructive marker reconcile
  (`OMG:START/END`), preserving any `USER:OMG:POLICY` block, with a source-hash
  handshake and rolling backup (`omg_cli/guidance.py`, `templates/omg-rules.md`).
  `omg setup --no-global-rules` opts out. Live-proven: `grok inspect` loads it and
  a fresh `grok -p` quotes the contract.
- **`omg update`:** git pull + `grok plugin update` (force-refresh the frozen
  snapshot) + doctor.
- **`omg uninstall`:** `--yes`-gated removal of plugin, global hook, OMG rules
  block (preserves `USER:OMG:POLICY`), and CLI symlink; never touches project `.omg/`.
- **`omg note`:** compaction-resistant project notepad (`.omg/notepad.md`, 7d /
  `--priority` permanent TTL, `--show`).
- **Kill switches:** `DISABLE_OMG` (all hooks off; deny fails open) and
  `OMG_SKIP_HOOKS` (per-hook logical names).
- **Doctor drift checks:** global-rules status, plugin version-drift + duplicate
  detection, `[plugins].enabled`, and a local-checkout capabilities lock
  (`omg_capabilities.lock.json` + `scripts/generate_capabilities_lock.py`).
- **Self-healing installer:** `install-plugin.sh` warns on duplicate entries and
  runs `grok plugin update` + `grok plugin enable`.
- **Anti-drift docs guard:** `tests/test_docs_cli_drift.py` diffs documented `omg`
  subcommands against the real argparse choices.

### Fixed (security / correctness, each with a RED-proven regression test)
- **deny.py:** external-CLI block bypassed by multi-line commands (a denied bin on
  its own line) — `\n\r` added to the command-position class.
- **verdict.py:** run_id false-accept hardened in three layers — document-level
  poison guard, extract-ALL top-level objects, severity aggregation
  (FAILED > REQUEST_CHANGES > APPROVE), and a UNION of quote-aware + quote-agnostic
  brace scans (closes stale-object hiding via unbalanced braces in strings and odd
  prose quotes). Path-bound unbound artifacts still accepted.
- **command_policy.py:** break-glass floor now denies `python -c` via combined
  short clusters (`-ic`).
- **autopilot.py:** invalidate review/QA stamps on every (re)entry into `implement`
  (closes the `qa→blocked→implement→blocked→qa` false-green round-trip).
- **workers.py:** empty `owned_files` fails closed in join.
- **docs/skills.md(+zh):** `omg goal start`/`complete` → real `start-story`/`complete-story`.

### Notes
- Keyword triggers live in the rules file's `<workflow_routing>` section, not a
  hook — Grok's non-`PreToolUse` hooks are passive (stdout ignored).
- Known backlog: ULW worker envelope `head_sha` requires `omg worker seal`
  (leader-side / omg on the worker's PATH); installed-snapshot content-drift lock;
  duplicate same-named plugin entries need manual `grok plugin uninstall` by key.

## [0.3.2] - 2026-07-21

### Fixed
- **QA freeze allowlist UX:** reject illegal scenarios at freeze (not only at run) with operator tips (`grep`/`test`/`omg`/`python -c`); prefer project `.py` or `python3 -m pytest`.
- **pytest marker coalesce:** unquoted `-m not live` → `-m 'not live'` on QA and accept paths so marker expr is not split into a fake path.
- **Autopilot complete short-circuit:** if the run is already verified (e.g. prior `omg accept`), sync autopilot phase without re-running freeze_and_run / full acceptance.
- **`status.autopilot_phase`:** set to `verified` on `set_verified` and complete so status no longer lingers at `acceptance`.

### Added
- **Auto PRD from clean UltraQA:** `materialize_prd_from_ultraqa` for missing `prd.json` (CLI-stamped clean only; never overwrites operator PRD); wired into `omg accept` and `omg autopilot complete`.
- **`merge_status_fields`:** non-authority status metadata merge (cannot set `verified`/`status`).

### Changed
- Skills `omg-ultraqa` / `omg-autopilot`: correct freeze examples (quoted markers; no illegal basenames); document complete short-circuit + optional prd.

## [0.3.1] - 2026-07-21

### Fixed
- **strict-v2 `omg accept` / `set_verified`:** auto-acquire execution lease when caller omits lease (default ralph completion gate).
- **Verdict false-green residuals:** case-insensitive prose `FAILED`; schema_version=2 documents no longer fall through to terminal prose APPROVE; balanced JSON extract when prose trails a JSON blob.
- **Integrate strict status:** failure paths write run status `blocked` (not illegal `failed`) on schema v2.
- **Process fanout:** child env uses `safe_supervised_child_env`; shared wait deadline (not N×timeout).
- **Acceptance/QA env:** scrub runner-hijack keys (PYTHONSTARTUP/PATH-like, GIT_*, LD_PRELOAD/DYLD_*, NODE_*, npm_config_*).
- **run_id path safety:** `_safe_run_id` on fanout/modes/ask/dual_review/ralplan/interview/integrate path joiners.
- **Dual-review product wording:** sequential path marked permanent PARTIAL (not open-ended interim).
- **Skill routing:** pipeline no longer claims bare `autopilot` primary; ralplan documents v2 + `omg ask`.
- **Hooks contract tests:** stop path must not set verified.
- **CI:** Python 3.13 matrix; ignore `.ruff_cache`/`.mypy_cache`; research residue gitignored.

### Docs
- security-model: acceptance env scrub + goal-verify disk-trust residual; spawn soft fail-closed retitled as shipped.
- OPEN-ITEMS: mark interview/QA/goal ledger shipped; residual is depth/live evidence.
- `plans/`: improve-deep advisor plans + execution artifacts.

## [0.3.0] - 2026-07-21

### Added
- **R2 continuity:** `omg resume` smart routing; SessionStart writes `.omg/state/RESUME.md`; `omg resume --clear` one-shot lifecycle; louder pack via resume MD + `omg hud`.
- **R3 verdict security:** expanded APPROVE negation; fence strip (incl. unclosed ``` / `~~~`); smart-apostrophe normalize; **schema_version=2** JSON with `run_id` binding (`expected_run_id` in dual-review).
- **In-session skills:** thick `omg-ultragoal`, `omg-autopilot`, `omg-deep-interview`, `omg-ultraqa`; new `omg-wiki`, `omg-hud`, `omg-lsp`.
- **Lifestyle CLI:** `omg wiki {ingest,list,query}`, `omg hud`, `omg lsp {status,check}` (honest: no host LSP MCP).
- **Dirs:** `.omg/wiki/` scaffolded with setup/hooks.
- Research pointer: `docs/research/omc-omx-mechanism-research-pointer.md`.

### Changed
- `omg-using` router: RESUME.md hard rule; priority includes ultragoal + lifestyle routes.
- README scope honesty updated for resume/wiki/hud (still no Stop hard-pin / full LSP MCP).

### Security
- Fail-closed prose APPROVE when unfenced body negates APPROVE or only fenced APPROVE appears.
- Schema v2 run_id mismatch cannot false-green dual-review verifier artifacts.

## [0.2.6] - 2026-07-20

### Added
- **`omg --madmax`**: OMC-style break-glass host launcher — full-open Grok (`--always-approve` + `--permission-mode bypassPermissions`) in a **new tmux session** each launch (timestamp + nonce).
- Guardrails: subcommand before `--madmax` → exit 2; `--safe` / non-bypass `--permission-mode` → exit 2; root `--yolo` is not a madmax alias (stripped with note).
- Login-shell pane command + `tmux new-session -e` env forward (no secrets in pane start-command text); best-effort DA1 drain.
- Docs: dual-track install, security-model Host launcher section, `docs/RELEASE.md`, CI smoke/e2e.

### Changed
- Hermetic CI runs `scripts/smoke.sh` in addition to pytest.
- Session naming / attach policy: never reattach old madmax sessions (continuity via `grok --continue` / `--resume`).

### Security
- Documented madmax as operator break-glass (not a sandbox); detached sessions remain until `tmux kill-session`.
- Env forward via tmux `-e` (not shell `export` in pane argv).

## [0.2.5] - 2026-07-20

### Added
- Core-purpose parity CLI surfaces (goal ledger, interview, review, UltraQA, autopilot destination gates).
- Open-source packaging: MIT LICENSE, SECURITY, CONTRIBUTING, hermetic GitHub Actions CI.
- Public verification summary under `docs/research/verification-2026-07-20.md`.
- `omg --version` (reads `plugin.json`).
- Dual-track install docs (full vs plugin-only); maintainer release protocol.

### Changed
- README recommends stable home `~/.local/share/oh-my-grok`.
- Live machine evidence no longer shipped; regenerate via `docs/research/live/README.md`.
- Git history scrubbed of home paths and live suite JSON (filter-repo).
- CI runs hermetic smoke/e2e in addition to pytest.

### Security
- Isolation honesty documented in `docs/security-model.md` (capability_mode primary; PreToolUse fail-open soft-gate).
- Global PreToolUse soft-gate install path remains absolute-checkout (re-run `install-plugin.sh` after relocate).
