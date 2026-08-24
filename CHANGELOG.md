# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Product version source of truth: [`plugin.json`](./plugin.json).

## [Unreleased]

### Changed
- **#147 leftover: TOCTOU promote, task inbox, interactive scale:**
  `input_ready` promotion re-proves pane/PID/start against current
  `team.json` and, on live pid-bound workers, `resolve_live_worker` /
  ExactPaneProof — identity flip between TUI_READY capture and stamp
  refuses the promotion. After proven ready, the leader submits
  `interactive_inbox_instruction` (not the seed / leader transcript)
  via `team input --submit`. Interactive and API seed inboxes are
  attempt-scoped (`{task_id}.a{attempt}.inbox.txt` /
  `{task_id}.a{attempt}.inbox.md`) so relaunch cannot reuse the old
  file; catalog `inbox.md` remains an alias. `omg team scale --add`
  on an interactive TTY team uses the interactive argv/wrapper (never
  supervisor panes); new workers stay `input_ready=false` until their
  own TUI_READY+TOCTOU proof. After a scale/relaunch generation bump,
  unchanged exact identities rebind persisted TUI_READY evidence
  instead of requiring the one-shot marker still in bounded
  scrollback (missing historical `pid_start` refuses rebind).
  Interactive inboxes carry the worker's assignment (owned files,
  subject, depends-on, board task id, worker protocol), not only
  the shared team goal. Interactive scale-up persists and returns
  readiness (`startup_status`); failed/degraded/blocked is not a
  silent success. Scale routing reload accepts persisted `by_role`
  snapshots from `start_team`. Mixed interactive/headless active sets
  fail closed. Fixture `interactive_tty.py` prints `WINCH:` on
  SIGWINCH and `INT:` on SIGINT without killing the leader.
  Default/`auto` stay headless. No `LIVE_TEAM_INTERACTIVE_TTY_OK`
  claim from fixtures. Refs #147 (does not close).

### Added
- **#75 PNG pixel overlay evidence:** `omg visual overlay --reference a.png
  --candidate b.png --json` decodes PNG with stdlib (`struct`/`zlib`; no
  Pillow) and writes `changed_pixels` / `changed_ratio_milli` / `bbox` plus
  a confined `overlay.png` sidecar. Visual Contract V1 stays pixel-agnostic.
  Fail-closed on non-PNG, truncated, oversize, symlink, and path escape.
  `--descriptor-only` keeps sha/byte identity. Never writes `passes` /
  `verified`; JSON never inlines image bytes. Not live screenshot smoke and
  not Antigravity vision. Hermetic: `tests/test_visual_cli.py`,
  `tests/test_visual_pixels.py`. Refs #75 (does not close).
- **#77 leftover migrate/import:** `omg setup import --from PATH` copy-safe
  ingests user artifacts into the versioned install manifest as
  `ownership=imported` with provenance (source posix path, sha256, byte_size,
  imported_at). Never follows symlinks; refuses credential-shaped bytes
  (`api_key`, `sk-`, bearer tokens, private-key PEM). `omg setup migrate --from PATH`
  classifies a legacy GROK_HOME / project `.omg` layout (managed / imported /
  user-owned / foreign) without overwriting user-owned files; apply fails closed
  on foreign/malformed rows. `--dry-run` writes nothing. `omg uninstall --yes`
  preserves manifest-owned paths whose on-disk sha256 drifted and only unlinks
  matching regular files; never deletes project `.omg/state`. File copy is **not**
  live Grok/Antigravity discovery; doctor `observed` / `healthy` / `verified`
  stay false. Does **not** close #77 (live clean-host matrix, doctor live
  evidence, Windows O_NOFOLLOW remain). Refs #77.
- **#69 catalog v6 remaining OMX-named file-store ops:** default Team catalog
  is schema v6 (42 named / 42 dispatched). The leftover reserved names
  (`mailbox-mark-notified`, `write-worker-identity`, `await-event`,
  `read-idle-state`, `read-stall-state`, `cleanup`, `read-monitor-snapshot`,
  `write-monitor-snapshot`, `read-task-approval`, `write-task-approval`) are
  implemented on confined hermetic stores. Mailbox v1 keys stay exact
  (`notify_cursor.json` is a separate per-recipient file). `await-event` is a
  bounded `events.jsonl` snapshot (`timeout_ms=0`; cap 1000ms). Idle/stall
  derive from heartbeat + task timestamps (no live tmux). `cleanup` is
  distinct from `orphan-cleanup` and fail-closes while running or claims
  unexpired. Task approval never writes OMG `verified`. Monitor snapshots are
  schema-versioned and redacted. Worker ACL stays fail-closed. v1–v5 goldens
  unchanged. Does **not** claim live AG, live grok job smoke, host-TUI
  prompt-queue, composition live executors, or Team v3 complete. Refs #69.
- **#72 CLI wrapper events:** `omg_cli.hooks_registry.emit_wrapper_event`
  journals `artifact.created` (classified CLI artifact writes),
  `job.terminal` (jobs runtime terminal status), and
  `team.member.transition` (team API heartbeat/shutdown-ack) with
  `source=wrapper`, schema `omg-wrapper-event/v1`, redaction, and
  post-hoc timeout. `prompt.submit` stays unsupported (no
  UserPromptSubmit inject). Does not set `verified`. Not live AG hook
  install. Does not close #72. Refs #72.
- **#74 leftover ACP session resume CLI:** `omg session acp-resume
  --session-id UUID --cwd PATH` reuses `host_acp.py` initialize +
  `session/resume` and prints a content-free receipt (no transcript).
  Teardown is process-group kill, not ACP `session/close`, and this is
  not the durable jobs sidecar. `--restore-code` is refused (resume ≠
  restore). `OMG_ACP_BIN` may point at the hermetic fake peer; that is
  not live Grok. Live AG history stays unimported. Does not set
  `verified`. Refs #74 (does not close).
- **#134 inspect-absent Medley #18 fallback UX:** Stock Grok Build with unset
  inspect reports `inspect_source=absent` and `attempt=null` on
  `omg agents list|explain --json`. Doctor addendum prints
  `inspect: absent (baseline fallback; Medley #18 not attempted)`. Policy
  next action is `omg agents list --host-inspect PATH`. Candidate ids are
  not consumed and receipts are not invented. A missing inspect *file*
  (path set) still fail-closes `E_MEDLEY_INSPECT_PATH`. Medley caps stay
  **unsupported**; route facts stay **unavailable**; this is not an install
  failure. Does **not** close #134: no live Medley session, no `/agents`
  TUI. Refs #134.
- **#131/#134 Medley inspect glue:** `omg agents list|explain --host-inspect`
  and `OMG_MEDLEY_INSPECT` consume `medley.native-subagent-route.inspect/v1`.
  Support is never inferred from PATH, binary name, or state dirs. Stock Grok
  Build stays the baseline (Medley caps **unsupported**). Doctor routing and
  policy views overlay secret-free receipts when the inspect document
  advertises `medley.native-route-receipt.v1`. Inspect `incompatible` stays
  incompatible even when a version is present; `supported` without a version is
  unavailable; explicit `unknown` is preserved; duplicate capability ids fail
  closed; receipt rows are schema-checked before overlay; diagnostic reason
  text is not treated as a secret. Does **not** close #131/#134: live spawn
  wiring, session-persisted receipts, and Medley `/agents` TUI (#290) remain
  host-owned. Refs #131 #134.
- **#147 grok 1.0.4 composer submit (stacked on TUI_READY wrapper):** Interactive
  grok argv seeds a persistent TUI turn with positional `[PROMPT]`
  `OMG_TEAM_SESSION_START`. grok 1.0.4 has **no** `--prompt` flag (unexpected
  argument; tip suggests `--prompt-file`). `-p` / `--single` / `--prompt-file`
  remain forbidden on the interactive path. Echo-only `--rules` stay
  smoke-probe-only (`OMG_TEAM_INTERACTIVE_ECHO_PROBE`). `omg team input --submit` sends
  literal CR after a bounded settle (two pulses on grok) so composer text is
  not paste-coalesced with Enter. `PROVIDER_ECHO` is still child-produced,
  never wrapper-faked. `LIVE_TEAM_INTERACTIVE_TTY_OK` is claimed only after
  unique stdin→pane echo — not from the positional seed alone. One WSL live
  launch (`team-smoke-20260815T164235Z.json`) submitted `OMG147-LIVE-01548eed17fe`
  through `team input --submit`; grok thought and replied
  `PROVIDER_ECHO: <token>` (space after colon). The first smoke matcher
  required zero space and false-negatived (exit 1); the matcher now allows
  optional ASCII space/tab. That artifact earns `LIVE_TEAM_INTERACTIVE_TTY_OK`.
  Default/`auto` stay headless. Resize/Ctrl-C/scale live AC remains open.
  Refs #147 (does not close).
- **#147 grok 1.0.4 interactive TUI_READY wrapper:** `--io-mode interactive`
  grok panes `exec` `python -m omg_cli.team.interactive_wrapper` on the pane
  TTY (not the Team supervisor). The wrapper prints `TUI_READY:<nonce>` only
  after stdin is a TTY and the grok child has started reading it; spawn-only
  is not ready. `--no-alt-screen --minimal --no-subagents` keep the
  TUI in tmux scrollback. Echo-only `--rules` (PROVIDER_ECHO, no tools) are
  smoke-probe-only (`OMG_TEAM_INTERACTIVE_ECHO_PROBE`), never production
  interactive argv. `PROVIDER_ECHO` is still child-produced (fixture or
  grok reply), never wrapper-faked. One WSL live launch against grok 1.0.4
  (`team-smoke-20260815T145621Z.json`) proved wrapper `TUI_READY` on a real
  TUI (`tui_ready=true`) but **did not** show `PROVIDER_ECHO:` after
  `send-keys` submit — composer local echo only. `LIVE_TEAM_INTERACTIVE_TTY_OK`
  is **not** claimed. Leftover is grok TUI submit/echo, not wrapper
  spawn-only false-green. poll/select/epoll sleep is not stdin-wait unless
  the shared TTY is already raw/noncanonical; zombies are not live.
  aarch64 stdin-wait uses `epoll_pwait` 22 / `epoll_pwait2` 441 (not the
  x86_64 232/281 numbers). Read-family syscalls only count as stdin-wait when
  `arg0 == 0` (they do not fall through to the poll/raw-TTY branch). Wrapper
  timeout signals the child **process group** (SIGTERM then SIGKILL) with
  bounded WNOHANG reaps. Refs #147 (does not close).
- **#69 catalog v5 + grok jobs + Team prompt-queue:** default Team catalog is
  schema v5 (42 named / 32 dispatched). `broadcast` fans out as N
  durable DMs (leader-only; retry-stable per-recipient dedupe). Host prompt-queue consume
  (`enqueue-host-prompt` / `list-host-prompt-queue` /
  `reorder-host-prompt-queue`) is a Team-owned LEGACY file — not mailbox,
  not task ACK, not host-TUI wiring (`grok.prompt_queue.*` stay BLOCKED on
  host probe). Durable Jobs public start admits `--provider grok` (headless
  `--prompt-file` only; fake-only flags denied; Team `--worker-topology=job`
  admits grok). Team job topology stamps each worker's owned worktree as
  Jobs `cwd` (not the leader checkout). `grok` PATH / `OMG_GROK_BIN` entries
  are stored as absolute paths while keeping the `grok` basename. Omitted
  broadcast `dedupe_key` is retry-stable; per-recipient mailbox keys are
  fixed-length hashes (never `{key}--{recipient}` overflow). Host prompt-queue
  load validates full entry shape, `content_hash`, and sequence-vs-order. Omitted broadcast keys
  hash the redacted body (no credential oracle). Grok jobs reject `--effort` /
  `--mode` (not forwarded).
  v1–v4 goldens unchanged.   Antigravity is not installed here;
  no live AG / live grok job smoke. Leftover AC remains on issue 69.
- **Parity coverage_digest after #69 catalog v5 GAPS sync:** refresh OMC/OMX/OmO/Antigravity
  proof `coverage_digest` values only after gap text for catalog v5 + hermetic
  grok jobs (no policy/mapping/status changes). OMC digest refreshed again
  after rewording catalog counts so the release gate does not read
  `implemented` as a maturity claim. Leftover AC remains on issue 69.
- **#70 Wave B/C skill playbooks + catalog-driven routing:**   29 catalog-only
  canonical skills now have real `skills/omg-*/SKILL.md` playbooks (13 Wave B +
  16 Wave C) plus bundled `resources/contract.json` on every plugin skill
  (45 total). Read-write playbooks (TDD/build-fix and peers) may edit their
  slice; they still must never write `passes`/`verified`. Grok `<workflow_routing>` is generated from `skills/catalog.json`
  triggers/aliases (no UserPromptSubmit injector; no second skill list).
  Localized catalog tables: `docs/parity/skills-catalog.zh.md` and
  `skills-catalog.zh-TW.md`. Projection writes are atomic/no-follow and prune
  obsolete AG files. **Not** a live smoke; Antigravity files remain
  projections; `live_verification` stays `unproven`. Does not set `verified`.
  Refs #70 (does not close — no live smoke).
- **#71 agents catalog (YAML + extra roles):** `agents/catalog.yaml` is the
  schema-versioned source of truth; `scripts/generate_agents_catalog.py`
  writes committed `agents/catalog.json` and static Antigravity `agent.md`
  projections (`--check` fails on drift). Adds plugin agents
  `omg-explore` (explore-high profile), `omg-planner`, `omg-tracer`,
  `omg-document-specialist`, `omg-build-fixer`, `omg-git-master`,
  `omg-code-simplifier`, `omg-scientist`, `omg-vision`,
  `omg-product-manager` (23 total) plus catalog aliases (sisyphus,
  hephaestus, librarian, style/quality/api-reviewer, …). Deterministic
  `resolve_category` for quick/deep/ultrabrain/visual-engineering/research/review
  never silently upgrades required read-only to write. Runtime
  `assert_agent_capability` blocks reviewer/verifier/planner `read-write`
  (PreToolUse still fail-open on hook/catalog crash). Bounded
  `render_handoff` (no full leader history; no self-approval). `omg doctor`
  reports missing/stale AG projections; inspect `observed`/`healthy`/`verified`
  stay false. **Not live AG** — Antigravity is not installed; live AG smoke
  remains open. Dual-host overlay stays `agents/model_policies.json` on this
  catalog (inherit/default for new ids). Refs #71 (does not close).
- **#74 session search / friction / replay / observatory:** `omg session
  search|friction|replay|observatory|retain|ag-history` plus `omg trace
  timeline` and `omg memory layers`. Journals under `resolve_state_root()`
  are the source of truth; reports redact credentials and home paths and
  never print raw prompts/responses/tool output. Replay never re-executes
  commands; restore-code refuses unsafe cwd/worktree. Retention skips
  `*.jsonl.lock` and `event-cursors`; event scans keep the newest records
  when the per-store cap is hit. `--project all` is
  required to search sibling stores. AG history is a read-only stub (pin
  `unsupported` / `unknown_version`; never mutates AG files). ACP
  session/resume on the session CLI is `omg session acp-resume` (hermetic
  fake peer; not live Grok; see leftover above). This is not a live smoke.
  **Refs #74 (does not close).**
- **#75 visual capture/verdict/ralph:** `omg visual capture|verdict|ralph`
  plus the existing `compare` wrapper. Capture auto-detects
  `capture.command` then `OMG_VISUAL_CAPTURE`, else **blocked** (not a fake
  pass); Playwright is not required. Verdict wraps `compare()`, writes
  descriptors/findings/score history under `.omg/artifacts/visual/<run_id>/`,
  and records `reviewer_status` from an independent read-only reviewer
  (`E_VISUAL_REVIEWER` if editor==reviewer or reviewer is read-write).
  Overlay sidecars are **descriptor-only** (masks + byte-identity); this
  slice does **not** decode pixels and does **not** call an AG vision model.
  Capture argv redacts the value after sensitive flags (`--token secret`),
  redacts credentials in persisted `capture.target` (query tokens),
  and redacts bounded stderr before it is persisted. Visual Ralph is a bounded evidence loop (repair prompt artifact, no agent
  spawn, no `verified` stamp). Hermetic: `tests/test_visual_cli.py`. Refs #75
  (does not close — no live screenshot smoke / no AG vision model).
- **#76 leftover: `omg edit verify` + simplify apply rollback:** `omg edit verify
  EDIT_ID|--input` re-reads the target through the same `O_NOFOLLOW` walk as
  apply, re-plans, and reports `ok` / stale / conflict JSON without writing the
  file. `omg edit simplify --apply-edits` restores original bytes when a later
  descriptor in that invocation fails; incomplete restore records
  `status=dirty` / `failed` (never `passes`/`verified`). CLI still does not
  call an LLM and does **not** claim `omo.edit.hash_anchored` host parity.
  Hermetic: `tests/test_edit_verify.py`. Refs #76 (does not close).
- **#76 comment checker, simplifier, Team authority:** `omg edit comments`
  (`--input` / `--git-diff` / `--paths`, report-only unless `--fix`) and
  `omg edit simplify` (disabled unless `--enable` or `.omg/simplify.json`;
  CLI never calls an LLM; assignment artifact for `omg-code-simplifier` then
  independent `omg-code-reviewer`). `omg edit apply` refuses
  `OMG_CAPABILITY_MODE=read-only` (`E_READ_ONLY`), ownership-task
  `capability_mode=read-only`, and unowned ULW/Team paths
  when an ownership manifest exists (`E_OWNERSHIP`; host edits still allowed
  with no manifest).   Writes refuse symlink ancestors, a symlinked
  `.omg/state/simplify-guard.json` leaf, and a symlinked
  `.omg/artifacts/edit/` tree. `--apply-edits` requires the CLI `--paths`
  set to match the recorded assignment. Durable redacted artifacts under `.omg/artifacts/edit/`.
  Never writes `passes`/`verified`. Does not claim `omo.edit.hash_anchored`
  host parity. Docs: `docs/hash-edit.md`. Hermetic:
  `tests/test_comment_checker.py`.   Refs #76 (does not close).
- **#77 install manifest (first cut):** `omg setup --runtime grok|antigravity|both`
  `--scope project|user` (defaults remain `grok` + `project`). Versioned
  `.omg/install/manifest.json` (or `~/.omg-user/` for user scope) records
  managed artifacts, classifies missing/exact/stale/user-owned/foreign, preserves
  foreign/user-owned unless `--force`, and rolls back interrupted transactions.
  Refuses to invent project `.omg` in `$HOME` without `--here`. `omg doctor`
  reports manifest drift; JSON `host` now separates `binary` / `version` /
  `auth` / `capabilities` / `compatibility` / `live_evidence` (`auth.ok` and
  `live_evidence` stay false — invalid keys cannot false-green). File copy is
  not live AG/Grok verification. Refs #77 (does not close).
- **#77 leftover AC (install transaction):** `omg setup` no longer calls
  legacy `run_setup` beside the manifest; AGENTS.md merge, generic `.omg`
  gitignore init (including `--runtime antigravity`), and optional machine-scoped
  grok rules/hook run inside the same backup/rollback transaction.
  Directories occupying a managed path classify `foreign` (`--force` refuses
  to write onto them). User-scope `user.manifest.marker` is a state marker and
  does not make doctor `enabled`/`loadable`. `desired_artifacts()` ids must
  match frozen `EXPECTED_IDS_BY_RUNTIME_SCOPE`. POSIX `ensure_omg_dirs`
  confinement failures stay fail-closed (no symlink-following mkdir fallback).
  Malformed global hook JSON is repaired/quarantined without `--force`.
  Foreign hook JSON (including dangling symlinks) is reconciled by
  `install_global_hook` rather than skipped. Quarantined hooks — including
  `failed:*` after a successful rename — are not restored onto grok's
  `*.json` discovery path when the transaction rolls back. POSIX confinement
  failures keep `E_PATH` after rollback. File copy is still not live
  Grok/Antigravity discovery (`verified`/`observed`/`healthy` stay false).
  Refs #77 (does not close).
- **#73 tools sidecar (first cut):** `omg tools doctor|serve|lsp|ast|codegraph|research`
  is an OMG-owned sidecar (`omg_cli/tools_sidecar.py`). Semantic LSP uses an
  explicit transport (fake protocol or `--lsp-command`); it is **not**
  Grok-native and does **not** change `omg lsp` (`E_LSP_HOST_OWNED`).
  `omg mcp-server` still forbids `lsp.*`. Missing ast-grep is blocked, not
  faked; replace defaults to dry-run. CodeGraph modes `off|auto|shared|local`
  label branch accuracy and never claim a shared index has worktree dirt.
  Network research is opt-in (`OMG_TOOLS_NETWORK=1`) with no bundled
  credentials. MCP image results are bounded descriptors (no raw bytes in
  state). Antigravity files under
  `docs/parity/projections/antigravity/mcp/` are **not** an installed AG
  plugin and **not** live AG evidence. Does not set `verified`. Refs #73
  (does not close).
- **#73 tools sidecar (follow-up):** Discover cargo `ast-grep` (identity-checked;
  ignore shadow-utils `sg`). `--lsp-command` no longer swallows server flags —
  pass them after `--` (e.g. `-- --stdio`). `code_action` sends the required
  LSP `range`. Disk edits after `didOpen` send `textDocument/didChange`.
  Minimal **local** CodeGraph import/symbol indexer
  (`omg tools codegraph index`; not SCIP; shared indexes are not
  branch-accurate on dirty worktrees). MCP `codegraph.index` requires
  `capability_mode=read-write`. Network research still has no provider.
  Doctor inventory never marks a detected language server `ready` until a
  session is actually started. Not live Antigravity MCP. Does not set
  `verified`. Refs #73 (does not close).
- **#72 lifecycle bus (journal + allowlist):** in-process dispatcher now
  fail-closes registry load when `host_hook` is outside the event allowlist
  or when bundled security ids (`omg.pretool.deny`, `omg.stop.gate`,
  `omg.continuation.guard`) are omitted, disabled, rebound to the wrong
  event/projection, or have their required `fail_policy` rewritten
  (continuation stays fail-closed; PreToolUse/Stop stay fail-open;
  test stubs may pass `allow_incomplete=True`). `dispatch()` appends a bounded redacted
  JSONL row via `omg_cli/runtime_events.py` with a monotonic
  per-root sequence; journal write failures fail open. Globally disabled
  buses (`OMG_DISABLE_HOOKS` / `DISABLE_OMG`) skip journaling. `duration_ms` is
  always recorded; timeout is **post-hoc** after the synchronous handler
  returns (Python cannot preempt). Antigravity `hooks.json` + README under
  `docs/parity/projections/antigravity/hooks/` plus
  `install_antigravity_hook_projection(dest)` for a later #77 install path
  — static projection only, not live AG / not `agy` loaded hooks. Does not
  invent UserPromptSubmit inject. Does not set `verified`. Refs #72
  (does not close).
- **#72 lifecycle registry (first cut):** host-neutral hook registry
  `hooks/registry.json` plus in-process dispatcher
  `omg_cli/hooks_registry.py`. Grok mappings are explicit:
  `PreToolUse`/`Stop` may block; `SessionStart`/`SubagentStop` are passive;
  `UserPromptSubmit` injection is **unsupported**. Dispatcher honors
  `OMG_DISABLE_HOOKS` / `DISABLE_OMG` / `OMG_SKIP_HOOKS`, bounded untrusted
  output, fail-open crashes, continuation
  `refuse`/`adopt_existing`/`artifact_only`, and ids-only compact handoff
  (no transcript). Existing `deny.py` / `stop_gate.py` behavior is delegated
  unchanged. Antigravity files under
  `docs/parity/projections/antigravity/hooks/` are **not** an installed AG
  plugin and **not** live AG evidence. Does not set `verified`. Refs #72
  (does not close).
- **#69 PR14 Fixture-backed Composition Execution V1:** leader-only
  `execute_composition_tasks_v1` runs admitted Hyperplan / Security Research
  lanes through the existing claim-lane / submit-lane-result protocol with
  **fixture** workers, then collect-tasks, and persists
  `omg.team.composition_execution_v1` **last**.
  `execution_supported=true` is allowed **only** on that evidence document
  and only with worker evidence (run ids, `fx-{worker_id}` pane ids, lane
  result / claim digests). Forged `{execution_supported:true}` without
  evidence is refused. Compile / produce / admit / collect / claim-lane
  contracts keep `execution_supported=false`. Executor is fixture-only
  (grok / agy / antigravity / cursor auto-execution fail closed). Job
  topology, PoC, tmux, Jobs, MCP, and `live_*` remain out of scope. No
  catalog v5. CLI: `omg team hyperplan|security-research execute --run RUN
  --team-id TEAM --executor fixture --input BUNDLE.json`. Hermetic coverage
  in `tests/test_team_composition_execution.py`. Does **not** close #69
  (authenticated Antigravity live evidence / Team job-backed live workers /
  host prompt-queue consume remain open). Refs #69.
- **#134 Grok-side dual-host agent-routing UX:** host-neutral
  `AgentPolicyViewV1` human layouts (narrow/normal/wide, `NO_COLOR`, CJK
  display width), doctor routing addendum, Team presentation human route-kind
  labels, and adapter schema `docs/schemas/omg.agent_policy_view.v1.json`.
  Locked `omg team status --json` keys are unchanged. Medley TUI/#290 remains
  a Ref — no native-host TUI claimed. Refs #134 (does not close).
- **#131 Grok-side dual-host agent/model policy (runtime):** versioned
  capability registry (`omg_cli/host_capabilities.py`) with outcomes
  supported/unsupported/unavailable/incompatible/unknown. Policy overlay
  `agents/model_policies.json` consumes the #71 catalog (not a second
  registry). Stock Grok Build uses explicit inherit; exact never silently
  becomes the parent model; Medley extensions are not flattened to the
  first catalog id. Native vs `external_executor` route schemas are
  distinct. `omg agents list|explain` is a read-only inspect surface
  (JSON + human). `omg doctor` reports the registry as a soft OK when
  Medley caps are unsupported. Medley #287/#290   remain Refs — no receipts,
  ordered-candidate runtime, or TUI parity claimed. Refs #131 (does not close).
- **#70 skill catalog (Wave A):** read-only machine catalog
  `skills/catalog.json` (16 Grok plugin playbooks plus classified
  aliases/catalog-only workflows from the #70 minimum set). Fail-closed
  loader `omg_cli/skills_catalog.py` (missing SKILL.md, duplicate id,
  host-native shadowing, `execute`/`all` capability_mode, `verified:true`
  without live evidence, path-traversal resources). Continuation policy
  `refuse` / `adopt_existing` / `artifact_only`. `omg skill
  list|show|resolve|resources` inspects the catalog and never sets
  `verified`. `omg capabilities` embeds `skills_catalog`. Static
  Antigravity projections under
  `docs/parity/projections/antigravity/skills/` are **not** an installed
  AG plugin and **not** live AG evidence.   New in-session playbooks,
  Wave B/C runtimes, and live promotion remain open. `omg skill list`
  exits 1 on a fail-closed catalog; `omg skill show` preserves alias
  rows; trigger resolve uses token/phrase boundaries. Refs #70 (does not
  close).

- **#71 agent catalog (PR slice):** read-only machine catalog
  `agents/catalog.json` (13 `omg-*` agents: id, file, capability_mode,
  permission_mode, tier, spawn policy, Grok plugin + Antigravity
  `agent.md` projection targets). Fail-closed loader
  `omg_cli/agents_catalog.py` (missing agent, duplicate id, capability_mode
  outside `{read-only, read-write}`; never `execute`/`all`). `omg
  capabilities` inspects `agents_catalog` (does not register `omg agents`).
  Static Antigravity projections under
  `docs/parity/projections/antigravity/agents/` are **not** an installed AG
  plugin and **not** live AG evidence. Dual-host routing (#131) must consume
  this catalog. OmO discipline routing engine and live AG projection install
  remain open. Refs #71 (does not close).

### Fixed
- **#77 leftover:** `omg uninstall --yes` no longer skips legacy global
  hook removal just because *any* install manifest exists. `remove_global_hook`
  is deferred only when the owned plan lists `user.grok.hook` (remove or
  preserve). A skill-only import manifest still unlinks
  `{GROK_HOME}/hooks/omg-pretool-deny.json`; drifted owned hooks stay
  preserved. Refs #77.
- **#73 tools sidecar leftovers:** `StdioLspTransport` answers server
  `workspace/configuration` requests with empty settings (not dropped by
  id mismatch) so hover/definition can complete. `omg tools doctor` emits
  `failure("tools.doctor", …)` with exit 1 when inner `ok` is false (outer
  envelope matches inner; never `verified`/`observed`/`healthy` as live AG
  evidence). `didOpen` stamps `truncated: true` and refuses document
  semantic ops (`E_LSP_TRUNCATED`) instead of analyzing a prefix.
  Refs #73 (does not close).
- **Team stop after headless grok exit:** `team stop` no longer treats a
  rebound tmux pane PID (shell after the receipted grok process exited) as
  either a signal target *or* a reason to skip `kill-session`. If the
  receipted pid/pgid are proven gone and the session/nonce are still owned
  by this Team, disappearance is verified and the **owned** session is torn
  down. Signalling a live pid that is not the receipted one remains refused.
  Does **not** claim `LIVE_TEAM_SMOKE_OK` (needs a live re-run). Refs #69
  (does not close).
- **Hook interpreter shims:** `python3_executable()` rejects pyenv/asdf
  shims when `/usr/bin/python3` (and other durable candidates) are absent,
  so install/doctor do not persist a cwd-dependent `.python-version` shim
  that later fail-opens via `|| true`. Custom `PYENV_ROOT` / `ASDF_DATA_DIR`
  shim dirs are rejected as well, as is a durable-looking launcher that is a
  symlink to such a shim (one `readlink` hop; Homebrew Cellar stays
  un-resolved). A failed repair that cannot replace
  an already-installed shim wrapper quarantines the active hook JSON
  instead of leaving the shim live. Refs #79 (does not close).
- **Grok 1.0.4 PreToolUse execvp:** grok 1.0.4 `execvp()`s the hook
  `command` string as argv0 (no shell). The previous
  `python3 -I -S "<abs>" || true` launcher was `ENOENT`, fail-opened, then
  headless `-p` hit `PermissionCancelled` so live canary never saw a deny
  (`DENIED_PARTIAL`). Install an executable `$GROK_HOME/hooks/omg_pretool_deny`
  wrapper (LF shebang, absolute python3, `|| true` inside) and point JSON at
  that path. Doctor smokes via execvp, not `/bin/sh -c`. Live canary passes
  `--cwd` at the isolated temp work dir (typically `/tmp`), not the product
  checkout: grok 1.0.4 also ENOENTs hook spawn when `--cwd` is a 9p/drvfs
  mount (WSL `/mnt/d/…`), and a checkout cwd lets the model quote `deny.py`.
  `deny.py` unchanged. Live team smoke `--live` passes `--yolo` so grok
  1.0.4 headless can run `claim-task` (otherwise `PermissionCancelled`,
  board stays `pending`, `mailbox_ack=0`). Board ids were already in the
  spawn prompt after #190. Doctor compares wrapper bytes to
  `render_wrapper`; setup receipts and uninstall rollback include the
  wrapper so a failed uninstall cannot restore JSON that points at a
  missing executable. `python3_executable()` prefers a durable system
  interpreter (`/usr/bin/python3`, Homebrew/local `bin/python3`) over the
  caller's venv `PATH`, and does not `Path.resolve()` through Cellar
  inodes.   Staging smoke uses that same interpreter (not a bare `python3`
  on PATH). Isolation tests authorize that durable interpreter for
  staged smoke (inode + argv), not only a bare `python3` on PATH, and
  hash wrapper bytes before authorizing execvp. Exact-idempotent setup
  publishes a new receipt when hook reconciliation changes receipt-owned
  wrapper/JSON/standalone bytes, so uninstall does not treat a repaired
  wrapper as foreign drift.
  Refs #79 (does not close).
- **Live WSL evidence (2026-08-15):** PATH `omg` shebang stays LF across
  Windows autocrlf (`bin/omg` / `scripts/*.sh` `eol=lf` plus installer CR
  strip). Project-root **and** state-root discovery fail-close on unrelated
  ancestor `.omg` (leftover `/tmp/.omg` must not steal `/tmp/omg-live-*`
  or nested team worktrees). `live_suite.sh` pins `OMG_PROJECT_ROOT` +
  `omg setup --here` outside `/tmp`. Team shorthand seeds the API board
  **before** pane spawn and puts exact `claim-task` /
  `transition-task-status` CLI (with control-plane retry) in the
  single-turn prompt. Staged/installed launcher identity is hashed raw
  after CR-strip copy. Canary parent prompt asks for a verbatim hook
  reason (classifier unchanged). Injected `TMPDIR`/`TMP`/`TEMP` classify
  shared-temp roots (process env is not leaked into `env={}`). Reused
  `--run` rolls back only board task ids this launch returned and does
  not restore unrelated pre-existing `task-*` files. Partial API seeds
  unlink their own files.
  Staged/installed identity readback hashes launcher bytes raw (CRLF
  cannot share the LF digest). Fallback `list-tasks` includes `--input`
  with run/team identity and does not require a claim when no board
  task is bound. Does not claim `LIVE_TEAM_SMOKE_OK` /
  `LIVE_TEAM_INTERACTIVE_TTY_OK`.
  Refs #69 #147 #79 (does not close).
- **Leftover Codex reviews (#177/#178/#179/#180):** project-local `.omg/state`
  stays confined while leftover writers remain; agent frontmatter must match
  catalog `capabilityMode`/`permissionMode` (camelCase keys required;
  snake_case aliases rejected); `omg edit plan` uses the apply
  `O_NOFOLLOW` walk (fail-closed without `dir_fd`) and a size bound before
  load; apply JSON omits local `--input` paths; missing `--input` emits
  `E_HASH_EDIT_USAGE`; leftover `modes.py` run-dir/PRD writers use
  `ensure_managed_dir` / `atomic_write_bytes`; `omg visual compare`
  sanitizes contract errors and bounds JSON load size. Plan/apply reads
  accumulate `os.read` until EOF; catalog agent files are pinned with
  `O_NOFOLLOW` + `O_NONBLOCK` before frontmatter validation. Refs #71 #74 #75
  #76 (does not close).
- **#77 Codex review:** mergeable `AGENTS.md` records a post-setup
  `content_hash` so inspect is not immediately stale; manifest writes
  replace (never follow) a symlink; a failed commit-marker rolls the
  manifest back with the transaction; claimed symlink artifacts count as
  drift; `omg doctor` probes user-scope `~/.omg-user` as well as the
  project manifest. Refs #77.
- **#77 Codex review:** rollback restores only paths under the install root
  and the expected `tx/<id>` backup directory; parent-directory symlinks
  (including `.omg`) are refused; preserved user-owned/foreign rows drop
  their managed hash so doctor is not immediately stale; overwrites larger
  than the backup cap fail closed. Rollback also refuses targets whose
  parent is a symlink. Inspect fails closed on `{}` / missing schema and
  on a symlinked `.omg` parent. File-backup restore refuses a symlink
  leaf (does not follow it). Empty `artifacts` is not installed. Rollback
  restores only writable catalog paths (not `.git/config`). All-preserved
  installs report `enabled=false`. Commit-marker writes are atomic; a
  truncated marker still rolls back via in-memory fallback. Refs #77.
- **#77 Codex review:** inspect compares recorded `content_hash` (hash
  mismatch is stale, not user_owned false-green); rollback unlinks files
  created in a failed transaction; `--force` replaces symlinks instead of
  following them; `omg setup --scope user` skips project-root discovery;
  `--runtime antigravity` does not run legacy Grok global setup. Refs #77.
- **#73 Codex review:** MCP `capability_mode` cannot escalate above the
  server ceiling; hover/definition/rename forward `--line`/`--character`;
  `didOpen` is per URI; missing `--lsp-command` raises `E_LSP_COMMAND`.
  Refs #73.
- **#73 Codex review:** stdio LSP reads the raw pipe (leftover-aware,
  no buffered `select` false-timeout), waits for the matching JSON-RPC
  `id`, and only treats `sg` as ast-grep after an identity probe.
  Refs #73.
- **#73 Codex review:** sidecar LSP sends `initialize`/`initialized`/`didOpen`
  before semantic requests; `omg tools serve --stdio` accepts
  `--lsp-command`/`--fake-lsp`; stdio reads honor the timeout without
  blocking forever; `--apply` fail-closes (`E_LSP_APPLY_UNSUPPORTED`)
  instead of claiming a write. Refs #73.
- **#72 Codex review:** `omg doctor` prints hooks-registry `installed`
  and `enabled` so `OMG_DISABLE_HOOKS` / `DISABLE_OMG` is visible (not
  only `omg capabilities`). Refs #72.
- **#72 Codex review:** Grok `host_capability` must match `GROK_EVENT_MAP`
  exactly (no native_passive/native_blocking swap); `omg capabilities`
  reports hooks `enabled: false` when `OMG_DISABLE_HOOKS`/`DISABLE_OMG` is
  set. `OMG_SKIP_HOOKS` honors legacy `stop`/`pre_tool_use` names; aggregate
  budget exhaustion fail-closes remaining `fail-closed` hooks. Continuation
  guard delegates to `skills_catalog.resolve_continuation`. Compact handoff
  uses no-follow managed writes. In-process handler deadlines remain
  cooperative (post-return). Refs #72.
- **#131 Codex review (follow-up):** `omg agents list` model-intent uses the
  requested extension's `host_capabilities` state, not the aggregate
  `medley_capability_outcome`, so mixed Medley caps cannot advertise
  candidate ids for an unauthorized route. Refs #131 (does not close).
- **#134 Codex review (follow-up):** `wrap_display` continues oversized tokens
  such as `policy_digest` onto the next line instead of truncating them;
  `omg team status --presentation` uses the stacked layout when the member
  table cannot fit the terminal. Refs #134 (does not close).
- **#134 Codex review:** `omg agents explain --json` leaves `effective_route`
  null until a receipt or negotiated model exists; `--width` wraps free-form
  reason/action lines; Team status omits `route=unknown` on locked tasks;
  presentation human copy honors terminal width; adapter schema requires the
  full typed view (nullable `effective_route` included). Refs #134.
- **#70 Codex review:** `omg skill list` exits 1 on a fail-closed catalog
  load; `omg skill show` looks up exact ids (alias rows keep `kind: alias`);
  short trigger matching requires token boundaries so `task`/`steam` no
  longer resolve to `omg-ask`/`omg-team`. Resource resolve inspects original
  path components for symlinks, empty resource allowlists deny undeclared
  files, and embedded NUL / non-UTF-8 catalog bytes raise
  `SkillsCatalogError`. Refs #70.
- **#131 Codex review:** stock-host `omg agents list` shows inherit (plus
  unsupported/unavailable) instead of Medley candidate ids; overrides
  reject unimplemented `models`; external executor routes reject mixed
  native/unknown fields. Refs #131.
- **#69 Codex review:** idempotent execute also binds stored worker
  evidence to the admitted `topo_order` / lane→task mapping, so a
  truncated or wrong-`task_id` artifact cannot false-green as
  `execution_supported=true`. Refs #69 (does not close).
- **#69 PR14 execute `--input` fail-closed:** fixture execute now
  normalizes the composition ResultBundleV1 (foreign writer, claimed
  digest, artifact_kind, exact keys) before lane submit, and idempotent
  re-execute conflicts when per-lane result digests differ. Interrupted
  partial execute remains refuse-until-repair (not auto-resume). Refs #69
  (does not close).
- **Capabilities lock LF-canonical hashes:** `generate_capabilities_lock.py`
  hashes skill/agent/source bytes after CRLF/CR → LF so a Windows
  `core.autocrlf` checkout matches Linux CI `--check`. Lock JSON is written
  with Unix newlines. Refs #69 (does not close).
- **Parity OMC/OmO coverage_digest after #69 PR14 GAPS sync:** refresh proof
  `coverage_digest` values only after gap.team.v3 +
  `omo.team.hyperplan_security` text update for fixture-backed composition
  execution (no policy/mapping/status changes). Refs #69 (does not close).
- **#147 Codex review:** `omg team scale --add` refuses interactive TTY
  teams until scale-up materializes the same I/O mode (it currently
  stamps headless supervisor panes). Refs #147.
- **#147 Codex review:** interactive Grok argv now honors `--safe` /
  `--yolo` the same way as headless D1 (default no longer injects
  `bypassPermissions`; `--yolo` also adds `--always-approve`), passes the
  routed model, refuses symlink inbox / exec-wrapper destinations, snapshots
  those artifacts for `--run` rollback, and threads
  `ExactPaneProof.tmux_socket_path` through liveness probes (not only the
  final `send-keys`). `team start --plan-only` includes `io_mode` like
  `team launch`. Resume/relaunch of an interactive worker demotes
  `input_ready` and requires attempt-bound `TUI_READY` evidence again
  before operator `input`/`key`. `team focus` does not claim
  `focused=true` when `$TMUX` is a different server than
  `proof.tmux_socket_path`. Real-Grok `TUI_READY` / PROVIDER_ECHO remains
  open #147 work. Refs #147.
- **#147 Codex review:** interactive inbox is published with atomic
  `0600` (no umask-window `write_text` then chmod). Exec argv rejects the
  `--prompt-file` / `--prompt-file=` option tokens, not path substrings.
  Refs #147.
- **#147 AG skill projection:** regenerate
  `docs/parity/projections/antigravity/skills/omg-team/SKILL.md` after
  `--io-mode` skill text (static #70 projection; not live AG evidence).
  Refs #147.
- **#147 interactive TTY fixture:** drain PTY startup junk (stray CR / DA1)
  and ignore CSI-only/empty lines before treating a TTY read as provider
  consume, so `PROVIDER_ECHO` is the operator payload. After echo the
  fixture lingers (`OMG_TEAM_PROVIDER_LINGER_S`, default 5s) so macOS
  tmux 3.7 can still capture the marker before the pane is destroyed.
  Status/resume liveness mocks accept `socket_path`. Refs #147.
- **#146 PR3 installed-plugin Team routing smoke:** `omg doctor`'s global
  PreToolUse hard check now smoke-allows first-party `omg team` (bare and
  path-prefixed) so a pre-fix hook that still classifies Team as an external
  CLI cannot pass. Isolated install + PATH-basename `omg team` tests prove
  slash-skill → bare CLI routing, nested-launch zero side effects, and
  foreign CLI deny. Refs #146.
- **#147 ExactPaneProof tmux socket:** `resolve_live_worker` binds
  `tmux_socket_path` onto `ExactPaneProof` so operator `input`/`key`/`focus`
  (and capture / attach argv) can pin `tmux -S`. The prior pin called
  `proof.tmux_socket_path` on a type that lacked the field. Refs #147.
- **#169 PR1 identity-safe release upload:** publish no longer uses
  `gh release upload --clobber`. `scripts/release_upload_assets.py` +
  `omg_cli.release_upload.plan_release_asset_upload` skip only when remote
  digest matches; length/digest mismatch fails closed.

### Added
- **#75 visual CLI compare:** public `omg visual compare --input <json>` wraps `compare()` only and emits a scored/blocked JSON envelope. Callers compare `aggregate` to `threshold`; the CLI never writes `passes`/`verified`, never decodes images, and never talks to agents. Capture adapters, overlay/diff, independent reviewers, and screenshot Ralph remain later #75 work. Parity `omc.quality.visual_release` / `omx.quality.visual_modes` stay catalogued/partial. Hermetic: `tests/test_visual_cli.py`. Refs #75 (does not close).
- **#76 public hash-edit CLI:** `omg edit plan|apply --input <descriptor.json>`
  wraps the V1 library. `plan` is read-only; `apply` calls `apply_hash_edit`
  (re-read, re-plan, splice at offsets, atomic replace) and never
  `patch(1)` the unified diff. JSON apply envelopes are copy-safe (no raw
  source/replacement/diff text). Stale/ambiguous/path errors fail closed
  with stable `E_HASH_EDIT_*` codes. Does not write `passes`/`verified` or
  claim `omo.edit.hash_anchored` host parity. Docs: `docs/hash-edit.md`.
  Hermetic: `tests/test_hash_edit_cli.py`. Refs #76 (does not close).
- **#169 PR2 canonical bundle/evidence producers:** `omg parity release-bundle`
  writes the documented `release-bundle-manifest.json` layout;
  `omg parity release-evidence` is the only constructor for
  `release-evidence-input.json`. Release workflow requires an annotated
  `vX.Y.Z` tag, versioned CHANGELOG notes, public latest install/doctor
  probe (no credentials in evidence), hashed GitHub remotes (never local
  identity fallback), and fail-closed fake-GitHub retry semantics.
  `finalize-release` still requires a `release_active` run (`.omg/` is
  gitignored; CI does not invent one). Server-side `main` / `v*` protection
  is recorded honestly (`claimed=false` when `gh api` is unavailable).
  Publication facts are uploaded as a workflow artifact. Does **not** close
  #169 until a tagged publish produces completion evidence and protection
  readback on `main`.
- **#147 PR2 follow-up interactive TUI-ready gate:** `--io-mode interactive`
  launch/start waits for `TUI_READY:<nonce>` on the pane TTY (bounded by
  `OMG_TEAM_READY_TIMEOUT_MS`; `--no-wait` stays `unverified_start`) instead
  of supervisor ACK receipts. Only the leader CLI promotes `input_ready`
  after that proof; workers/descriptors never self-promote from stdout
  scrape. Timeout fails closed with no silent headless downgrade.
  Operator `input`/`key` pin `tmux -S` to the team's socket (isolated
  servers do not depend on ambient `TMUX`). Default/`auto` stay headless.
  Does **not** claim `LIVE_TEAM_INTERACTIVE_TTY_OK`. Refs #147.
- **#147 PR2 direct-exec interactive pane:** `--io-mode interactive` on
  `omg team launch`/`start` execs grok or the TTY fixture in the pane (0700
  wrapper, no `--prompt-file`, no supervisor between pane and provider).
  Default/`auto`/`headless` stay on the supervisor path. Explicit interactive
  never silently downgrades (job topology and unqualified providers fail
  closed). `input_ready` stays false until TUI-ready evidence. Live Grok
  marker `LIVE_TEAM_INTERACTIVE_TTY_OK` is still optional. Refs #147.
- **#147 PR1 Team worker I/O capability (fail-closed):** CLI-authoritative
  `io_mode` / `provider_tty_owner` / `input_ready` / `operator_input_supported`
  / `interaction_evidence` independent of pane/job topology. New supervisor
  panes stamp `headless_stream` + `supervisor` + unsupported/not-ready;
  job topology stamps `background_job`. Legacy/missing rows normalize to
  unproven/unsupported. `omg team input` / `key` refuse with stable
  `E_OPERATOR_INPUT_UNSUPPORTED` / `E_OPERATOR_KEY_UNSUPPORTED` /
  `E_OPERATOR_INPUT_NOT_READY` **before** any tmux send; `--operator-override`
  bypasses only CLI TTY policy. Success shape uses
  `submitted_to_exact_tty` / `acknowledged_by_provider` (no `delivered:true`
  overclaim). Aggregate status / presentation / human table project I/O
  honesty without changing frozen `status_locked_view` keys. Real-tmux
  tests assert headless refuse (local echo never proves provider
  consumption). **Does not** close #147: no direct-exec interactive Grok,
  no multi-provider interactive parity, no `interactive_tty` ownership
  (PR2+). Issue synonym note: plan public codes are `E_OPERATOR_*` (not
  `E_TEAM_INPUT_UNSUPPORTED`). Refs #147.

### Fixed
- **#146 first-party `omg team` routing:** PreToolUse soft-gate no longer
  classifies first-party `omg team …` as an external agent CLI. Executable
  heads are basename-normalized across bare, absolute/relative path,
  `env`/`command`/`exec`/`nice`/`nohup` wrappers, and `sh|bash|zsh -c/-lc`
  recursion so path-prefixed forms match bare forms. Foreign
  `omc team` / `claude`/`codex`/`omx`/`agy`/`cursor-agent`/`kimi` stay
  denied. Nested Team launch in a worker process env is refused with
  `E_TEAM_NESTED_LAUNCH` (hook defense-in-depth + runtime before side
  effects); identity-bound `omg team api` reaches runtime validation.
  Command-text env assignments never authorize. Standalone hook regenerated.

### Added
- **#74 PR2 core run-state writer cutover:** `omg_cli/state.py`
  writers for `runs/`, `active.json`, `create.lock`, and status
  `passes`/`verified` fields use `resolve_state_root(...).state_dir`
  so `OMG_STATE_DIR` and workspace-marker scopes receive CLI-written
  run state. Project identity is unchanged. No `omg --state-dir`.
  Team/wiki/workers/workflows, session search/replay, ACP, and wiki
  HUD still use `<project_root>/.omg`. Resolver still does not mkdir.
  `verified` is still only written via `set_verified` / `omg accept`.
  Docs: `docs/state-root.md`. **Refs #74 (does not close)**.
- **#75 PR-A Visual Contract V1:** pure library `omg_cli.contracts.visual_contract` — copy-safe comparison schema, scores, digests; status only `scored`/`blocked`; never emits `approved`/`passes`/`verified` or image bytes. No screenshot capture, agent loop, or `.omg/state` writer. Docs: `docs/visual-contract-v1.md`. Hermetic: `tests/test_visual_contract.py`. Refs #75 (does not close).
- **#74 PR1 canonical state-root contract:** pure resolver
  `omg_cli.state_root.resolve_state_root` (API/env only:
  `OMG_STATE_DIR`, `OMG_WORKSPACE_MARKER`,
  `OMG_DISABLE_WORKSPACE_MARKER`). Scopes `per_worktree` |
  `workspace_shared` | `centralized`. PR1 shipped the resolver only
  (no writer cutover, no CLI flags, no mkdir/write). Hermetic coverage
  in `tests/test_state_root.py`.
  Docs: `docs/state-root.md`, `docs/project-root.md`. **Refs #74
  (does not close)**.
- **#76 PR1 versioned hash-anchored edit core:** library-only
  `omg_cli.hash_edit` with a strict V1 descriptor
  (`parse_hash_edit_descriptor`), a pure planner (`plan_hash_edit`;
  caller-supplied current bytes; exact text+context only), and confined
  atomic apply (`apply_hash_edit`; workspace-root fd walk, parent-dir
  flock, `atomic_write_bytes_at` + readback, preserve `stat.S_IMODE`).
  This **supplements** host-native edits; it does **not** make unobserved
  host edits hash-anchored. No public CLI, Team/read-only authority,
  comment hygiene, MCP, or `.omg/state` writer. A protocol claim requires
  a successful `apply_hash_edit` result. Docs: `docs/hash-edit.md`.
  Hermetic coverage in `tests/test_hash_edit_descriptor.py`,
  `tests/test_hash_edit_planner.py`, `tests/test_hash_edit_apply.py`.
  Refs #76 (does not close).

### Fixed
- **#138 Slice A / PR #161:** `normalize_ask_argv` hoists options that sit
  between `ask` positionals so CPython 3.11–3.13 accept
  `omg ask explain --json fable` (and `ask provider --timeout N prompt`)
  instead of `unrecognized arguments`. Does not close #138.
- **#138 Slice A:** Public consultation strings also reject `file://` private paths (`file:///tmp`, `file:///private/tmp`, `file:///Users`, `file:///C:/`) by treating `://` and extra `/` as path delimiters. Relative `docs/tmp` stays copy-safe. Does not close #138.
- **#138 Slice A:** Public consultation/council strings reject copy-unsafe secrets and private paths (redaction delta, `/tmp`/`HOME`/UNC, `token=sk-*`, Authorization/Cookie) instead of best-effort redact. Does not close #138.
- **#138 Slice A:** Legacy ask mapper rejects contradictory taxonomy/flags and nested `advisor_route` facts instead of silently rewriting them; only genuine write_ask_meta v1 providers map. Does not close #138.
- **#138 Slice A:** Consultation v1 rejects `qualified` on attempt/receipt/view, rejects structured output and advisor synthesis (harnesses unproven), requires `succeeded` ⇒ `exit_class=ok`, binds every duplicated attempt/receipt fact, and derives view output from `response_digest`. Exit-0 empty output is allowed. Does not close #138.
- **#138 Slice A:** Capabilities lock binds the canonical advisor registry (`advisor_catalog` from the six unproven specs) and isolates `providers.py` structured-verdict routing under `legacy_ask_execution` (not qualification/support). Registry byte drift fails `--check`. Does not close #138.
- **#138 Slice A:** `omg ask list-advisors` / `explain` reject every ask execution option by explicit presence (including explicit defaults) in either ordering, reject `--` extras, emit JSON `E_USAGE` on usage exit 2, and include `E_ADVISOR_NOT_FOUND` on human unknown. Does not close #138.
- **#138 Slice A:** Council receipt and view share one count/status invariant helper: lane_count is the exact digest count, `0<=success_count<=lane_count`, `1<=minimum_successes<=lane_count`; succeeded iff all lanes, mixed iff threshold met but not all, fail-family only below threshold; queued/running may be 0..lane_count. Does not close #138.
- **#138 Slice A:** ConsultationView rejects an injected ConsultationAttempt whose harness_id, attempt, or receipt_digest does not match the supplied receipt (consultation_id bound via that receipt). Does not close #138.
- **#138 Slice A:** AdvisorHarnessSpecV1 rejects `advisor_read_only=qualified` without pinned identity/version/behavior evidence (schema v1 has none). Does not close #138.
- **#146 / PR #156 wrapper option peeling:** PreToolUse consumes `env -`/`-C`/`--chdir`, `sudo -u`/`--user`, `xargs -n`/`--max-args`, and `exec -a` operands before classifying the executable head; budget-exhausted foreign/first-party heads fail closed instead of false-green on truncated wrapper tails. Refs #146 (does not close).
- **#164 supervisor signal-forwarding publication race:** Team supervisors now
  install forwarding to the provider wrapper process group immediately after
  spawn, refine the target after provider-child resolution, and only then
  publish `provider_spawned`. A termination signal delivered as soon as the
  receipt becomes observable can no longer bypass forwarding and orphan the
  provider process group.
- **Issue-state `closure_sensitive` HIGH:** production receipts must use
  the exact canonical list `["#67", "#68", "#78"]` (order + set; drop /
  add / duplicate / reorder fail closed). Arbitrary nonempty `#N` lists
  are no longer accepted. Digest, source identity, and open-P0 reopen
  semantics unchanged. Refs #158.
- **Issue-state identity HIGH:** `load_and_validate_issue_state_evidence`
  rejects boolean `schema_version`, non-canonical
  `github.com/ImL1s/oh-my-grok` source identity, `#N` keys whose
  `number` is not the exact non-bool integer `N`, and issue URLs that
  are not `https://github.com/ImL1s/oh-my-grok/issues/N`. Digest and
  temporal validation stay fail closed. Refs #158.
- **Host-review historical to_ref HIGH:** pin-transition no longer
  swallows `assert_host_generated_docs_consistent`, globs arbitrary
  `GROK_BUILD-from-to-*.json` receipts, or authorizes a
  candidate-provided `generated_docs_hash`. Historical edges recompute
  the exact transition plan and canonical generated-doc digest from
  committed blobs at `to_ref` (fail closed if unrecomputable), resolve
  only the content-binding filename, and advertise that filename when
  missing. Claim-gate git used for committed-blob identity now passes
  `--no-replace-objects` and a sanitized env that drops `GIT_DIR` and
  related object overrides. Refs #158 / #105.
- **Host-review receipt HIGH:** `assert_host_review_binds_current_content`
  no longer accepts a forged untracked or committed JSON file just
  because nested `host_baseline` hashes match. Current-content and
  compatible pin-transition gates share
  `assert_canonical_immutable_host_review_receipt`: exact non-bool
  `schema_version`, exact `store_kind`/`source`/`from`/`to`/
  `reviewed_pin`/`previous_pin`/`snapshot_path`, canonical
  `change_digest`, recomputed `content_binding_digest`, filename bind,
  required acknowledgments, and a HEAD-committed blob. No
  change_digest-only filename fallback. Refs #158 / #105.
- **#105 PR5 ACP sidecar hardening:** validate `session/resume` identity
  (`sessionId` or the single `session_id` alias exclusively — dual keys
  rejected even when equal, string-equal UUID, JSON
  boolean `resumed is True`) before any receipt; `resume_matched` is
  fail-closed (missing / truthy-not-True ≠ success);
  `validate_receipt` / ensure inherit that check. Daemon-drain peer
  stderr to a bounded in-memory discard (never persisted). Cumulative
  `byte_budget` is compared only to `max_total_bytes` (per-line cap
  stays `max_line_bytes`). Does **not** close #105 (no `session/close`,
  `session/load`, live evidence, or UUID search).
- Constructor identity hashes must match `hash_session_id(session_id)` /
  `hash_cwd(cwd)` else `E_ACP_IDENTITY` and no receipt.
- Leftover newline-free `rx_buf` that already exceeds `max_line_bytes` fails closed with `E_ACP_OVERFLOW` before poll/timeout so a coalesced oversized suffix cannot ride through the quiet window into a receipt.
- Leftover newline-free `rx_buf` counts toward `max_total_bytes` (committed `byte_budget` plus currently buffered bytes) before poll/timeout and after append so a coalesced under-line-cap suffix cannot ride a quiet-window timeout into a receipt; complete frames still increment the budget only once (no double-count when leftover later completes). Does **not** close #105.
- Leftover incomplete suffix is checked for both per-line and cumulative caps before every timeout/`allow_timeout` return and unconditionally at the handshake pre-receipt boundary so `quiet_window_s=0` or an expired overall deadline cannot issue a receipt over a leftover overflow. Does **not** close #105.
- Complete NL-terminated frames already in `rx_buf` are checked against `max_line_bytes` before every timeout/`allow_timeout` return and at handshake pre-receipt so a coalesced oversized complete frame cannot ride `quiet_window_s=0` or an expired window into a receipt; extract-first leftover-only checks during read are unchanged; each buffered frame is classified individually (no combined-line false overflow; no budget double-count). Does **not** close #105.
- Pre-receipt absorb of pending incomplete-frame continuation still in the OS pipe so a coalesced resume + 300000-byte unterminated suffix at default `max_line_bytes` / `quiet_window_s=0` cannot issue a receipt before overflow. Does **not** close #105.
- Pre-receipt drain parses every complete NL-terminated frame already in `rx_buf` even when `quiet_window_s=0` (quiet loop never runs) so a coalesced resume + `agent_message_chunk` in one `os.write` raises `E_ACP_REPLAY` and never emits a receipt; malformed complete frames raise `E_ACP_MALFORMED` and unknown session/update frames raise `E_ACP_PROTOCOL`. Does **not** close #105.
- `validate_receipt` requires `initialized is True` (JSON boolean only). Missing, `false`, `0`, `1`, strings, and other truthy non-bools fail closed even when `receipt_sha256` is recomputed over the forged body; `build_receipt_from_dict` no longer coerces missing/`1`/`"true"` to True. Does **not** close #105.
- Buffered/frame overflow is classified before process-poll/`E_ACP_EOF` so an exited peer with an oversized complete or incomplete leftover yields `E_ACP_OVERFLOW` (never `E_ACP_EOF`, never a receipt). Handshake absorb + all-frames check run before the post-quiet exit poll. Does **not** close #105.
- Pre-receipt absorb always nonblocking-probes even when leftover ends on NL and drains every currently-ready pipe chunk; a resume/allowed frame ending exactly at the 4096-byte read chunk plus a queued >300 KiB suffix at `quiet_window_s=0` is `E_ACP_OVERFLOW`. Partial-suffix EOF is `E_ACP_EOF` even if `poll()` is still `None` (overflow still wins). Complete-frame EOF lets F9 classify replay/malformed/unknown first, then `E_ACP_EOF` before receipt. Deadline/no-ready with an incomplete suffix is `E_ACP_TIMEOUT` (never a silent return + receipt). Does **not** close #105.
- Pre-receipt absorb waits for a partial suffix in cancel-poll slices (0.05s), not the full remaining handshake deadline; `chunk is None` is not EOF (only `b""` is). Cancel raises `E_ACP_CANCELLED` before any receipt. Does **not** close #105.
- Handshake `cancel_event` is threaded through `_read_line` / every pre-receipt read; select waits are bounded by 0.05s; `b""` is EOF and `None` is not; overflow still beats cancel. Does **not** close #105.
- Handshake/env tests prove `OMG_ACP_FAKE_SUFFIX_BYTES` forwarding with a non-default value and an exact allowlist assert so the fixture default cannot hide a missing allowlist entry. Does **not** close #105.
- Handshake cumulative-total test now derives `max_total_bytes` from the actual serialized initialize/resume response frames plus the chosen leftover suffix (no magic 220), and a direct expired-deadline leftover check covers both buffered caps before timeout return. Does **not** close #105.
- session/resume request is sessionId+cwd only; result allowlist modes/models/configOptions/_meta; {} valid; unknown keys and wrong container types fail closed; identity is JSON-RPC id + request hashes; padding not in result. Does **not** close #105.
- **#146 / PR #156 F16 lexical leaf + exact schema:** supervisor
  admission never `Path.resolve()`s the descriptor leaf. Only parent
  directories are resolved; `read_managed_regular_bytes` opens the
  original leaf with `O_NOFOLLOW`. Publish and `build_supervisor_prefix`
  store/embed that lexical path. Provider descriptor, prepublish, and
  authoritative `team.json` schema versions reject `bool`, strings, and
  floats (`True` / `"1"` / `1.0` no longer admit as schema 1). Present
  prepublish defects with `team.json` present fail closed and do not
  fall through; only true ENOENT uses the published path. Same-FD
  pinning tests replace the lexical path immediately after the leaf fd
  opens.

- **#146 / PR #156 prepublish same-FD admit:** supervisor admission
  authenticates the CLI prepublish record first (confinement /
  `O_NOFOLLOW`, regular single-link file, mode 0600, schema / writer /
  run / worker / generation / attempt from that FD) and only then opens
  the referenced descriptor the same way. `descriptor_sha256` must match
  the authority and, when `team.json` is present, the published task
  bind. Path `stat` / `read_text` / reopen cannot authorize a replacement
  inode. Present-but-unsafe prepublish (symlink, hardlink, bad mode,
  corrupt) is refused and does not fall through to the published
  descriptor path.

- **#146 / PR #156 `command -v`/`-V` discovery:** PreToolUse no longer
  peels `command -v`/`-V` (or `-pv`/`-vp`/`-pV`/`-p -v`) as an execution
  wrapper, so `command -v claude` / `command -v omc` stay Allow — the
  name is lookup data, not the wrapped head. `command -p cmd` and
  `command -- cmd` remain execution and still deny deny-bins / `omc team`
  / worker nested `omg team launch`. Unknown flags stay fail-closed.

- **#146 / PR #156 named-FD `{ident}>` peel:** PreToolUse treats adjacent
  bash named file descriptors (`{fd}>/dev/null`, `{fd}>>`, `{fd}<`,
  `{fd}<>`, `{fd}<<<`, `{fd}>&`) as one redirect unit before semantic
  argv, so `omc {fd}>/dev/null team` cannot slip `team` past the
  foreign-orchestrator limit. Quoted, spaced, invalid-ident, and
  alnum-glued `{ident}` stay literal. Digit FDs and the 512/64
  fail-closed budget are unchanged.

- **#146 / PR #156 scale-up attempt-owned prepublish rollback:** after
  supervisor prepublish authority is created, a non-receipt-bound
  scale-up failure (partial publish, tmux/pane bind, or commit-prep)
  removes only this attempt's matching regular-file records
  (`kind`/`writer`/`run_id`/`worker_id`/`generation`/`attempt`).
  Receipt-bound / live windows preserved for retry keep matching
  prepublish. Rollback never masks the primary error.

- **#146 / PR #156 env `-S` / `--split-string` peel:** PreToolUse parses
  BSD/GNU `env -S` and GNU `--split-string` (including attached
  `-SSTRING`, `--split-string=`, BSD `--S`, and combined `-iS`/`-vS`)
  so hidden `omg team` / `omc team` / deny-bin heads classify.
  Invalid, recursive, ambiguous (`-S=` / `--S=`), or over-budget split
  strings fail closed. Ordinary `NAME=value` and `-i`/`-u`/`--unset`
  still peel after expansion.

- **#146 / PR #156 worker composition publication gate:** workers cannot run
  leader-owned `omg team hyperplan|security-research` materialize / validate /
  produce / admit-tasks / collect-tasks. Parsed-argv preflight and persist
  writers fail closed with `E_TEAM_WORKER_OPERATION_REFUSED` /
  `E_TEAM_COMPOSITION_TASK_GATE` before project-root or FS writes; PreToolUse
  classifies those sub-actions as nested launch (`E_TEAM_NESTED_LAUNCH`).
  Identity-bound `claim-lane` / `submit-lane-result` stay worker-reachable;
  zero-mutation `plan` is ungated.

- **#146 / PR #156 redirect `<>` / `<<<` + head-tail budget:** PreToolUse
  now treats POSIX read/write `<>` and bash here-string `<<<` (including
  FD-adjacent `2<>` / `0<<<`) as real redirects in the leading-redir
  regex, operator sets, and quote-aware boundary inserter. Here-strings
  stay excluded from the heredoc body parser. Character (512) and
  raw-token (64) budget exhaustion is indeterminate and fail-closes only
  for foreign `omc` / first-party `omg` candidates (and their wrapper
  paths), not narrative `echo` / quoted mentions / spaced `2 <>out`.
  Standalone hook regenerated.

- **#146 / PR #156 descriptor admit/use digest bind:** post-publication
  supervisor admission now requires `descriptor_sha256` on the matching
  `team.json` task row (start and scale stamp it from the exact
  published file). Admit loads the descriptor once (O_NOFOLLOW regular
  file, mode 0600), binds that digest against prepublish or the
  published task, and carries the immutable mapping into spawn so a
  replacement between admit and use cannot execute. Missing digest
  after publication fails closed. Prepublish digest validation and
  run/team/worker/owner identity are unchanged.

- **#146 / PR #156 reuse rollback of descriptor/authority:** failed
  `--run` reuse now snapshots provider descriptors and supervisor
  prepublish authority (bytes + mode) before materialize/publish.
  Rollback restores prior regular files exactly or unlinks artifacts
  created by the failed start, so no uncommitted executable authority
  remains. New-run team dirs are still removed wholesale. Covers D1,
  multi-CLI, fixture, and exact retry.

- **#146 / PR #156 scale-up prepublish authority:** live
  `omg team scale --add` now passes start_team-equivalent authority
  kwargs into every scale materialize/fixture call and publishes
  generation/attempt-bound supervisor authority after WAL plan,
  before pane spawn. On success, prepublish is cleared after
  team.json readback commits the new worker. Dry-run does not
  publish.

- **#146 / PR #156 startup fixture prepublish:** hermetic
  `tests/test_team_startup.py` CLI supervisor cases publish the same
  CLI-style prepublish authority as production (after descriptor write,
  before `team supervisor` Popen). `admit_pane_supervisor` stays
  fail-closed; forged/missing-authority negatives are unchanged.

- **#146 / PR #156 P1 wrapper-option peel:** PreToolUse consumes bounded
  wrapper options before the wrapped head (`env -i` /
  `--ignore-environment` / `-u NAME`, `command -p` (execution peel), `nice -n N` /
  `--adjustment`). `env -i omg team launch` under worker markers is
  `E_TEAM_NESTED_LAUNCH` instead of treating `-i` as the command.
  Unknown/malformed wrapper flags fail closed when the next non-flag
  token is a nested launch; `echo omg team` after residue is not scanned.
  Foreign `omc team` and deny-bin heads keep the same peel. Standalone
  hook regenerated.

- **#146 / PR #156 deny leading-global DiD:** PreToolUse first-party Team
  scan peels the same supported leading globals as `normalize_team_argv`
  (`--json` / `--safe` / `--yolo` / `--project-root PATH`) before
  requiring `team`, so `omg --json team 3:executor …` under worker
  markers is `E_TEAM_NESTED_LAUNCH`. Unknown flags are not skipped.
  watch / hyperplan / security-research stay non-launch. Standalone hook
  regenerated.

- **#146 / PR #156 worker gate leading globals:** `normalize_team_argv`
  now peels supported leading `--project-root PATH`, `--json`, `--safe`,
  and `--yolo` (arity-aware; no payload scan) so Form A/B shorthand
  rewrites before argparse. `--json team 3:executor …` and prefix
  `--project-root /missing team …` under worker markers emit
  `E_TEAM_NESTED_LAUNCH` instead of argparse `SystemExit`. Refused-path
  tests also boom `write_pid_metadata`, `_SYSTEM_POPEN` / `Popen`,
  `prepare_leader_spawn`, and `launch_team`. `cmd_team` launch DiD
  re-proved. Legal status/panes/capture/API reach their helpers.

- **#146 / PR #156 real-tmux fixture adapter:** `install_fixture_provider`
  now accepts and forwards every production authority argument
  (`leader_root`, `run_id`, `team_id`, `worker_id`, `owner_token`,
  `authority_generation`, `authority_attempt`, `publish_authority`) into
  `materialize_supervisor_pane_command`. Unknown kwargs still TypeError
  (not swallowed). Fixes live `team-real-tmux-*` TypeError after 2c2283d.

- **#146 / PR #156 parsed-argv Team worker preflight:** `main()` runs
  `preflight_team_worker_parsed_argv` after argparse / `normalize_team_argv`
  and **before** `clear_resolved_project_root` / project-root discovery /
  git / supervisor-root, so a missing `--project-root` cannot mask typed
  `E_TEAM_NESTED_LAUNCH` / `E_TEAM_WORKER_OPERATION_REFUSED`. `cmd_team`
  keeps the same preflight as defense-in-depth. Legal pane `supervisor`,
  identity-bound `api`, and read-only `status`/`panes`/`capture` continue.

- **#146 / PR #156 Team worker preflight proof:** tests/placement now prove
  git/tmux/process/state writers are not reached for prefix `--project-root`,
  bare `team`, and other worker markers; legal `capture` + non-catalog `api`
  still resolve.

- **#146 / PR #156 post-publish descriptor bind:** after authoritative
  `team.json` with `owner_token`, supervisor admission still requires the
  CLI-published per-worker `{worker_id}.provider.json` path (and, when tasks
  are listed, a known worker id). A shared owner token alone cannot spawn an
  arbitrary schema-valid descriptor. Surviving prepublish records remain the
  stronger digest bind when present.

- **#146 / PR #156 parser residuals:** PreToolUse scanner recognizes leading
  redirections before the executable (`>out omc team`, `2>/dev/null env
  /opt/omc team`, worker analogues) and adjacent file-descriptor prefixes of
  any shell-valid digit length (removed the arbitrary 4-digit cap so
  `12345>/dev/null …` still drops the redir). Spaced `2 >out` remains argv.
  Standalone hook regenerated.

- **#146 / PR #156 hook launch-shorthand DiD:** worker PreToolUse nested-launch
  classifier now matches `normalize_team_argv` Form B goal strings
  (`omg team "fix tests"`) and the `shutdown` alias, not only lifecycle
  verbs and numeric `N[:role]` specs. Path/wrapper/shell-c forms included;
  api/status/panes and other non-launch reserved ops still reach runtime.
  Standalone hook regenerated; vocab drift test vs `team.cli.RESERVED_ACTIONS`.

- **#146 / PR #156 `--run` owner token reuse:** `start_team(--run existing)`
  preserves the published `owner_token` from `team.json` instead of minting
  a conflicting fresh token before supervisor admission. Explicit caller
  tokens that disagree fail closed with `E_TEAM_OWNER_TOKEN_CONFLICT`
  before pane/materialize mutation; rollback leaves published authority
  intact. Covers materialize-only→live reuse.

- **#146 / PR #156 supervisor prepublish authority:** `omg team supervisor`
  no longer authorizes when `team.json` is absent. CLI publishes a managed
  per-worker prepublish record under the existing team tree (binds
  root/run/team/worker, owner token, descriptor path + content digest)
  **before** pane spawn; admission validates it without side effects and
  refuses forged env+descriptor. After authoritative `team.json` is
  written, prepublish records are cleared. Split/windows/fixture/dry-run
  paths covered; metadata absence never authorizes provider spawn.

- **#146 / PR #156 worker operator controls:** worker process markers
  (`OMG_TEAM_WORKER` / fanout / spawn markers) refuse leader-only operator
  mutations (`omg team input|key|focus|view`, including takeover forms)
  with typed `E_TEAM_WORKER_OPERATION_REFUSED` **before** `project_root`
  discovery, operator helpers, or tmux client mutation. Lifecycle verbs
  remain `E_TEAM_NESTED_LAUNCH`. Identity-bound `api` and read-only
  `status` / `panes` / `capture` stay usable; legal pane `supervisor`
  admission is unchanged. Command-text env assignments never authorize.

- **#146 first-party `omg team` routing:** PreToolUse soft-gate no longer
  classifies first-party `omg team …` as an external agent CLI. Executable
  heads are basename-normalized across bare, absolute/relative path,
  `env`/`command`/`exec`/`nice`/`nohup` wrappers, and `sh|bash|zsh -c/-lc`
  recursion so path-prefixed forms match bare forms. Foreign
  `omc team` / `claude`/`codex`/`omx`/`agy`/`cursor-agent`/`kimi` stay
  denied. Nested Team launch in a worker process env is refused with
  `E_TEAM_NESTED_LAUNCH` (hook defense-in-depth + runtime before side
  effects); identity-bound `omg team api` reaches runtime validation.
  Command-text env assignments never authorize. Standalone hook regenerated.

- **#159 ralplan staged-proposal mtime freshness flake:**
  `_validate_v2_proposal` no longer treats filesystem `st_mtime` vs
  invocation wall-clock as authorization. Coarse timestamps / clock
  drift no longer reject a newly written identity-bound proposal.
  Exact `schema_version`, `run_id`, `stage`, `role`, `round`,
  `invocation_id`, `session_id`, and `input_sha256` bindings still
  fail closed (replayed artifacts cannot match a fresh invocation).
  `_atomic_write_json` unlink and identity error strings unchanged.
  Hermetic coverage in `tests/test_ralplan.py` (forced-old mtime
  accept; independent one-at-a-time mismatch reject for all eight
  exact identity/schema bindings with fresh mtime; no sleep /
  tolerance). Closes #159.

### Changed
- **Reconcile #105 current host downstream owners:** session attach/close
  caps list `#74` only (not closed `#103`); queue/subagent/fan-out and
  auto-recap-no-interleave stay `#69` (not closed `#68`); auto-theme must
  not list `#95`/`#104`/`#147` as current owners. Production
  `check_host_downstream_owners` plus mutation tests.
  `HISTORICAL_GOVERNANCE_GAP_IDS` now restricts closed-gap `#78` to
  `gap.parity.governance.remaining` only. Minted a new content-bound
  `GROK_BUILD` receipt (`731c4c27…` / snapshot `31ff814c…` / docs
  `c2488910…`); historical `80a22517…` and `81e709b1…` ledgers
  untouched. Release-gate hermetic fixtures retain F4 issue-state
  bindings (`#67`/`#68` closed gaps + `v1.json`) so `--release` still
  fails on upstream drift, not missing evidence. Inventory stays
  `bootstrapping`. No live/completeness promotion. Refs #105 #74 #69
  (does not close). Historical #78.
- **Pinned offline issue-state evidence:** `--strict` consumes
  `docs/parity/issue-state/v1.json` (digest-bound, no network) for
  closure-sensitive `#67`/`#68`/`#78`. Observed GitHub: `#67`/`#68`
  closed; `#78` open/reopened with close pending PR #158 — not live
  truth. Rejects missing/stale/unknown/tampered receipts, inventory
  disagreement, and mutation reopening those issues as Open P0.
  Inventory stays `bootstrapping`.
- **Host baseline review ledger content-bind:** mint a new immutable
  `GROK_BUILD` receipt bound to current snapshot/docs hashes
  (`38350559…` / `23d41ed5…`) instead of rewriting the historical
  `81e709b1…` ledger (`3eed15cc…` / `fe0b9855…`). Filename digest is
  `change_digest` + `snapshot_hash` + `generated_docs_hash`. Strict/host
  `--check` fails if no receipt binds current content. Inventory stays
  `bootstrapping`. Refs #78 (historical) #105.
- **Closed #78 must not remain a present-tense residual owner** on
  capability/gap `issues`. Child owners: skills #70, agents #71, hooks
  #72, LSP/AST/MCP #73, session/state #74, visual #75, edit/hygiene #76,
  install #77, Team/runtime #69; #79 aggregate only. Locks #73/#76.
  Strict gate rejects residual #78 and #79-only replacement of locked
  children. Inventory stays `bootstrapping`. Open P0 still #69. Refs
  #70-#77 #69 #79 (does not close). Does not close #78 again (already
  closed as governance).
- **#78 GAPS governance reconciliation:** closed GitHub issues #67/#68 are
  no longer open P0 owners in `docs/parity/omg-parity.json` or generated
  `GAPS.md` Open P0. `gap.parity.governance.remaining` is closed: pinned
  inventory, CI claim gates, and generated docs exist. Remaining
  authenticated Antigravity live evidence, Team job-backed workers, and
  host prompt-queue/fan-out consume stay on #69; provider loading/doctor
  follow-up on #77; near-1:1 leftovers (including host-owned typed AG
  model/effort/mode) on #79. Inventory stays `bootstrapping`. No
  `live_verified` or completeness promotion. Historical #67/#68/#78 links
  remain on closed gaps and capability provenance. Closes #78. Refs #69
  #77 #79 (does not close #69/#77/#79).

### Fixed
- **Parity FEATURE-MATRIX / GAPS owner drift (#77):**
  `antigravity.provider.adapter` now lists active owner `#77` (provider
  loading/doctor via `gap.install.provider_doctor`) alongside historical
  `#67` and remaining live/team `#69`, so generated FEATURE-MATRIX agrees
  with GAPS. Inventory stays `bootstrapping`. Open P0 remains `#69` only.
  No live promotion. Refs #77 #69 (does not close).

### Added
- **#138 Slice A external-advisor contracts + offline catalog:** versioned
  taxonomy (`runtime_kind=external_cli`, `purpose=advisory`,
  `lifecycle=foreground|background_job`), canonical harness registry
  (`claude-cli` / `codex-cli` / `grok-cli` / `cursor-cli` /
  `antigravity-cli` / `gemini-cli`; `fable`→`claude-cli`,
  `agy`/`antigravity`→`antigravity-cli`), fail-closed
  `AdvisorHarnessSpecV1` plus Consultation/Council V1 documents, and a
  legacy ask-meta mapper (`legacy_field=true`; never Team/Medley/native).
  Every listed harness stays `advisor_read_only=unproven` with no
  identity/version/behavior fixture. Offline CLI: `omg ask list-advisors`
  and `omg ask explain <id>` (human + global `--json`; unknown id exit 1
  `E_ADVISOR_NOT_FOUND`; usage exit 2). No PATH/binary/network probe, no
  execution-path change, no consultation store. Docs: `skills/omg-ask`,
  `docs/skills.md` (+ zh / zh-TW), `docs/cli-contract.md`. Tests:
  `tests/test_advisor_registry.py`, `tests/test_consultation_legacy.py`,
  `tests/test_ask_catalog.py`. Does **not** close #138.

### Added
- **#69 PR13 Composition Lane Worker Protocol V1:** shared worker-scoped
  `claim-lane` / `submit-lane-result` for Hyperplan and Security Research
  (`omg_cli/team/compositions/lane_protocol.py`). Resolves `lane_id` →
  immutable PR12/PR11 task binding, claims via existing `claim-task`, returns
  bounded `CompositionLaneClaimV1` (goal/target + validated dependency
  outputs; never leader conversation), and submits `LaneTaskResultV1` via
  `transition-task-status`. Leader-only `collect-tasks` unchanged.
  `execution_supported=false` retained. No scheduler / launcher / mailbox
  dispatcher / provider callback / Catalog V5 / MCP mutation. CLI:
  `omg team hyperplan|security-research claim-lane|submit-lane-result`.
  Hermetic coverage in `tests/test_team_composition_lane_protocol.py`. Does
  **not** close #69 (no composition execution / Antigravity live evidence /
  full OMX / maturity promotion / `live_*` / `execution_supported=true`).

### Fixed
- **Parity OMC/OmO coverage_digest after #69 PR13 GAPS sync:** refresh proof
  `coverage_digest` values only after gap.team.v3 +
  `omo.team.hyperplan_security` text update for composition lane worker
  protocol (no policy/mapping/status changes). Refs #69 (does not close).

### Added
- **#69 PR12 Shared Composition Task Driver V1:** one hermetic bridge
  (`omg_cli/team/compositions/task_driver.py`) admits materialized Hyperplan
  / Security Research manifests into PR11 `TaskBatchV1` and collects
  completed lane `LaneTaskResultV1` payloads into the existing result-bundle
  producers. CLI: `omg team hyperplan|security-research admit-tasks|collect-tasks
  --run RUN --team-id TEAM [--json]`. Lock order: composition → batch →
  numeric task IDs. Workers must not supply lane/digest/writer identity;
  collector derives them. `execution_supported=false` retained. No auto
  workers / panes / Jobs / providers / PoC / MCP mutation / catalog V5.
  Docs: `docs/team-hyperplan-v1.md`, `docs/team-security-research-v1.md`.
  Hermetic coverage in `tests/test_team_composition_task_driver.py`. Does
  **not** close #69 (no composition execution / Antigravity live evidence /
  full OMX / maturity promotion / `live_*` / `execution_supported=true`).

### Fixed
- **Parity OMC/OmO coverage_digest after #69 PR12 GAPS sync:** refresh proof
  `coverage_digest` values only after gap.team.v3 +
  `omo.team.hyperplan_security` text update for shared composition task
  driver (no policy/mapping/status changes). Refs #69 (does not close).

### Added
- **#69 PR11 Team Catalog V4 — Atomic Task-Batch DAG Admission V1:** catalog
  schema v4 adds leader-only mutating `bulk-create-tasks` (v1–v3 goldens
  byte-frozen). Pure `compile_task_batch_v1` admits 1–32 unique safe
  task_keys with intra-batch deps only (no cycles/self/dupes/missing),
  deterministic topo order, bounded exact-key `expected_artifact`, and
  rejects caller-supplied IDs/status/claims/results/versions. Crash-safe
  `admit_task_batch_v1` under API-config + batch locks: prepared → reserve
  contiguous IDs → write immutable batch/task-key bindings → re-read verify
  → commit marker last. Uncommitted batch tasks invisible to
  read/list/claim. Idempotent same key+digest; conflicts / symlink /
  corrupt / foreign-writer fail closed. CLI:
  `omg team api bulk-create-tasks --input BATCH.json --json`. No MCP
  mutation in this slice. Docs: `docs/team-operation-catalog-v4.md`.
  Hermetic coverage in `tests/test_team_task_batch.py` (+ golden). Does
  **not** close #69 (no composition execution / provider/pane/Jobs launch /
  Antigravity live evidence / full OMX / maturity promotion / `live_*` /
  `execution_supported=true`).

### Fixed
- **Parity OMC/OmO coverage_digest after #69 PR11 GAPS sync:** refresh proof
  `coverage_digest` values only after gap.team.v3 +
  `omo.team.hyperplan_security` text update for catalog v4 task-batch
  admission (no policy/mapping/status changes). Refs #69 (does not close).

### Added
- **#69 PR10 Hyperplan Hermetic Result Production V1:** offline
  `compile_hyperplan_decision_v1` / `produce_hyperplan_decision_v1` over
  `HyperplanResultBundleV1` (exact-key lane receipts; CLI digests;
  `execution_supported=false`). CLI:
  `omg team hyperplan produce-decision --run RUN --input BUNDLE.json`.
  Persistence: `hyperplan-v1-result-bundle.json` then decision commit marker
  under the composition lock. Once a result-bundle exists,
  `validate-decision --persist` refuses overwrite unless byte/normalized
  equivalent (produce-owned marker). Strengthened
  `load_hyperplan_manifest` recompile-vs-core. Docs:
  `docs/team-hyperplan-v1.md`. Hermetic coverage in
  `tests/test_team_hyperplan.py` (+ goldens). Does **not** close #69 (no
  Hyperplan execution / Security Research composition execution /
  Antigravity live evidence / catalog v4 / maturity promotion / `live_*`).

### Fixed
- **Parity OMC/OmO coverage_digest after #69 PR10 GAPS sync:** refresh proof
  `coverage_digest` values only after gap.team.v3 +
  `omo.team.hyperplan_security` text update for Hyperplan V1 result
  production (no policy/mapping/status changes). Refs #69 (does not close).

### Added
- **#69 PR9 Security Research Hermetic Result Production V1:** offline
  `compile_security_research_report_v1` /
  `produce_security_research_report_v1` over
  `SecurityResearchResultBundleV1` (exact-key lane receipts; CLI digests;
  `execution_supported=false`). CLI:
  `omg team security-research produce-report --run RUN --input BUNDLE.json`.
  Persistence: `security-research-v1-result-bundle.json` then report commit
  marker under the composition lock. Closes PR8 dual-review P1: high/critical
  `validator_artifact_refs` must equal exactly `validate.primary` +
  `validate.independent` coverage digests; `reproduced` only on dual
  `local_fixture`; CVSS 3.1 metric enums; recompile-vs-core forged-lane
  refusal. Docs: `docs/team-security-research-v1.md`. Hermetic coverage in
  `tests/test_team_security_research.py` (+ goldens). Does **not** close #69
  (no composition execution / PoC / Hyperplan result production / Antigravity
  live evidence / catalog v4 / maturity promotion / `live_*`).

### Fixed
- **Parity OMC/OmO coverage_digest after #69 PR9 GAPS sync:** refresh proof
  `coverage_digest` values only after gap.team.v3 +
  `omo.team.hyperplan_security` text update for Security Research V1 result
  production (no policy/mapping/status changes). Refs #69 (does not close).

### Added
- **#69 PR8 Security Research Composition Contract V1:** non-executing
  `omg_cli.team.compositions.security_research` with `SecurityResearchSpecV1` /
  `ManifestV1` / `ReportV1` and pure `compile_security_research_v1()` (N hunters
  + dual validate + consolidate + verify; immutable safe-PoC policy;
  `execution_supported=false`). CLI:
  `omg team security-research plan|materialize|validate-report`. Materialize
  persists only
  `.omg/state/runs/<run>/team/compositions/security-research-v1.json`
  (idempotent; digest/symlink/corrupt/foreign-writer fail-closed). Report gate:
  `pass` / `pass_with_findings` / `block` with severity proof rules; never
  writes `passes`/`verified`. Docs: `docs/team-security-research-v1.md`.
  Hermetic coverage in `tests/test_team_security_research.py` (+ golden). Does
  **not** close #69 (no execution / PoC running / Hyperplan execution /
  Antigravity live evidence / catalog v4 / maturity promotion / `live_*`).

### Fixed
- **Parity OMC/OmO coverage_digest after #69 PR8 GAPS sync:** refresh proof
  `coverage_digest` values only after gap.team.v3 +
  `omo.team.hyperplan_security` text update for Security Research V1
  scaffolding (no policy/mapping/status changes). Refs #69 (does not close).

### Added
- **#69 PR7 Hyperplan Composition Contract V1:** non-executing
  `omg_cli.team.compositions.hyperplan` with `HyperplanSpecV1` /
  `ManifestV1` / `DecisionV1` and pure `compile_hyperplan_v1()` (N critics +
  synthesize + verify; `execution_supported=false`). CLI:
  `omg team hyperplan plan|materialize|validate-decision`. Materialize
  persists only
  `.omg/state/runs/<run>/team/compositions/hyperplan-v1.json` (idempotent;
  digest/symlink/corrupt/foreign-writer fail-closed). Docs:
  `docs/team-hyperplan-v1.md`. Hermetic coverage in
  `tests/test_team_hyperplan.py` (+ golden). Does **not** close #69 (no
  execution / synthesis / security compositions / Antigravity live evidence /
  catalog v4 / maturity promotion / `live_*`).

### Fixed
- **Parity OMC coverage_digest after #69 PR7 GAPS sync:** refresh OMC proof
  `coverage_digest` only after gap.team.v3 text update for Hyperplan V1
  scaffolding (no policy/mapping/status changes). Refs #69 (does not close).

### Added
- **#69 PR6 Team Presentation State V1:** pure read-only
  `build_team_presentation_v1()` with generation-fenced snapshot; identical
  payload via `omg team status --presentation [--json]`, catalog **v3**
  leader-only `read-presentation-state`, and MCP `team_status.read`
  `projection=presentation.v1`. Additive `route` stamp on start/scale;
  replacement preserves/archives it. Default locked/`--full` status and
  catalog v1/v2 goldens unchanged. Docs: `docs/team-presentation-state-v1.md`,
  `docs/team-operation-catalog-v3.md`. Hermetic coverage in
  `tests/test_team_presentation_state.py`. Does **not** close #69 (no
  Hyperplan / security compositions / Antigravity live evidence / maturity
  promotion / `live_*`).

### Fixed
- **Parity OMC coverage_digest after #69 PR6 GAPS sync:** refresh OMC proof
  `coverage_digest` only after gap.team.v3 text update for Presentation
  State V1 (no policy/mapping/status changes). Refs #69 (does not close).
- **#69 PR5 replace-worker P0 hardening:** fence/cancel before claim
  invalidate (cancel failure rolls back intent WAL; attempt/claim/generation
  + old execution unchanged); crash-after-launch adopts the Jobs record
  stamped with the replacement idempotency key (no dual-launch); pane probe
  exceptions fail closed (not `proven_absent`). Hermetic coverage in
  `tests/test_team_replacement_attempts.py`. Refs #69 (does not close).
- **Parity OMC coverage_digest after #69 PR5 GAPS sync:** refresh OMC proof
  `coverage_digest` only after gap.team.v3 text update (no policy/mapping/
  status changes). Refs #69 (does not close).

### Added
- **#69 PR5 identity-fenced worker replacement attempts:** leader-only
  `omg team api replace-worker` (catalog **v2**) fences the old pane/job
  handle + claim, archives non-secret prior-attempt evidence, increments
  `attempt` + `launch_generation` under lifecycle lock/CAS, and relaunches
  via existing `launch_worker()` (pane|job on the #68 Jobs plane — no second
  scheduler). Crash-safe replacement WAL + idempotency adoption; resume
  recovers pending replacement before claim reconcile. Hermetic coverage in
  `tests/test_team_replacement_attempts.py`. Docs: `docs/team.md`,
  `docs/team-operation-catalog-v2.md` (v1 golden unchanged). Does **not**
  close #69 (no Hyperplan / security compositions / Antigravity live
  evidence / maturity promotion / `live_*`).

## [0.8.0] - 2026-08-10

### Fixed
- **Release writer-ownership for Jobs/Team/completeness paths:** register
  `docs/durable-jobs.md`, `docs/team*.md`, `docs/parity/completeness/**`,
  `omg_cli/jobs/**`, new `omg_cli/team/*` modules, host probe/ACP surfaces,
  and matching tests under `OMG_OWNER_PATTERNS` so `release.yml` verify
  no longer fails closed on unowned dirty paths (same class as #93).

### Highlights
- **Team tmux milestone:** default-on `omg team` with same-window topology,
  identity-fenced attach/view, claim reconcile on resume, and optional
  `--worker-topology=job` on the #68 Jobs plane. Fixture + interactive UX
  smokes green (`FIXTURE_TEAM_SMOKE_OK`, `LIVE_TEAM_INTERACTIVE_UX_OK`).
  Does **not** close #69 (replacement / Hyperplan / live Antigravity still open).
- **Durable Jobs plane (#68 PR1–PR5):** start/status/wait/cancel/collect,
  retry+GC, lease recovery, bounded `auto-retry`. Does **not** close #68
  (authenticated live Antigravity evidence still open).
- **Parity real-source completeness (#78-F–I):** OMC/OMX/OmO/Antigravity
  pinned discovery triples; Antigravity docs-only (`promotion_sufficient=false`).
  Canonical inventory stays **bootstrapping** — no promotion.

### Added
- **#69 PR4 job-backed Team workers:** unified `launch_worker` execution
  abstraction (`omg_cli/team/launch.py`) with `--worker-topology=pane|job`
  on `omg team start|run|launch`. Job topology reuses the #68 Jobs plane
  (no second scheduler); Team persists only durable execution refs
  (`topology` + XOR `job_id`/`pane_id` + `launch_generation`). Resume binds
  existing jobs without relaunch; stop cancels via Jobs; stale/foreign
  completions fail closed. Hermetic coverage in
  `tests/test_team_job_workers.py`. Docs: `docs/team.md`. Does **not**
  close #69 (no replacement attempts / Hyperplan / security compositions /
  Antigravity live evidence / maturity promotion).

### Fixed
- **#69 PR4 merge-gate P1s:** stop no longer stamps cancelled/`stop_state`
  when Jobs cancel fails; `launch_worker` stamps `team_id` on Jobs
  `request` and resume refuse same-root foreign binds; `apply_job_completion`
  requires non-empty claim tokens (no `None`/`None` soft success); stamp
  refuses corrupt dual-id prior handles; team-exec job topo observes Jobs
  (no pane-wait / no auto-complete). Refs #69 (does not close; no `live_*`).

### Fixed
- **Parity #78-I Antigravity docs-only promotion false-green:** encode
  `proof_kind` + `promotion_sufficient` on completeness policy/proof
  schema; Antigravity is `documentation_catalog_seed` /
  `promotion_sufficient: false`. Promotion gate refuses
  `source_status.Antigravity=complete` (and category/inventory promotions
  that depend on it) even when digests verify. OMC/OMX/OmO remain
  `implementation_registry` / promotion-sufficient. Refs #78 (does not
  close).

### Added
- **Parity #78-I real pinned Antigravity discovery evidence:** committed
  discovery_rules v2 policy, surface→capability mapping, and completeness
  proof under `docs/parity/completeness/{policies,mappings,proofs}/Antigravity.json`
  for pin `bfab12da…` (https://github.com/google-antigravity/antigravity-cli).
  Documentation-only extractors cover README/CHANGELOG/examples/ISSUE_TEMPLATE
  (fail-closed; no package.json/TS/plugin/hooks/agent registries). Hermetic
  fixture under `tests/fixtures/parity/completeness/real_source/Antigravity/`.
  Canonical `source_status.Antigravity` (and inventory/categories) stay
  **bootstrapping** — docs/catalog seed only, **no promotion**. Stronger
  upstream still needed for implementation-level completeness. Refs #78
  (does not close).

### Fixed
- **Parity #78-I OMC coverage_digest after governance gap sync:** gap text for
  `parity.inventory.governance` (and GAPS/MATRIX) now includes #78-I; refresh
  OMC proof `coverage_digest` only (no policy/mapping/status changes). Refs #78
  (does not close).
- **Parity #78-H OMC Commander alias proof drift:** after shared
  `commander_command_graph_v1` began emitting static `.alias()` surfaces,
  regenerate the OMC mapping/proof at pin `67dddfc…` to include `cli.rm` and
  `cli.sessions`, and extend the hermetic OMC CLI fixture so alias discovery
  cannot silently diverge from the committed real-source triple. Statuses
  remain bootstrapping (no promotion). Refs #78 (does not close).
- **Parity #78-H OmO upstream snapshot fingerprint sync:** add
  `cli-program.ts` to `omo.tools.lsp_ast_codegraph_mcp` in
  `docs/parity/upstream-snapshots/OmO.json` and refresh OmO proof
  `seed_digest` so release-gate catalog fingerprints match the inventory
  enrichment (fail-closed drift gate unchanged; no promotion). Refs #78
  (does not close).
- **Jobs recovery reused-PID test under CI load:** plant a RUNNING+lease
  fixture without a live heartbeat so `recover_job` CAS cannot race
  generation (`E_JOB_RECOVERY_CONFLICT`) while still asserting no signal
  on REUSED identity. Refs #78 (does not close).
- **Parity #78-H OmO Commander/Zod discovery fail-closed:** resolve
  `addCommand(factory())` (emit `cli.mcp` from `createMcpOAuthCommand()`),
  emit Commander `.alias()` surfaces (`cli.setup`, `cli.uninstall`), and
  reject non-string-literal Zod enum elements instead of partial extract.
  Regenerated OmO mapping/proof at pin `4ca872b…`; statuses remain
  bootstrapping. Refs #78 (does not close).

### Added
- **Parity #78-H real pinned OmO discovery evidence:** committed
  discovery_rules v2 policy, surface→capability mapping, and completeness
  proof under `docs/parity/completeness/{policies,mappings,proofs}/OmO.json`
  at pin `4ca872b…` (zod schema enum + CLI/command/package extractors against
  the real oh-my-openagent tree), plus hermetic registry fixtures and
  `tests/test_parity_real_source_omo.py`. Network-free artifact consistency
  verifies without claiming source reproduction. Canonical
  `source_status.OmO` / categories / `inventory_status` stay bootstrapping —
  proof present, **no promotion**. Antigravity real-source proof remains
  open. Refs #78 (does not close).

- **Parity #78-G real pinned OMX discovery evidence:** committed
  discovery_rules v2 policy, surface→capability mapping, and completeness
  proof under `docs/parity/completeness/{policies,mappings,proofs}/OMX.json`
  at pin `435d4a9…` (catalog-manifest-first extractors), plus hermetic
  registry fixtures and `tests/test_parity_real_source_omx.py`. Network-free
  artifact consistency verifies without claiming source reproduction.
  Canonical `source_status.OMX` / categories / `inventory_status` stay
  bootstrapping — proof present, **no promotion**. Refs #78 (does not close).

- **Parity #78-F real pinned OMC discovery evidence:** committed
  discovery_rules v2 policy, surface→capability mapping, and completeness
  proof under `docs/parity/completeness/{policies,mappings,proofs}/OMC.json`
  at the existing OMC pin, plus hermetic synthetic registry fixtures and
  `tests/test_parity_real_source_omc.py`. Network-free artifact consistency
  verifies without claiming source reproduction. Canonical
  `source_status.OMC` / categories / `inventory_status` stay bootstrapping
  — proof present, **no promotion**. Refs #78 (does not close).

- **Partial work for #68 PR5 / bounded auto-retry scheduler:** caller-driven
  `omg job auto-retry JOB_ID|--all` one-pass tick over the existing
  `retry_job` / exact-next-attempt path (deterministic backoff, `--limit`
  default 1 / max 32, project `auto-retry.lock`, dry-run admission without
  mutation). Automatic intent admits only `state=failed` with persisted and
  recomputed `retry_class=automatic`; preserves PR4 live/unproven/
  spawn-uncertain/provider-unbound gates; never recovers, signals, or
  auto-retries `lost`/`cancelled`/`manual_only`. Hermetic coverage in
  `tests/test_jobs_auto_retry.py`. Docs: `docs/durable-jobs.md`. Does **not**
  close #68 (authenticated live Antigravity evidence remains open; Team
  job-backed workers stay on #69).

### Fixed
- **#68 PR5 P1 safe-conflict fail-closed reread:** `_is_safe_conflict`
  returns false when the post-error job reread fails (`cur is None`) —
  never `ok=True` conflict without proof that attempt advanced or state
  became nonterminal. Hermetic coverage in `tests/test_jobs_auto_retry.py`.
  Refs #68 (does not close).

- **#68 PR5 P1 strict process-identity JSON:** PID/PGID must be JSON
  integers (not bool/float), fingerprints/`pid_starttime` null-or-string
  only, and `spawn_identity.json.job_id` must exactly equal the enclosing
  job id. Malformed claims in any recorded state (including `exited`) map
  to `E_JOB_CANCEL_UNPROVEN` / `IDENTITY_UNPROVEN` — never GONE/REUSED via
  `int()`/`str()` coercion. Hermetic coverage in
  `tests/test_jobs_auto_retry.py`. Refs #68 (does not close).

- **#68 PR5 P1 classify_retry strict booleans:** `timed_out` / `overflow` must
  be JSON booleans (`true`/`false`/absent). String `"false"` / `"0"` / other
  truthy non-bools fail closed as `unknown` (`malformed_timed_out` /
  `malformed_overflow`); primary `spawn_error`/`malformed`/`parse_error`/
  `unknown` classes are classified before flag trust so contradictory
  envelopes cannot auto-retry. Hermetic coverage in
  `tests/test_jobs_auto_retry.py`. Refs #68 (does not close).

- **#68 PR5 P1 present-but-malformed / bound-but-incomplete identity:**
  `provider_process.state=bound` (or `launching`) with missing/malformed
  PID/PGID, and present-but-unparseable `spawn_identity.json`, map to
  `E_JOB_CANCEL_UNPROVEN` / `IDENTITY_UNPROVEN` — never absent/reclaimable.
  Aligns auto-retry/cancel/observe/recover/GC with PR4 unbound-launch
  semantics. Hermetic coverage in `tests/test_jobs_auto_retry.py` and
  `tests/test_jobs_recovery.py`. Refs #68 (does not close).

- **#68 PR4 cancel_requested beats racing success:** once `cancel_requested_at`
  is durable (persist-before-signal), `transition_job` /
  `transition_owned_job` / CAS remap racing `succeeded`/`failed` stamps to
  `cancelled` under the same lock. Closes the CI flake where an
  `ignore_sigterm` fake finished Adapter.run during SIGTERM→grace→SIGKILL and
  `cancel_job` returned `succeeded`. Hermetic coverage in
  `tests/test_jobs_runtime.py`. Refs #68 (does not close).

- **#68 PR4 P0 unbound provider launch window:** `provider_process.state=launching`
  with incomplete PID/PGID is never treated as provider-absent
  (`recoverable_lost`). Observe/recover classify it as `IDENTITY_UNPROVEN`
  (`provider_launch_unbound`, same gate as `cancel_job`);
  `_assert_prior_attempt_gone` refuses retry on such records so a duplicate
  attempt cannot start while an orphan provider may still be alive. Hermetic
  coverage in `tests/test_jobs_recovery.py`. Refs #68 (does not close).

### Added
- **Partial work for #68 PR4 / lease recovery:** attempt-scoped owner lease +
  runner heartbeat; read-only liveness observation on `omg job status|list|wait`;
  explicit `omg job recover` / `recover --all` reconciles expired abandoned jobs
  to `lost` after OS identity proof (CAS/generation-safe; concurrent recover one
  winner). Lost jobs reclaim only via existing exact-next-attempt
  `omg job retry` (no auto-retry scheduler). Fail-closed for live/unproven/
  orphan-provider identities; public surfaces never expose owner tokens.
  Hermetic coverage in `tests/test_jobs_lease.py` and
  `tests/test_jobs_recovery.py`. Docs: `docs/durable-jobs.md`. Does **not**
  close #68 (automatic retry scheduling and authenticated live Antigravity
  remain open).

- **#69 PR3 leader-resume task-claim reconciliation:** `omg team resume`
  (`resume_for_identity`) reconciles Team API task claims under the existing
  scale.lock after pane reconcile/relaunch — preserve coherent unexpired
  claims; release only coherent expired claims to `pending` (version +1;
  old token fenced). Additive `claim_reconcile` on resume output
  (IDs/counts only). Hermetic coverage in `tests/test_team_reconcile.py`.
  Does **not** close #69 (no job-backed workers / attempts / Hyperplan /
  catalog-v1 reconcile op / maturity promotion). Fail-closed preflight
  rejects non-string/unsafe owners, non-string / padded / whitespace-only
  tokens, filename/body id mismatches, and duplicate embedded task ids
  (zero mutation; aborts resume).

- **Partial work for issue 68 (PR3 / retry+GC+ask --background):** explicit
  `omg job retry JOB_ID --attempt N` with immutable `attempt_budget`, attempt
  archives under `.omg/jobs/<id>/attempts/NNNN/`, retry classification
  (automatic/manual_only/never/unknown; no auto-scheduler), terminal
  `omg job gc --retention-days N` (never deletes nonterminal; refuses
  ACP-bound; skips malformed; revalidates under lock), and
  `omg ask --background` thin seam (`fake`|`agy` → durable job + immediate
  `job_id`; sync ask unchanged). Reuses the existing `start_job` /
  `launch_job_runner` path. Docs: `docs/durable-jobs.md`. Does **not** close
  #68 (lease recovery / auto-retry / authenticated Antigravity still open).

- **#105 PR4 hermetic ACP resume sidecar:** Team `--provider-session` on an
  AVAILABLE `session_resume` gate starts/reuses an internal jobs-plane
  `grok-acp-session` sidecar (`grok agent stdio`, initialize → one
  `session/resume`, no-replay quiet window, atomic content-free receipt).
  Public `omg job start` still admits only `fake`/`antigravity`. Cancel is
  dual process-group teardown (not `session/close`). Hermetic fake-peer
  coverage only — no live-host maturity promotion. #105 stays open for
  close/restore-code/search/background deltas.

- **#78-E README↔JSON managed-snapshot drift gate:** hand-authored
  `docs/parity/README.md` keeps exactly one managed block between
  `BEGIN/END GENERATED PARITY INVENTORY SNAPSHOT` markers.
  `scripts/generate_parity_docs.py` validates the inventory, renders a
  deterministic snapshot (statuses, pins, maturity/classifications as code
  spans, counts only — no timestamps), and `--check` fails closed on
  stale/malformed markers. Write mode repairs only that block
  (`MANAGED_BLOCK_PATHS`, not whole-README overwrite). Inventory
  maturity/status remain bootstrapping; #78 stays open (real-source
  policies/proofs still outstanding). No `live_*` capability claims.
- **#69 PR2 (Team operation catalog v1):** immutable
  `omg_cli/team/operation_catalog.py` is the single source for Team API op
  metadata; derives `TEAM_API_OPERATIONS` / `P0_OPERATIONS` /
  `WORKER_ALLOWED_OPS` / `WORKER_DENIED_OPS`. Introspection
  `omg team api catalog` emits versioned JSON
  (`kind: omg.team.operation_catalog`, schema_version 1) with no `--input`,
  team state, tmux, `.omg`, or subprocess. Golden
  `tests/golden/team_operation_catalog_v1.json` +
  `docs/team-operation-catalog-v1.md`. Does **not** close #69 (no new ops /
  reconcile / jobs / Hyperplan).
- **#68 PR2 Antigravity job adapter wiring:** durable jobs can execute the
  existing `ProviderAdapter.run` contract with repository fake-agy coverage,
  fail-closed provider admission, and bound child-process cancellation.
  Authenticated execution was not exercised. #68 remains open for retry,
  lease recovery, GC, and ask --background.
- **#78-D completeness promotion proof gate:** fail-closed
  `omg_cli/parity_completeness.py` policy/proof validators + reproduction,
  wired into strict `omg parity check` / `scripts/check_parity_inventory.py`.
  Promoting `source_status` / `category_status` / `inventory_status` to
  `complete` without a digest-bound proof fails closed; catalogue seeds are
  not proofs. Maintainer `scripts/check_parity_completeness.py` (`--plan` /
  `--check`) may emit a candidate proof but never mutates inventory status.
  Hermetic fixtures under `tests/fixtures/parity/completeness/`; schema in
  `docs/parity/completeness-schema-v1.md`. Canonical inventory stays
  bootstrapping — promotion remains unperformed and is proof-gated.
  Does **not** close #78 (real-source policies/proofs still outstanding).

### Fixed
- **#105 PR4 P0 unproven spawn identity persist:**
  `_persist_unproven_spawn_identity` no longer swallows `update_job_fields`
  failure after an unproven post-spawn cleanup. Runner pid/pgid/starttime is
  retained in durable `spawn_identity.json` (written at Popen, before RUNNING
  commit); when job.json stays `starting`+`pid=null`, `cancel_job` uses the
  recovery identity or refuses with `E_JOB_CANCEL_UNPROVEN` for job-bearing ACP
  bindings / uncertain markers — Team stop cannot publish stopped over an
  orphan. Hermetic regression in `tests/test_jobs_acp_session.py`.
- **#78-D P0 checkout/pin binding:** completeness proofs authenticate
  `--upstream-root` to `pin_revision` (`HEAD` match + clean porcelain) and
  stamp `checkout_provenance` before hashing registry/surface bytes. Wrong
  HEAD or dirty/untracked checkouts fail closed (hermetic git fixture
  coverage).
- **#78-D P0 pin blob binding:** discovery digests use `git cat-file` at the
  pin; auth also compares worktree `hash-object` OIDs to pin blobs so
  `skip-worktree` / `assume-unchanged` mutations fail closed.
- **#78-D P0 replace-ref binding:** completeness git invocations set
  `GIT_NO_REPLACE_OBJECTS=1` and `git --no-replace-objects` so
  `refs/replace/<pin>` cannot rebind pin trees/blobs.
- **#68 PR1 PID ownership (best-effort):** cancel records a `pid_starttime`
  fingerprint at `starting→running` (Linux `/proc/<pid>/stat` starttime or
  `ps -o lstart=`). Before **each** cancel signal (SIGTERM and again before
  SIGKILL), cancel revalidates pid/pgid both `> 1`, live `getpgid(pid)` vs
  recorded PGID, and (when present) the fingerprint. Outcome is explicit:
  OK → signal; GONE (dead / ProcessLookupError) → **no** signal,
  continue cancel stamp; mismatch fail-closes with `E_JOB_PID_REUSED` /
  `E_JOB_PGID_MISMATCH` and does **not** signal. Null fingerprint (probe
  failed at start) still falls back to pid/pgid + live PGID checks only —
  **not** full OmO-style lease/nonce ownership; deferred to a later #68 slice.
- **#68 PR1 launch ownership:** parent alone commits `starting→running`
  (pid/pgid/handle); child readiness barrier polls `job.json` until that
  commit (or terminal/timeout) before `ProviderAdapter.run`, and stamps
  only `running→succeeded|failed`. Immediate post-spawn child exit and
  cancel-during-uncommitted-window fail closed (never durable `running`
  without a live handle).
- **#105 PR3 review:** `omg team resume --provider-session` evaluates
  `session_resume` with `required=False` so missing cap yields reachable
  `LEGACY` (+ `next_action`), not unconditional BLOCKED; `provider_session_result`
  fail-closes on non-`session_resume` gate capability ids.

### Added
- **#69 PR1 (renew-task-claim):** explicit `omg team api renew-task-claim`
  extends an active in-progress claim lease under the per-task lock
  (`leased_until` = now + CLAIM_LEASE_SECONDS; never shortens; same claim
  token/owner/status). Fail-closed on missing task, terminal status, token
  mismatch, expired/malformed deadline, and `expected_version` conflict.
  Does not rotate tokens, mark tasks complete, or write `passes`/`verified`.
  Hermetic coverage in `tests/test_team_api.py` (+ heartbeat non-renewal in
  reliability). Does **not** close #69 (recovery/sweeper/job wiring later).
- **Partial work for issue 68 (PR1 / jobs MVP):** durable `.omg/jobs/<id>/`
  store + `omg job start|status|wait|collect|cancel|list` with JSON CLI
  envelopes. Subprocess job runner owns `ProviderAdapter.run` (no second
  launcher). Hermetic `FakeProvider` worker (`--provider fake`);
  `--provider antigravity` returns clear `E_JOB_PROVIDER` (live spawn
  deferred). Atomic start (queued→starting before launch; launch failure →
  failed, never running with null pid). Cancel by recorded PID/PGID only
  (sibling-safe; never `pkill`/`killall`). Large outputs as
  `artifacts/` descriptors only. Does **not** close #68 (retry/GC/lease
  recovery/`ask --background` later).
- **Partial work for issue 67 (slice D / #67-D):** Team Antigravity panes
  route through adapter-owned `ProviderLaunchRequest` /
  `ProviderLaunchEnvelope` + `AntigravityProvider.build_launch_envelope`
  (argv array only; never a shell string). Team `_build_agy` /
  `build_executor_argv` consume the envelope; supervisor retains
  spawn/PTY/PID/PGID/readiness/nonce authority — **does not** call
  `ProviderAdapter.run` for interactive panes. Preserves Team contract
  (`needs_pty=True`, `-p` path placeholder + `positional-text`,
  `--dangerously-skip-permissions`, RO `--sandbox`). Descriptor gains
  optional `identity_basenames` / `provider_strategy` / `startup_strategy`
  without bumping schema v1 (resume-safe; launch-receipt semantics
  unchanged). Hermetic coverage in `tests/test_team_agy_envelope.py`.
  **Slices A–D complete** — issue #67 may be closeable after dual review +
  CI (do not merge until then).
- **Partial work for issue 67 (slice C / #67-C):** `omg ask agy` routes
  through `ProviderAdapter.run` (Antigravity headless adapter). `agy` is a
  first-class ask provider — **not** aliased to `gemini`. Legacy
  codex/claude/gemini ask paths unchanged. Artifacts / exit codes / dry-run /
  timeout (exit 4) / missing-binary (exit 3) / auth-blocked failure preserved.
  Hermetic fake-`agy` coverage in `tests/test_ask_agy.py`. Team cutover
  landed as **slice D**.
- **Partial work for issue 67 (slice B / #67-B):** headless Antigravity
  execution on `ProviderAdapter.run` — provider-neutral
  `ProviderRunRequest`/`ProviderRunResult` (+ events/usage/exit_class),
  shared `run_provider_process` (probe remains a thin wrapper; no second
  subprocess stack), `json`/`stream-json` parsers with partial-output
  preservation on timeout/cancel, session/resume *metadata* without Team
  coupling, and `omg provider antigravity run`. Hermetic fake-`agy` only —
  no live-network CI. Ask cutover landed as #67-C; Team envelope as #67-D.
- **#105 Team resume consumes host gates (PR3 / seq E first slice):**
  `provider_session_result` replaces the fixed ACP stub in Team resume/view
  envelopes. CLI `omg team resume --provider-session` probes via
  `probe_host` → `evaluate_feature_gate(session_resume, required=False)` and
  injects the `FeatureGateResult` into `resume_with_view` (runtime does not
  re-parse versions). Outcomes stay independent: reconcile / provider_session
  (`available`|`legacy`|`blocked`) / tmux view — attach success never implies
  provider resume. Absent resume → LEGACY (actionable `next_action`); BLOCKED
  still fails closed when it occurs. No ACP transport; hermetic/fixture
  host-probe scope only — no live Antigravity CI claim.
  See `docs/host-compat.md`.
- **#105 host doctor/capability probe (PR2):** canonical
  `omg_cli/host_probe.py` + `host_models.py` report active Grok version,
  tested range (`0.2.107`…`0.2.121`), compatibility, and a bounded capability
  set (resume/close/restore-code/uuid-search) with truth order
  behavior → ACP advertisement → CLI inspect → version fallback.
  Feature gates are three-state (`AVAILABLE` / `LEGACY` / `BLOCKED`);
  `omg doctor` / `omg doctor --json` surface the host block without
  auth/session/transcript/home leaks. Hermetic fixtures under
  `tests/fixtures/host/`; operator note in `docs/host-compat.md`.
  Pin ≠ forced minimum — does not require every install to run v0.2.121;
  hermetic fixtures only, no live Antigravity CI claim in this change.
- **#105 Grok Build host-baseline gate (PR1):** independent host catalogue at
  `docs/parity/upstream-snapshots/grok-build.json` for pin
  `a5589e958437d79e13db026eedcb1720bffd4063` (`0.2.121`), fail-closed
  `GROK_BUILD` pin-transition reviews under `docs/parity/reviews/`, and
  generated `docs/parity/generated/host-baseline.md` +
  `host-capability-matrix.md`. Host rows are `catalogued` only (not a
  sibling of OMC/OMX parity scoring); `host_owned` cannot claim OMG
  `omg_paths` as implementation evidence.
- **#104 real-tmux Team UX regression suite:** hermetic Layer B coverage
  (`tests/test_team_real_tmux_ux.py` + `tests/support/team_tmux_harness.py`)
  on an isolated `tmux -S` socket with fake providers under
  `tests/fixtures/providers/`. Protects same-window leader visibility/focus,
  invocation race fail-closed, one-worker death, provider-ready/blocked/exit,
  bootstrap scrollback cleanliness, operator exact-pane I/O, scale topology,
  resume reconcile-only, and stop/rollback survivors. CI jobs
  `team-real-tmux-linux` / `team-real-tmux-macos` run `-m tmux_real` and
  must pass on this PR (GitHub branch-protection “required checks” are
  admin-owned and not claimed here).
  `live_team_smoke.py --interactive-ux` emits `interactive_evidence_v1` +
  `LIVE_TEAM_INTERACTIVE_UX_OK` (optional; fail-closed without tmux).
- **#103 Team resume/view attach semantics:** `omg team resume` stays
  reconcile-only by default (never attaches because stdout is a TTY).
  Explicit `resume --view` / `omg team view` restore the exact Team
  window/leader via a pure view planner + #102 topology target +
  identity re-probe before `select-*` / `switch-client` /
  `attach-session` (no lifecycle lock across interactive attach;
  `--takeover` required for `-d`; `--print` / `--json` never execute
  client effects). Reconcile, provider-session (ACP stub:
  `no_replay=true`, `restore_code=false`), and tmux-view outcomes are
  reported separately. `--worker` delegates to #101 focus.
- **#101 identity-fenced live pane inspect/operator input:** `omg team panes|capture|focus|key|input|watch` resolve Team identity → receipt
  chain → exact-pane proof (#98) → authorize → tmux effect (`shell=False`).
  Bounded/redacted capture; key allowlist; literal `send-keys -l`; audit
  stores length/hash only; `--json` never focuses or delivers input.
  Prefer durable `omg team api` for automation.
- **#102 preserve Team tmux topology across scale/relaunch:** persisted
  `view_mode` (same_window / dedicated_window / detached_session /
  legacy_windows) is the authority for scale-up, scale-down, resume, and
  relaunch. Non-legacy scale-up splits into the exact Team window (never
  `new-window`); logical_worker_index is separated from live pane/window
  coordinates; identity receipt schema v3 + team meta schema v2 carry
  topology fingerprints; post-commit `reconcile_layout` is cosmetic
  (`layout_repair_needed` does not roll back identity). Legacy
  window-per-worker adapters remain for pre-#96 runs.
- **#99 provider-ready Team startup:** schema-v2 monotonic phases
  (`pane_created` → `provider_spawned` → `provider_ready` → `task_dispatched`;
  optional `mailbox_ack`), pane `team supervisor` consuming vetted argv
  descriptors, provider readiness adapters, and Team statuses
  `running` / `degraded` / `failed_start` / `blocked_start` /
  `unverified_start`. Legacy v1 `worker-ready` receipts are
  `wrapper_ready_legacy` only and cannot false-green `startup_status=running`.
  Review hardening: process_stable is provisional (post-stable observe catches
  delayed auth/trust); gate requires distinct provider≠supervisor identity,
  live PID, and phase history including `provider_spawned`+`provider_ready`.
  Pro #99 blockers: process_stable / provisional finalize require matching
  provider binary identity (cmdline/exe basename or descriptor allowlist);
  supervisor tees bounded provider stdout to the pane tty; `needs_pty`
  records the real provider child PID (fail-closed if unresolved); pipes are
  drained after ready; TUI idle uses a longer post-stable floor; auth after
  finalize remains out of window (documented, not infinite watch).
- **Partial work for issue 67 (slice A / #67-A):** typed `omg_cli/providers/`
  Antigravity probe (discover binary, version argv probe, compat range,
  schema-versioned capabilities envelope) plus
  `omg provider antigravity {capabilities,doctor}`. Hermetic fake-`agy`
  fixtures only — no ask/Team cutover, no fabricated live-evidence verification claims. Issue #67
  remains open for slices B–D.

### Fixed
- **#105 PR2 doctor probe:** when ACP advertisement or inspect is present,
  omitted capability keys fail closed (no version fill). Partial
  `methods: ["session/resume"]` and empty `methods: []` no longer false-green
  `session_close` via semver. Doctor JSON scrubs home prefixes in
  `project_root.path`; docs clarify fixture/env injection vs live ACP.
- **#104 B1 leader operator visibility:** `_restore_leader_focus` now
  `select-window -t %pane` then `select-pane` so session `window_active`
  flips (select-pane alone is window-local). Postconditions require
  `window_active=1`; real-tmux asserts + negative self-check reject the
  hollow window-local `pane_active` check. Hermetic CI excludes
  `tmux_real` (`not live and not tmux_real`); dedicated artifact upload
  uses `if-no-files-found: error`.
- **#104 same_window scale-up geometry:** grow Team window before
  `split-window` when headless defaults leave no space for another pane
  (macOS GHA flake: scale returned with only leader+2 panes). Harness
  pins 160x48; scale UX test asserts distinct new `pane_id` + live count.
- **#104 scale inherits `executor=fixture`:** scale-up pane records now
  use `build_fixture_pane_command` when team meta has
  `executor=fixture`. Previously scale always built grok argv; on CI
  without `grok` the pane exited and tmux destroyed it while the API
  still returned `added=1` (TimeoutError waiting for 4th pane).
- **#104 misleading dedicated-window unit test name:** renamed
  `test_inside_tmux_splits_current_window` →
  `test_inside_dedicated_window_uses_new_window` so it no longer reads as
  locking the buggy new-window default (same-window default remains
  `test_inside_same_window_default_never_calls_new_window`).
- **#67-A probe fail-closed / process contract (PR #94 re-audit):** version and
  help probes require successful exit + observed evidence (no invented
  formats/efforts/modes); `run_probe_process` uses POSIX `start_new_session` +
  `killpg` on timeout/cancel/overflow with bounded output; `provider` is
  install/global-scoped; CLI routes via `ProviderAdapter`; neutral
  `ProviderCapabilities` defaults no longer carry Antigravity-positive claims.
- **#67-A PR #94 GPT Pro re-audit round 2:** `_parse_help_supports` is
  fail-closed on structured flag/enum/subcommand boundaries only (colliding
  prose like `allows`/`plan`/`low`/`--project` cannot forge evidence);
  `run_probe_process` kills the process group on KeyboardInterrupt/BaseException
  before joining pipe readers, and version/help probes pass `cancel_event`
  (SIGINT-wired); `omg provider antigravity doctor --json` uses
  `success`/`failure` envelopes (`E_PROVIDER_DOCTOR`) instead of splatting
  `DoctorReport.ok` over `success()`. Top-level `omg doctor --strict` remains
  out of scope for the Antigravity probe in slice A.
- **#67-A PR #94 GPT Pro re-audit round 3:** post-`Popen` setup failures and
  cancel/SIGINT through the wait/join/close window always `_kill_tree`; success
  drains readers to EOF (forced stop sets truncation flags; truncated help
  fails closed); `OMG_AGY_BIN` requires `agy` basename + `Usage of agy` help
  identity; `parse_version` is first-line-anchored; tested compat window is
  fixture-backed `1.1.10` only; provider JSON/doctor errors run through
  `redact_text`/`redact_value`.
- **#67-A PR #94 GPT Pro re-audit round 4:** `Popen` and post-spawn work share
  one BaseException+`_kill_tree` region (`proc=None` then nested OSError
  convert); result construction / final cancel check kill before return;
  `_run_probe_argv` `killpg`s via returned `pid` before `KeyboardInterrupt`;
  `parse_version` rejects leading zeros and enforces explicit digit/value caps
  (ASCII `[0-9]`, typed `ProviderVersionError` — not CPython digit-guard only).

## [0.7.6] - 2026-08-06

### Fixed
- **Release install gate / `omg update` dogfood (#89):** install-time dual-pass
  doctor probe (`--strict` then non-strict) for **both** release and
  development — coexistence-only soft risks become `completed_with_warning`;
  integrity FAILs stay fail-closed. Bare `rc=2` without dual-pass evidence is
  rejected; malformed dual-pass fields never coerce into legacy `rc=0`. Dual-pass
  success also requires a consistent aggregate matrix (`strict=0` ⇒
  `relaxed=None` + `rc=0`; soft ⇒ `rc=2`) so contradictory evidence cannot
  classify as installed. Exact same-digest installs reuse a receipt only when
  mode, release asset/checksum evidence, status authority, and live Grok host
  plugin path/enabled authority match; otherwise they re-attest
  (development→release promotion writes a release receipt without host churn,
  and warning status is never downgraded) or fall through to a full
  uninstall/install/enable transaction when the host path has drifted or the
  plugin is disabled. Gate failures print a
  bounded (64 KiB) non-strict doctor transcript. Authoritative receipts record
  pending + post-publication probe hashes and the stricter status. `omg update`
  uses one `VerifiedCurrentInstall` authority; release receipts and
  unprovable/dirty development installs promote through stage `install.sh`
  (source preserved); clean development still fast-forwards +
  `install-plugin.sh`. Interactive `omg doctor --strict` coexistence semantics
  are unchanged.
- **Parity release claim gate (PR #91 Pro re-audit):** expand overclaim scan to
  `CHANGELOG.md` / `docs/skills.md`; keep forbidden-phrase restrictions active
  until `inventory_completion_claims_allowed` (category + source complete);
  bind required snapshot filename → `source`; bind refresh ack `detail` to
  promise/source_paths before→after values so same-revision stale acks cannot
  replay.
- **Parity release claim gate (PR #91 Pro re-audit round 3):** durable release
  base (previous `v*` tag / `--base-ref` / `OMG_PARITY_BASE_REF`, not `HEAD^`);
  scan intermediate pin transitions; require git-tracked HEAD-blob-matching
  review ledgers; own `docs/parity/reviews/**` under OMG-W0; keep live-maturity
  phrase scan active unless every capability reaches the top live-maturity tier;
  expose
  `--base-inventory` / `--base-ref` on `omg parity check`.
- **Parity release claim gate (PR #91 Pro re-audit round 4):** `--release` rejects
  file-only `--base-inventory` (no git provenance → endpoint-only A→A miss on
  A→B→A mid pins). Pair `--base-inventory` with `--base-ref` whose inventory
  blob matches the file; never silently prefer the file over `--base-ref`.
- **Parity release claim gate (PR #91 Pro re-audit round 2):** require committed
  pin-transition reviews under `docs/parity/reviews/`; bind deleted-change
  fingerprints; validate upstream snapshot capability schema (no duplicate /
  malformed silent skip); forbid live-evidence marketing phrases in docs and
  scrub historical CHANGELOG wording.

### Added
- **Parity full upstream inventory (#78-B):** expand the v2 catalogue with
  `source_status` (OMC/OMX/OmO/Antigravity), the #78-B category taxonomy,
  minimum OMC/OMX/OmO/Antigravity capability rows (mostly `catalogued`; no
  fake top-tier live-maturity labels), generated per-source matrices + SUMMARY (EN/zh/zh-TW),
  and NON-AUTHORITATIVE banners on historical research matrices. Inventory
  completeness ≠ product parity — `inventory_status` stays `bootstrapping` while
  `parity_governance` / `platform_live_evidence` remain open. Issue #78 stays
  open after #78-C for remaining maturity / live-evidence work.
- **Partial work for issue 78 (slice C / #78-C):** seed pinned upstream snapshot
  catalogues (`docs/parity/upstream-snapshots/`), `omg parity refresh --plan`
  review workflow, release claim gate (`--strict --release` in `release.yml`),
  and live-evidence freshness enforcement. Completeness promotion and issue #78
  remain open — hermetic catalogue only; no fake top-tier live-maturity product claims.

## [0.7.5] - 2026-08-05

### Added
- **Parity inventory v2 (#78-A):** claimability-safe schema with ordered maturity,
  expanded classifications, bootstrapping canonical inventory for open P0 gaps
  (#67/#68/#69/#78 remaining), `omg parity check|gaps`, generated
  `FEATURE-MATRIX.md`/`GAPS.md` (no percentages while bootstrapping), and CI
  `--strict` gate. Issue #78 remains open for #78-B/#78-C.

### Fixed
- **Parity claimability / upgrade safety (PR #85 Pro re-audit):** upgrade from
  older managed installs tolerates missing newly required shipping roots
  (`docs/parity`) on prior identity/rollback while still requiring them on the
  candidate package; alias maturity cannot outrank its canonical target (and
  cannot positive-claim when the target is host_impossible/excluded/
  optional_unclaimed); `--strict` requires non-empty `omg_paths` for claimable
  classifications and repo-verifiable `healthy_evidence` for `healthy` /
  top live-maturity tiers.
- **Process-fanout cancel linearization (Round 18 / R18-1):** each
  ``run_process_fanout`` worker's cancel recheck → ``Popen`` → PID publish
  runs under the same per-run ``transition_guard`` as ``_launch_grok``
  (``launch_refused_for_cancel``); refused spawns do not Popen and roll back
  prior workers (R15-4). Closes Pro R17b P1 where legacy cancel could miss a
  later fanout worker.
- **Legacy ``set_verified`` cancelled absorbing (Round 18 / R18-2):** legacy-v1
  ``set_verified`` re-reads under ``transition_guard`` and refuses
  ``cancelled`` → ``verified`` unless ``force=True`` with the in-process force
  capability; already-``verified`` is idempotent. Closes the product bypass
  where post-cancel ULW acceptance could still stamp verified.
- **``clear_active`` / ``create_run`` create.lock (Round 18 / R18-3):**
  ``active.json`` read/compare/unlink shares ``.omg/state/create.lock`` with
  ``create_run``; legacy cancel clears active only after releasing
  ``transition_guard`` (no create-while-transition lock-order reversal).
  Prevents cancel from unlinking a newer run's active pointer.
- **Legacy cancel / launch linearization (Round 17 / R17-1):**
  ``_cancel_run_legacy`` now acquires the same per-run ``transition_guard`` as
  ``_launch_grok``: commit ``cancelled`` + snapshot PIDs under the guard, then
  signal outside. Closes the Pro R16 P1 race where legacy cancel could mark
  terminal while launch still Popens after its pre-check. Legacy
  ``write_status`` treats ``cancelled``/``verified``/``completed``/``failed``
  as absorbing (guarded no-op) so post-launch writers cannot resurrect a
  cancelled run. Pipeline / process-fanout ULW remain legacy-v1 (strict-v2
  would require ``execution_lease`` on every stage write — not a cheap upgrade);
  linearized legacy cancel is the supported closure for those paths.
- **Cancel-aware `_launch_grok` gate (Round 16 / R16-1):**
  ``modes._launch_grok`` re-checks terminal status /
  pending ``cancel.request.json`` under the same ``transition_guard`` as
  ``prepare_leader_spawn`` (shared ``launch_refused_for_cancel`` with
  autopilot) so ralph/ralplan cannot Popen after cancel.
- **Autopilot FSM public contract:** split `MANUAL_TRANSITIONS` vs
  `COMMIT_ONLY_TRANSITIONS`; `omg autopilot status` `legal_next` no longer
  advertises `verified` as a `transition()` edge (use `commit_only_next` +
  `terminal_action: omg autopilot complete`). Advance-gate failures are
  recorded on status / stall JSON instead of being swallowed silently.
- **Resume ralplan hint:** drop dead `omg ralplan --resume` placeholder
  (`--resume` is ralph-only); recommend `omg state --run` + `--run` re-invoke.
- **Interview/consensus gates:** prefer CLI-owned stamps; bare
  `interview_complete` / `consensus` booleans require `break_glass=true` and
  are audited on history (`gate_audit`). StageEvidenceEnvelope v3 still planned.
- **Review/QA fingerprint recheck:** `stage_review_is_clean` / `stage_qa_is_clean`
  re-validate `diff_hash` lane stamps and `product_hash` against the current
  workspace; drifted or tampered stamps fail closed (legacy hash-less stamps
  keep weaker clean-flag-only behavior).
- **Implement→review work gate:** require workspace fingerprint drift since
  implement entry, a real on-disk CLI implementation receipt, or audited
  `no_change_reason` / inline receipt with `break_glass=true`; unattended
  auto-advance records `gate_failure` instead of silently skipping.
- **Implement-gate fingerprint (Codex PR review P1):** the implement→review
  work gate no longer reuses `qa.product_hash` (which only hashes
  `omg_cli/**/*.py`, missing changes confined to `plugin.json`, `hooks/`,
  `skills/`, `agents/`, `templates/`, or non-`.py` files under `omg_cli/`).
  A dedicated `autopilot._implement_workspace_fingerprint` helper covers
  those curated product surfaces without changing `qa.product_hash`
  semantics used for UltraQA acceptance.
- **Implementation receipt now real (Codex PR review P2):** added
  `omg_cli/implementation.py` with `stamp_implementation_receipt` /
  `read_implementation_receipt`, writing a CLI-owned
  `stages/implementation.json` (`writer=omg-cli`, `content_sha256`,
  `stamped_at`) under `.omg/state/runs/<run_id>/`. The implement→review gate
  now actually loads and trusts this on-disk stamp (fingerprint-rechecked)
  without `break_glass` — previously the docs advertised this path but no
  producer/reader existed, so it always fell through to the unauthenticated
  inline-receipt break-glass path.
- **Implementation receipt bound to implement cycle (Codex PR review P2):**
  a receipt stamped during one implement cycle no longer satisfies the
  implement→review gate for a later cycle whose fingerprint happens to
  still match (e.g. `review → ralplan → implement` with no new product
  work). `implementation.invalidate_implementation_receipt` marks any
  leftover receipt stale on every (re)entry into `implement`, mirroring
  `invalidate_quality_stages`; `read_implementation_receipt` treats
  `invalidated=true` the same as a missing file.
- **Implement-gate fingerprint excludes generated caches (Codex PR review
  P2):** `_implement_workspace_fingerprint` no longer hashes
  `__pycache__/`, `*.pyc`, `.pytest_cache/`, `.ruff_cache/`, `.mypy_cache/`,
  or `*.egg-info/` (mirrors this repo's own `.gitignore` cache entries) —
  previously, merely running tests or importing an `omg_cli` module during
  `implement` could write/update bytecode with zero source edits and
  falsely register as implementation work.
- **Autopilot sidecar writes:** `autopilot.json` and stage invalidation use
  temp-file + `os.replace` atomic publish (mini-WAL; cross-file WAL still planned).
- **Resume bundle v1:** `omg resume` emits partial `resume_bundle`
  (`schema_version=1`: `run_view`, `gate_failure`, `autopilot_phase`,
  `legal_next`, `provenance`) — wiki/memory/compaction deferred.
- **Tracker process identity:** lease PID match fails closed when
  `process_starttime` is unavailable (unknown ≠ same process).
- **`set_verified` force hatch (Round 14):** `force=True` requires an
  in-process capability from `enable_force_verified_for_tests()` (tests
  only); env `OMG_INTERNAL_FORCE_VERIFIED` alone is insufficient (deprecated
  no-op). Not exposed on any CLI argparse path.
- **Review gate workspace binding (Round 2 / R2-2):** `omg review`
  records `workspace_fp` (`_implement_workspace_fingerprint`) on every
  structured review stamp; `stage_review_is_clean` requires it to match the
  current workspace (missing on schema_version≥2 stamps → fail-closed).
- **QA gate workspace binding (Round 2 / R2-1):** clean UltraQA cycles
  also record `implement_workspace_fp`; `stage_qa_is_clean` rechecks it
  alongside `product_hash` so non-`omg_cli/**/*.py` product-surface drift
  after QA went clean cannot reach acceptance.
- **Blocked recovery gates (Round 2 / R2-3):** destination gates run for
  `blocked→implement` / `ralplan` / `review` / `qa` — e.g.
  `blocked→review` still requires implement work evidence;
  `blocked→qa` still requires a fresh clean review stamp; re-entering
  `review` from `blocked` invalidates stale review/QA stamps.
- **Interview trusted path envelope (Round 2 / R2-4):** `_interview_complete`
  requires a CLI-owned `interview.json` complete envelope (writer, run_id,
  spec artifact + content hash) — a bare `status=complete` string alone
  no longer unlocks `ralplan`.
- **Consensus stamp-only (Round 2 / R2-5):** `_consensus_ready` trusts only
  CLI `ralplan.json` with `writer`, `run_id`, and `accepted=true`;
  `status.ralplan_consensus` and artifact markers alone no longer unlock
  `implement`.
- **Implement-gate fingerprint entrypoints (Round 3 / R3-1):**
  `_implement_workspace_fingerprint` now includes `scripts/` and `bin/` so
  install/CLI surface edits register as implement work and bind review/QA
  freshness rechecks — previously those paths were omitted from the curated
  fingerprint tuple.
- **Ralplan stamp invalidation on replan (Round 4 / R4-1):** any
  non-interview re-entry into `ralplan` (including `review→ralplan`,
  `qa→ralplan`, and `blocked→ralplan`) invalidates the prior accepted
  `ralplan.json` stamp and review/QA stamps — a stale accepted consensus
  can no longer silently unlock `implement` without a fresh ralplan cycle.
- **Interview attach-run for autopilot (Round 4 / R4-2):** added
  `omg interview start --attach-run RUN_ID` to seed CLI interview envelopes
  under an existing autopilot run in phase `interview` (task defaults to
  the run goal; fail-closed on phase/mode mismatch or task/goal disagreement).
- **Ralplan invalidation cleared on fresh accept (Round 5 / R5-1):** writing
  `accepted=true` to CLI `ralplan.json` (legacy + strict-v2 paths) clears
  `invalidated` / `invalidated_reason` / `invalidated_at`; a new strict-v2
  consensus attempt also clears stale invalidation at cycle start so a prior
  replan fence does not block a genuinely fresh ralplan round.
- **Ralplan invalidation on any re-entry with prior stamp (Round 5 / R5-2):**
  entering `ralplan` when a CLI-owned `ralplan.json` already exists for the
  run_id invalidates that stamp and review/QA stamps regardless of `src` —
  closes `review→blocked→interview→ralplan` bypass where a detour through
  `interview` made `src=="interview"` again while a stale accepted stamp
  still sat on disk; first `interview→ralplan` with no stamp remains a no-op.
- **Interview attach-run re-authorize under lease (Round 5 / R5-3):**
  `omg interview start --attach-run` re-loads the run and re-checks
  mode/phase/non-terminal/goal match after acquiring the execution lease,
  closing a TOCTOU race between pre-lease attach checks and the interview
  envelope write.
- **Embedded ralplan bound to frozen autopilot goal + phase (Round 5 / R5-4):**
  `omg ralplan * --run RUN` against an autopilot run requires
  `phase==ralplan` and a `--run` goal that exactly matches the frozen run
  goal; `_consensus_ready` also rejects accepted stamps whose `goal` field
  disagrees with the current run goal (defense in depth).
- **Ralplan history reset on fresh replan (Round 6 / R6-1):** starting a
  new strict-v2 consensus attempt against a state left `invalidated: True`
  by a prior replan now calls `_reset_for_fresh_cycle` (wipes `history`,
  per-session `attempts`, and `round`) so `first_round` starts at 1 and the
  configured `max_rounds` ceiling is available again — previously, stale
  history past the ceiling pinned every future replan into an immediate block
  with zero stages executed.
- **Attached interview close resume includes `--run` (Round 6 / R6-2):**
  `omg interview close` on an autopilot-attached interview
  (`--attach-run`) now sets `resume_command` to
  `omg ralplan <goal> --run <run_id>` instead of a standalone
  `omg ralplan <goal>` that would spawn an orphaned ralplan run.
- **Attached interview close resume via autopilot transition (Round 7 / R7-0):**
  attach-mode `resume_command` now chains
  `omg autopilot transition --run <run_id> --phase ralplan` before
  `omg ralplan <goal> --run <run_id>` — the phase sidecar is still
  `interview` at close time, so a bare embedded ralplan would be rejected
  (phase mismatch) until the transition advances the FSM.
- **Cancelled not reachable via generic transition (Round 7 / R7-1):**
  `cancelled` is removed from `MANUAL_TRANSITIONS` / `legal_next`; only
  `omg cancel` / `cancel_run` may enter the terminal `cancelled` phase —
  `transition(..., "cancelled")` raises cleanly without mutating state.
- **ralplan_epoch gates re-entry invalidation (Round 7 / R7-2):** a
  monotonic `ralplan_epoch` on autopilot state drives quality/consensus
  invalidation on re-entry to `ralplan` (epoch ≥ 1), not stamp existence —
  the first `interview→ralplan` handoff (epoch 0→1) remains a no-op even
  when no stamp exists yet; `--skip-interview` starts at epoch 1.
- **Consensus stamp requires matching non-empty goal (Round 7 / R7-3):**
  `_consensus_ready` for autopilot runs requires a non-empty string `goal`
  on the CLI `ralplan.json` stamp that exactly matches the frozen run goal
  (missing/null/empty → fail-closed).
- **Reject legacy schema for autopilot ralplan embedding (Round 7 / R7-4):**
  `omg ralplan * --run RUN` against `mode=autopilot` rejects non-strict-v2
  run schemas — autopilot embedding no longer enters the legacy-v1 FSM path.
- **Fresh replan session reset (Round 8 / R8-1):** `_reset_for_fresh_cycle`
  now mints new `session_id` UUIDs for planner/architect/critic (not reused
  with `attempts` zeroed) so a fresh strict-v2 replan cycle does not inherit
  stale session identity from the prior round.
- **Ralplan embedding cancel race (Round 8 / R8-2):** `_authorize_autopilot_embedding`
  rejects terminal autopilot statuses and pending cancellation requests;
  strict-v2 embedding re-authorizes immediately before writing
  `accepted=true` so a concurrent `omg cancel` cannot lose the race to
  acceptance.
- **Interview sidecar cancel race (Round 8 / R8-3):** interview attach/close
  paths call `_assert_run_writable` under the execution lease immediately
  before every sidecar write — terminal runs and pending cancellation
  requests fail closed without writing `interview.json` or spec artifacts.
- **Missing ralplan_epoch conservative migration (Round 9 / R9-1):**
  pre-R7 ``autopilot.json`` sidecars that lack ``ralplan_epoch`` no longer
  default to ``0`` on load — only a run still at ``phase==interview`` with
  no CLI ``ralplan.json`` stamp and ``cycles.ralplan==0`` migrates to ``0``;
  every other missing-epoch run migrates to at least ``1`` so re-entry
  invalidates stale consensus/quality stamps. Present values must be plain
  ``int >= 0`` (bool/float/negative rejected).
- **Terminal/cancel gates on transition/resume (Round 9 / R9-2):**
  ``transition()`` re-checks ``status.json`` under the execution lease and
  refuses sidecar writes when the run is terminal or has a pending
  cancellation request; ``omg autopilot run --resume`` prefers terminal
  ``status.json`` over a stale non-terminal sidecar ``phase``;
  ``status_autopilot`` returns empty ``legal_next`` for terminal runs.
- **Idempotent attach interview resume (Round 9 / R9-3):** attach-mode
  ``omg interview close`` sets ``resume_command`` to
  ``omg autopilot run --resume <run_id>`` (single idempotent entry); re-close
  of an already-complete interview migrates stale two-step resume commands on
  disk to the current form.
- **Consensus stamp strict-v2 schema (Round 10 / R10-1):**
  ``_consensus_ready`` requires ``schema_version==2`` and
  ``lifecycle_version==2`` on CLI ``ralplan.json`` (fail-closed if missing
  or wrong) — autopilot only embeds strict-v2 RALPLAN.
- **Interview writable assert on all sidecar saves (Round 10 / R10-2):**
  ``answer_interview``, ``pressure_pass_interview``, and non-attach
  ``start_interview`` reload the run under the execution lease and call
  ``_assert_run_writable`` before ``_save`` (same cancel/terminal gate as
  attach/close).
- **Ralplan re-authorize before each sidecar save (Round 10 / R10-3):**
  strict-v2 embedded ralplan calls ``_assert_autopilot_still_writable``
  immediately before every ``save_ralplan_state`` (not only the accept
  write) so a mid-round ``omg cancel`` cannot land a history/stage save
  on a cancelled run.
- **Resume preflight advance completed interview (Round 11 / R11-1):**
  ``run_autopilot`` advances a completed interview to ralplan before any
  Grok launch so ``--resume`` does not spawn an unnecessary interview
  session.
- **Stricter missing ``ralplan_epoch`` migration (Round 11 / R11-2):**
  migrate missing epoch to ``0`` only when phase is interview, no CLI
  ralplan stamp, ``cycles.ralplan==0``, history has no post-interview
  phases (missing/corrupt history fails closed to >=1), and no clean
  review/QA stamps or implementation receipt; otherwise migrate to >=1.
  Persists ``ralplan_epoch_source`` (``native`` / ``migrated``).
- **Refuse grok launch on cancel (Round 11 / R11-3):**
  ``_launch_grok`` / resume launch re-checks ``status.json`` and pending
  ``cancel.request.json`` and refuses Popen when the run is terminal or
  has a pending cancellation request.
- **Refuse bare ``omg accept`` for autopilot (Round 12 / R12-1):**
  ``cmd_accept`` refuses autopilot-mode runs (directs operators to
  ``omg autopilot complete``); ``set_verified`` requires sidecar
  ``phase==acceptance`` for autopilot (fail-closed unless force hatch).
- **Implementation receipts require lease ``invocation_id`` (Round 12 / R12-2):**
  ``stamp_implementation_receipt`` writes the active execution-lease id;
  ``read_implementation_receipt`` rejects missing/empty ``invocation_id``
  so a hand-written ``writer=omg-cli`` file cannot forge the gate.
- **Exclusive autopilot driver flock (Round 12 / R12-3):**
  ``run_autopilot`` holds a non-blocking exclusive flock on
  ``autopilot.driver.lock`` for the whole invocation so concurrent
  ``--resume`` cannot double-launch Grok.
- **Linearize grok spawn under transition_guard (Round 13):**
  cancel check + ``Popen`` + pid publish run under a short
  ``transition_guard`` so cancel cannot miss a newly spawned Grok;
  kill the child if pid publish fails.
- **Force verified process-private only (Round 14 / R14-1):**
  ``set_verified(force=True)`` requires ``enable_force_verified_for_tests()``;
  env alone does not unlock force.
- **Implementation receipts + live lease binder (Round 14 / R14-2):**
  ``stamp_implementation_receipt`` requires a live ``ExecutionLease`` and
  rebinds ``implement_receipt_binder``; implement entry clears the binder
  so a hand-copied receipt cannot unlock the next cycle.
- **Refuse spawn on live leader pid (Round 14 / R14-3):**
  resume/spawn refuses when ``pid.json`` still matches a live leader
  (PID + starttime); also refuses a live PID without starttime (do not
  clear/spawn).
- **Require starttime for pid publication (Round 14 / R14-4):**
  ``write_pid_metadata`` fails closed without starttime; fanout kills the
  worker if publish fails after spawn.
- **Authority nonce + phase bind accept/verified (Round 14 / R14-5):**
  ``authority_nonce`` / ``authority_phase`` bind ``status.json`` ↔
  ``autopilot.json``; bare ``omg accept`` refused whenever the autopilot
  sidecar exists; ``set_verified`` requires matching nonce and
  ``authority_phase==acceptance``. Residual: dual-edit of both files under
  a writable ``.omg/state`` / OS write-deny still out of scope.
- **Tri-state PID identity UNKNOWN refuse (Round 15 / R15-1):**
  live-leader spawn classifies ``MATCH`` / ``MISMATCH`` / ``UNKNOWN``;
  ``UNKNOWN`` (alive + recorded starttime + ``process_starttime`` probe
  ``None``) refuses spawn and does not clear ``pid.json`` (unknown ≠
  reclaimable).
- **Stamp requires target-run lease (Round 15 / R15-2):**
  ``stamp_implementation_receipt`` binds via ``_require_current_lease``
  (lease root/run must match the stamp target), refuses terminal /
  cancel-pending, and requires autopilot ``phase==implement`` when the
  sidecar exists — a foreign run's live lease cannot stamp this run.
- **Ralph resume live-leader gate (Round 15 / R15-3):**
  ``modes._launch_grok`` / ralph ``run_mode`` resume share
  ``prepare_leader_spawn`` with autopilot — refuse MATCH / UNKNOWN /
  missing-starttime; clear only DEAD / MISMATCH stale leaders.
- **Fanout kill prior workers on publish fail (Round 15 / R15-4):**
  when a later worker's ``pid.json`` publish fails after ``Popen``, kill
  and reap all earlier workers in the batch, persist failure evidence,
  and mark the run failed (no orphan batch).

### Planned
- Optional residual team API ops (broadcast / await-event / preflight pack) —
  not blockers for production path; full OMX 33-op not claimed.
- Optional CI wire for live team smoke (quota-heavy; fixture path already hermetic).
- Optional PyPI/`pipx` CLI track — **shipped editable-only** (`pyproject.toml` +
  `pipx install --editable` / `pip install -e .`); non-editable wheel / PyPI
  publish still deferred (`plugin_root()` needs checkout siblings).
- Optional PR to xAI plugin-marketplace (sha-pinned) — **deferred / prep-only**
  (document prerequisites in `docs/RELEASE.md`; do not submit).
- Host Stop veto (not feasible on Grok today).
- Full OMC semantic LSP proxy (host-owned `.lsp.json` registration ships in 0.6.0;
  OMG does not claim host health or proxy hover/rename/goto operations).
- StageEvidenceEnvelope v3 / full ResumeBundle / autopilot cross-file WAL (from
  external Pro+GitHub review) — Round 1 landed partial `resume_bundle` v1 +
  atomic single-file writes; full envelope + WAL deferred.

## [0.7.4] - 2026-08-01

### Fixed
- **Team scale/resume crash recovery (window readback + identity receipts):**
  generation-scoped WAL before side effects; fail-closed scaled-window ownership
  readback (`@omg_scale_nonce` + rename → exact `display-message`); pending
  identity-receipt generations and scale-up WAL gate join/collect/stop/integrate;
  scale-down recovery binds receipt victims (not re-drain) with generation/task
  ids in errors; meta commit result-loss classifies committed/not_committed/
  unknown without treating volatile `last_scale.actions` as sole identity;
  remain-on-exit dead panes clean then commit `needs_collect` when process
  absent. Integration isolation only — not an execution sandbox.
- **Hermetic scale recovery tests** without live `tmux` (mock dead-pane cleanup
  on Ubuntu CI); stop SIGKILL process-group reaping poll.

## [0.7.3] - 2026-07-30

### Added
- **`omg autopilot run --unattended`** (#40): hands-off outer loop re-launches
  Grok after host-turn stalls (no human `go`); pauses on interview/`await`;
  `--max-stall-relaunches` budget; machine JSON on stdout, resume hints on stderr.
- **`omg team {start,launch} --plan-only`** (#27): side-effect-free plan JSON
  (no `.omg` / worktrees / tmux). `--materialize-only` alias for mutating dry-run;
  CLI never prints `Team started` for non-live modes.
- **`omg lsp validate`** and stable `E_LSP_*` codes (#28); legacy
  `check`/`symbols`/`diagnostics` return `E_LSP_HOST_OWNED` with `next_action`.
- **`omg_cli/command_registry.py`** (#29 Phase 1): authoritative top-level
  `KNOWN_SUBCOMMANDS` / `CommandSpec` inventory.
- **CLI composition (#29 / #30):** handler families extracted from `main.py`
  (`commands/inspect|install|run|memory|workflow|modes|mcp|team`); argparse
  registration leaves `main` as composition-only; global **`--json`** +
  `CommandContext` / schema_version 1 envelopes; golden envelope tests;
  `docs/cli-commands.md` + `docs/cli-contract.md`.
- **Team process-level readiness (P0-1, #62):** pane commands run
  `omg team worker-ready` before the agent binary; launch success counts
  process receipts **or** mailbox ACK so read-only posture panes are not
  structurally `failed_start`.
- **Team P0′ reliability API (#63):** heartbeat read/update, worker status,
  shutdown request/ack, orphan-cleanup; `omg doctor` soft check for team
  gate/tmux/API surface.
- **Team plane default-on (#64):** enabled unless `OMG_DISABLE_TMUX_TEAM=1`
  (legacy `OMG_EXPERIMENTAL_TMUX_TEAM=0` still disables). Fixture smoke:
  `FIXTURE_TEAM_SMOKE_OK` via `scripts/live_team_smoke.py --fixture-executor`.
- **Team P0′ task/events/manifest API (#65):** `read-task`, `update-task`
  (CAS `expected_version`), `read-manifest`, `append-event`, `read-events`;
  live process-ready gate + stop kill-grace / pane rebind.
- **Grok live team smoke (#66):** `scripts/live_team_smoke.py --live` achieved
  `LIVE_TEAM_SMOKE_OK` (2026-07-30 local; process-ready gate + stop proof).
- **Deterministic release packager + staged publish** (#26 / #42).
- **CI:** full-package static analysis entrypoint; macOS platform contract lane.

### Documentation
- Autopilot EN/zh/zh-TW + skills: primary hands-off path is
  `omg autopilot run --resume … --unattended` (no longer “forthcoming”).
- Clarified the external-agent CLI PreToolUse contract: direct provider
  execution remains denied, while passive discovery, path inspection, and inert
  literals are allowed.
- **Team plane docs sync (post-promotion):** skills / security-model / README /
  RELEASE (EN+zh+zh-TW), `CLAUDE.md`, and `templates/omg-rules.md` document
  default-on + kill switch + `LIVE_TEAM_SMOKE_OK` (local) + integration-only
  isolation honesty (no longer experimental-gate language).

## [0.7.2] - 2026-07-28

### Fixed
- Global PreToolUse external-CLI detection now distinguishes quoted arguments,
  comments, and heredoc data from executable shell syntax (for example
  `git commit -m "fix(kimi): ..."`), while still denying real substitutions,
  continued commands, shell/eval bodies, and external CLI execution.
- Live `omg team resume` relaunch now acquires the same run-dir scale lock as
  `omg team scale`, refusing concurrent scale/resume that could double-spawn
  panes or last-writer-wins `team.json`.
- `omg team api transition-task-status` now requires `worker` and binds it to
  claim/task owner (same floor as `release-task-claim`), blocking cross-worker
  token completion. Worker panes also drop client-supplied `owner_token` when
  pane env has none.

### Added
- Identity-matrix hermetic coverage for worker-env mailbox/claim/transition/
  release impersonation + `owner_token` strip when pane env has none.
- Inside-tmux shorthand launch opens a dedicated team window + split panes in
  the current session (`attach_mode=inside`); stop kills only that window/panes
  — never the shared session or leader pane. Outside TTY creates a detached
  session and prints an attach hint; non-interactive live launch requires
  `--detach` (fail-closed).
- Shorthand `omg team launch` waits for worker mailbox ACKs (body `ACK` to
  `leader-fixed`) before reporting success. Knob: `OMG_TEAM_READY_TIMEOUT_MS`
  (default 45000). Partial ACK → `startup_status=degraded`; zero →
  `failed_start`; both exit non-zero and leave state for diagnosis.
- OMX-like team launch shorthand: `omg team [N[:role]] "<goal>"` → `launch`
  (split-pane topology, goal decomposition, team-name ref index, P0 api board
  seed, `skills/omg-team`). Still gated by `OMG_EXPERIMENTAL_TMUX_TEAM=1`;
  not promoted to full live OMX `$team` parity yet.
- Team pane workers may use identity-bound `omg team api` (ACK/claim/mailbox
  self); process-fanout / spawned-subagent contexts stay denied.
- Experimental `omg team api <op> --input JSON [--json]`: OMX-shaped P0 façade
  over mailbox + claim/transition task store (11 ops). Remaining OMX ops stay
  `E_TEAM_API_UNIMPLEMENTED`; full 33-op parity is not claimed. Still gated by
  `OMG_EXPERIMENTAL_TMUX_TEAM=1`. Fail-closed: requires CLI-stamped `team.json`
  control plane before materializing mailbox/task stores.

### Changed
- `docs/security-model.md`: document that identity receipts omit `pane_command`/
  `worktree`, and that `owner_token` is a same-UID shared secret (not
  cross-user isolation).
- Hardened `scripts/live_team_smoke.py --live` as the team promotion gate:
  asserts `dry_run=false`, pane count, grok (not fixture) pane commands,
  owned worktrees, ≥N ACKs, claim→completed, and stop clears only the owned
  session; prints `LIVE_TEAM_SMOKE_OK` only if all pass. Dry path still prints
  `DRY_TEAM_SMOKE_OK`. Live attempt on 2026-07-25 exited non-zero
  (`startup_status=failed_start`, ACKs=0) — **not** promoted; experimental
  gate `OMG_EXPERIMENTAL_TMUX_TEAM=1` remains. Optional wire in
  `scripts/live_suite.sh` (`OMG_LIVE_TEAM=1` / quota-heavy).

## [0.7.1] - 2026-07-24

### Fixed
- Release-verify writer-ownership gate: assign owners for host-launch/madmax
  modules, `CODE_OF_CONDUCT.md`, docs research/superpowers trees, plans, and
  historical locale/README rename paths so tag verification no longer fails
  closed on unowned dirty records.

## [0.7.0] - 2026-07-24

Host-launch parity with OMX (bare interactive + `--madmax` + launch policy).

### Added
- OMX-aligned host launch: bare `omg` / `omg "<prompt>"` launches interactive
  Grok at safe defaults; `omg --madmax` remains full-open break-glass
  (`--always-approve` + `--permission-mode bypassPermissions`). Shared transport
  policy via `OMG_LAUNCH_POLICY` / `--direct` / `--tmux` (auto falls back;
  explicit `--tmux` fails closed). First `--` suffix is opaque.

## [0.6.0] - 2026-07-23

Evidence-gated product composition across the parity workstreams. This release
adds public CLI routes without promoting unobserved Grok-native capabilities.

### Added
- **Repository workflows:** immutable `repository-workflow/v1` registry,
  deterministic plan/task IDs and waves, explicit permission intersection,
  externally gathered task receipts, replay/effect fences, and independent
  verifier + skeptic `ship` decision. Grok `/create-workflow` and Rhai stay
  `optional_unclaimed` until stable public schema plus fresh invocation proof.
- **Session continuity:** exact create/resume/continue/fork argv routes and
  bounded immutable JSONL recovery that preserves `W_BROKEN_CHAIN` and unknown
  record warnings instead of fabricating full history.
- **Project services:** redacted deterministic fact memory, generation-fenced
  lifecycle tracker, lossless compaction checkpoints, and outbound-only,
  non-authoritative notification adapters.
- **Host discovery manifests:** conventional `.mcp.json` and `.lsp.json` using
  `${GROK_PLUGIN_ROOT}`. `omg capabilities` reports configured, installed,
  enabled, loadable, observed, healthy, and verified independently.
- **Parity/release routes:** `omg parity run` delegates the frozen W0 manifest
  engine; `omg parity release-readback` rejects missing, extra, renamed, or
  byte-drifted prebuilt assets.
- **GitHub-only install:** convenient latest-release bootstrap plus a pinned
  manual/offline archive path. Both verify `SHA256SUMS`, switch transactionally,
  run strict doctor, emit a receipt, and roll back failed activation.

### Security
- Workflow planning never launches shell agents. Runtime receipts must bind to
  planned task IDs/actors, and effective permission is the repository/host/
  launch intersection. Notifications never own run status. OMG does not probe
  private sidecars or infer native health from config files.

### Fixed
- **Workflow runner:** executor liveness is observed before draining the result
  pipe, so a child that publishes its receipt and exits between an empty poll
  and the liveness check is no longer misclassified as
  `E_WORKFLOW_EXECUTOR_EXITED_WITHOUT_RESULT` (`effect_unknown`).
- **Uninstall (host-copy model):** receipt-backed uninstall now accepts the
  receipt's verified `plugin_realpath` under Grok's `installed-plugins/` copy
  root (byte-identity still required), and rollback reinstalls the host copy
  from the immutable stage when the host already deleted the copy.
- **Team scale retry:** an aborted scale attempt (intent receipt persisted,
  signalling failed) no longer wedges the identity chain — an orphan receipt
  that matches the retried intent on every deterministic field is adopted
  idempotently; any other content stays a hard conflict.

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
  (curated allowlist, structural refusal under `OMG_MCP_SERVER=1`, path-confinement). Exercised on a real Grok host.
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
full-branch GO). 468 → 528 unit tests; exercised against a real Grok host.

### Added
- **Global guidance injection (`~/.grok/rules/omg.md`):** the Grok-native OMC
  `CLAUDE.md` / OMX `AGENTS.md` equivalent. `omg setup` writes an always-loaded
  operating contract (tuned to Grok 4.5) via a non-destructive marker reconcile
  (`OMG:START/END`), preserving any `USER:OMG:POLICY` block, with a source-hash
  handshake and rolling backup (`omg_cli/guidance.py`, `templates/omg-rules.md`).
  `omg setup --no-global-rules` opts out. Observed in a one-time Grok host smoke
  on 2026-07-21: `grok inspect` loads it and a fresh `grok -p` quotes the contract.
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
