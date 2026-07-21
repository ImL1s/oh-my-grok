# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Product version source of truth: [`plugin.json`](./plugin.json).

## [Unreleased]

### Planned
- Optional PyPI/`pipx` CLI track — **shipped editable-only** (`pyproject.toml` +
  `pipx install --editable` / `pip install -e .`); non-editable wheel / PyPI
  publish still deferred (`plugin_root()` needs checkout siblings).
- Optional PR to xAI plugin-marketplace (sha-pinned) — **deferred / prep-only**
  (document prerequisites in `docs/RELEASE.md`; do not submit).
- Host Stop veto (not feasible on Grok today).
- Full OMC LSP/AST MCP bridge (local pyright probe only in 0.3.0).

## [0.5.0] - 2026-07-22

Grok-native parity completion: fail-closed hardening, a multi-CLI tmux **team plane**
(D0–D4), and an **in-session MCP server**. Every workstream carries a model-diverse
(Fable 5) adversarial GO plus a REAL/live test pass. The multi-CLI team plane ships behind
an explicit experimental gate; see the blast-radius note in `docs/security-model.md`.

**Live testing earned its keep — it caught THREE integration/wire bugs that unit tests +
adversarial security review all missed:** an MCP NDJSON-vs-Content-Length framing mismatch
(grok timed out connecting), multi-CLI pane prompt-delivery (a real codex pane hung because
its stdin sentinel `-` was never fed), and a team-exec/collect race (collect ran before the
panes sealed). All three found by real `grok`/`codex` in real tmux, fixed, and re-verified live.

### Fixed (fail-closed hardening — each RED→GREEN, each only makes a gate stricter)
- **verdict/ralplan (A2):** `ralplan.verifier_has_approve` raw-`or` across sibling verifier
  artifacts → cross-artifact severity aggregation; `verdict.parse_verdict` folds prose severity
  into step 2 so a fenced-example APPROVE can't short-circuit an unfenced prose REQUEST CHANGES.
- **install classifier (A1):** extracted to an importable, unit-tested
  `scripts/omg_install_classifier.py` (independent candidates, realpath both sides;
  mandatory no-false-positive on genuinely-different paths).
- **doctor --strict (B):** the `spawn_subagent` bare-substring FP on the repo's own CLAUDE.md
  — now matches routing-trigger shape; environmental FAILs stay honest.

### Fixed (live-integration bugs, found by real-CLI smoke)
- **MCP wire framing:** `omg mcp-server` now replies in the client's framing (NDJSON in →
  NDJSON out); grok could not parse the Content-Length reply and timed out.
- **multi-CLI pane prompt delivery:** codex reads the prompt via a stdin redirect; cursor/agy
  get the prompt text (grok's `--prompt-file` unchanged) — a codex pane hung indefinitely before.
- **team-exec race:** the staged pipeline now waits for panes to finish/seal before `collect`
  (bounded by `OMG_TEAM_EXEC_WAIT_SECS`); collect had run before workers sealed → integrate refused.

### Fixed (install security — the global hook could deny EVERY tool call)
- **Root cause (live, 2026-07-22):** the global PreToolUse soft-gate pointed
  `python3 "<checkout>/hooks/bin/pre_tool_use_deny.py"` — a script under
  macOS-TCC-protected `~/Documents` that also `import`ed `omg_cli`. A grok session in
  another workspace (or lacking Documents access) could not `open()` it, so `python3`
  exited **2**; grok reads a PreToolUse exit code of 2 as an *explicit deny*, so it
  blocked every tool call (even `ls`, `spawn_subagent`). The in-code fail-open never
  ran — python could not open the file. Confirmed live and fixed model-diverse
  (Codex gpt-5.6-sol max + Fable 5 design review + a real grok canary).
- **Fix:** a SELF-CONTAINED, stdlib-only standalone (`hooks/bin/omg_pretool_deny_standalone.py`,
  generated from `omg_cli/deny.py` + `_common.hook_disabled` by
  `scripts/generate_standalone_hook.py`, `--check`-guarded in CI) installed under
  `$GROK_HOME/hooks/` (always readable, non-TCC, workspace-independent). It signals
  deny ONLY via stdout JSON (grok honors that regardless of exit code) and **always
  exits 0**; the launcher `python3 -I -S "<abs>" || true` normalizes any
  interpreter/startup failure to fail-**open** (the path is `shlex.quote`d so a
  `$GROK_HOME` with shell metacharacters can't inject an `exit 2`). A live grok 0.2.106
  canary confirmed the hook's deny-JSON-at-rc0 actually blocks the command (parent
  `parent_host_signature=true`, no shim marker written); the spawned child was
  additionally capability-isolated.
- **Install/repair:** one transactional installer (`omg_cli/hook_install.py`) shared by
  `omg setup` (new; end-user path previously installed NO hook) and
  `scripts/install-plugin.sh` (new `omg install-hook` subcommand; `omg setup
  --no-global-hook` opts out). Atomic writes; migrates a prior checkout-path json and
  **quarantines** it to a non-`.json` name on failure ("no hook > broken hook").
  Plugin-bundled `hooks/hooks.json` now points at the standalone too.
- **doctor:** `check_global_pretool_hook` rewritten — realpath-under-`$GROK_HOME`
  (rejects checkout paths + symlink escapes), rejects a 2nd command hook, real `open()`
  + a behavioral subprocess smoke (allow/deny), and a soft freshness check
  (installed-vs-committed hash + TCC-home WARN). `os.access` (TCC-blind false-green)
  removed. GROK_HOME honored consistently across setup/install/doctor/uninstall.

### Added
- **`omg team` — multi-CLI tmux team plane** (behind `OMG_EXPERIMENTAL_TMUX_TEAM=1`): D0 vetted
  executor argv adapters (grok/codex/agy/cursor/gemini) → D1 grok-only start/status/collect/stop
  → D2 staged pipeline (`omg team run`) → D3 per-role multi-CLI executor panes + routing
  (reviewer roles → structured-verdict providers only, **cursor forbidden**; unknown roles
  fail-closed) → D4 dynamic scaling + resume + **ralph composition** (`omg team run --ralph`, a
  bounded loop that NEVER sets verified). `deny.py` strengthened (worker can't launch a team).
  Agent-role parity + machine-readable role taxonomy (F).
- **In-session MCP server (`omg mcp-server`, `grok mcp add`)** — 14 read + non-authoritative-proposal
  tools for Grok-native in-session parity. `verified` stays CLI-only via three fail-closed mechanisms
  (curated allowlist, structural refusal under `OMG_MCP_SERVER=1`, path-confinement). Live-verified.
- **`omg lsp symbols`/`diagnostics` (E):** stdlib-`ast` local probe. **`pyproject.toml` (C):**
  editable-pipx packaging.

### Scope honesty
- The multi-CLI team plane provides **integration isolation, NOT execution isolation**: executor
  panes run with operator-level machine access; only worktree ownership + seal + integrate bound
  what reaches the leader tree, and `verified` stays CLI-only. Per-provider CLI-sandbox enforcement
  is non-uniform (grok/codex CLI-enforced; agy `--sandbox` best-effort; gemini none). See
  `docs/security-model.md`.

## [0.4.3] - 2026-07-21

Local-path install refresh + codebase docs. Merged via PR #4; standing reviewer
(Fable 5) GO on the engineering bar.

### Fixed
- **`install-plugin.sh` force-refreshes a local-path install:** `grok plugin
  update` is a no-op for a local-path (frozen-snapshot) install, so a bumped
  checkout left the installed plugin snapshot stale (caught only by `omg doctor`'s
  version-drift / installed-capabilities-lock checks). The installer now detects a
  same-path install (realpath match) and force-refreshes via `grok plugin
  uninstall … && install`, erroring loudly (exit 1) if the reinstall fails;
  different-path duplicates stay WARN-only.
- **`omg update` surfaces the installer's recovery output:** on a non-zero
  `install-plugin.sh` exit it now forwards the captured stdout+stderr (previously
  it printed only `exited rc=1`, swallowing the reinstall-gap recovery message).

### Added
- **`CLAUDE.md`** — a codebase architecture guide (two-surface design, the Grok
  host contract, `capability_mode` isolation, the two fail-closed security modules
  `verdict.py`/`command_policy.py`, the worker/seal/integrate flow, and the
  version-bump gotchas incl. the `grok plugin update` no-op-for-local-path finding).

### Docs
- README/skills refreshed: the Upgrade note now documents uninstall+reinstall for
  local-path installs, plus `omg worker seal --all` and `omg note --prune`.

## [0.4.2] - 2026-07-21

ULW leader batch seal — closes the ULW→integrate gap the live suite surfaced.
Merged via PR #3; standing reviewer (Fable 5) design bless + implementation GO.

### Added
- **`omg worker seal --all [--force]`:** a leader-side batch seal — one command
  seals every prepared worktree with a real `head_sha` from `git rev-parse HEAD`,
  so real grok ULW sessions stop hand-writing envelopes with invalid head_shas
  (which `omg integrate` correctly refused). A pure driver over the existing
  fail-closed `seal_task`; join's ownership gate and integrate's
  `preflight_clean_tree` are untouched.
  - Fail-closed status discrimination: only a literal "worktree missing" is a
    benign skip; a returned `status="failed"` envelope (head==base / still-dirty)
    surfaces as `failed` (never masked as `sealed`); every other `WorkerError` is
    `error`. The CLI returns nonzero if any task failed/errored.
  - Honest trust boundary: seals only `.omg/worktrees/<run_id>/<validated task_id>`
    for task_ids in a CLI-written manifest (no provenance verification claimed);
    a traversal task_id is rejected by `validate_task_id`.
  - `--force` re-seals a worktree whose head advanced past its recorded head_sha.

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
