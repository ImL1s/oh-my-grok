# OMG Upgrade Synthesis — Install Polish, Guidance Injection, Gap Matrix, Fix List, Test Plan

Scope: oh-my-grok (OMG) v0.3.2 on Grok Build CLI 0.2.106. Ports the *purpose* of OMC/OMX mechanisms onto Grok's real, documented host surface — never claiming a mechanism Grok doesn't support (no Stop hard-pin, no tmux/team control plane, no full LSP/AST MCP bridge — all per the parity-council NEVER list, which remains binding).

---

## 1. INSTALL UPGRADE PLAN

Goal: match OMC's "plugin install + one setup command" and OMX's "install≠setup, explicit opt-in, hash-verified idempotent regen" polish, using only what `grok plugin`/`grok --help` actually expose (confirmed via recon: `grok plugin {list,install,uninstall,update,enable,disable,details,validate,tag,marketplace}`, `[plugins].enabled` required-allowlist, no plugin auto-trust).

### 1.1 Fix the two-surface installer so `install-plugin.sh` is idempotent and self-healing
**What**: Rewrite `scripts/install-plugin.sh` so a re-run always converges to the current checkout instead of the current "install once, forget forever" behavior.
**Why**: Confirmed live on this machine — two duplicate `oh-my-grok`-named plugin registrations exist (`~/.grok/installed-plugins/-ed6f3e28` and `oh-my-grok-7faf0130`), both pinned at the v0.3.1 commit while the working tree is at v0.3.2, and `omg doctor` reports `[OK]` for both mismatched version lines because `_summarize_plugin_payload()` never diffs them. This is FIX LIST items #6 and #7 below — this section is the install-flow-level remediation, the FIX LIST entries are the code-level remediation. Both must land together.
**Exact files**:
- `scripts/install-plugin.sh`: after computing `ROOT`, run `grok plugin list --json` (or `grok inspect --json` if that surfaces plugin sources), find any entry named `oh-my-grok` whose `source` (post-resolving `.` vs absolute path via `realpath`) differs from `$ROOT`; if found, `grok plugin uninstall oh-my-grok` it before installing fresh, or hard-fail with a printed repo_key so the user can `grok plugin uninstall` manually. Then call `grok plugin install "$ROOT" --trust`; on any non-zero/"already installed" outcome, immediately follow with `grok plugin update oh-my-grok` (confirmed real subcommand via `grok plugin update --help`) so the on-disk snapshot is force-refreshed rather than silently left stale.
- `omg_cli/doctor.py` `_summarize_plugin_payload()` (~line 320-383): compare the `version` field of the matched plugin entry against the local `plugin.json` version; emit `[FAIL]` (strict) / `[WARN]` (non-strict) on divergence instead of blanket `[OK]`. Also change candidate-selection from "first name match" to "match by resolved source path against cwd/repo root first, fall back to first name match with a printed disambiguation warning."

### 1.2 Make `[plugins].enabled` part of the documented install flow, not a silent gap
**What**: Since Grok plugins are disabled-by-default unless listed in `[plugins].enabled` (config.toml) or enabled via CLI, `install-plugin.sh` (or a new `omg setup --plugin`) must explicitly call `grok plugin enable oh-my-grok` after install/trust, and `omg doctor` must hard-check `[plugins].enabled` contains `oh-my-grok` (not just that trust/inventory shows it "installed").
**Why**: A user who runs `grok plugin install . --trust` successfully can still have a plugin that never loads in-session because it's absent from `[plugins].enabled` — this is the single most common "why isn't my skill showing up" failure mode for any Grok plugin author, and OMC/OMX don't have this problem (Claude Code plugins auto-enable on install; Codex has no such config gate).
**Exact files**: `scripts/install-plugin.sh` (add `grok plugin enable oh-my-grok || true` post-install, non-fatal since some grok versions may auto-enable), `omg_cli/doctor.py` (new hard check reading `~/.grok/config.toml [plugins].enabled` array, matching plain-name or `<scope>/<hash>/<name>` id forms).

### 1.3 One-liner curl bootstrap, matching OMX's `curl | bash` ergonomics
**What**: Add `scripts/bootstrap.sh` (curl-able from raw GitHub) that does: clone to `~/.local/share/oh-my-grok` (pin to latest release tag by default, not `main`), run `install-plugin.sh`, symlink `bin/omg` to `~/.local/bin/omg` (create dir + PATH hint if missing), run `omg doctor`. Publish as `curl -fsSL https://raw.githubusercontent.com/ImL1s/oh-my-grok/main/scripts/bootstrap.sh | bash`.
**Why**: Recon `omg-current` (e) confirms the current README requires ~5 manual shell commands before first `omg doctor`, explicitly weaker than OMC's `/plugin install` or OMX's `npm i -g`. This doesn't remove the two-surface requirement (Grok genuinely needs plugin+CLI, unlike OMC/OMX single-surface installs) but collapses it to one user-facing command.
**Exact files**: new `scripts/bootstrap.sh`; `README.md` "Full install" section gets the one-liner promoted above the manual steps (keep manual steps as "what the script does" for transparency/audit, matching OMX's own pattern of documenting exactly what setup touches).

### 1.4 Version pinning as the default, not an afterthought
**What**: `bootstrap.sh` and `install-plugin.sh` `git checkout` the latest **release tag** (`git describe --tags --abbrev=0` on the default branch, or a `--channel stable|dev` flag mirroring OMX's `AutoUpdateMode`) instead of tracking `main` HEAD implicitly.
**Why**: Recon flags "optional pin" as riskier than OMC's pinned-plugin-marketplace default; OMX's `stable`/`dev` channel split is a proven pattern worth porting in purpose.
**Exact files**: `scripts/install-plugin.sh`, `scripts/bootstrap.sh`, `README.md` Upgrade table (line ~105).

### 1.5 `omg doctor` as a real drift auditor (OMX capabilities-lock pattern, right-sized)
**What**: Add a lightweight `omg_capabilities.lock.json` at repo root: per-file SHA-256 of every `skills/omg-*/SKILL.md` and `agents/omg-*.md`, rolled up into one aggregate digest. `omg doctor` compares the digest of what's actually loaded under `~/.grok/installed-plugins/<matched-entry>/` against the lock file computed from the working tree, and reports drift explicitly (this directly catches the exact 1.1 staleness bug as a generic, future-proof check rather than a one-off version-string compare).
**Why**: OMX's `omx-capabilities.lock.json` is explicitly called out in recon as "the single most portable idea" for drift detection; OMG's current doctor only checks trust/enabled flags, not content parity.
**Exact files**: new `scripts/generate_capabilities_lock.py` (writes `omg_capabilities.lock.json`), `omg_cli/doctor.py` (new check consuming it), CI step in `.github/workflows/ci.yml` to regenerate-and-diff (mirrors OMX's `verify:native-agents` in `prepack`/`test`).

### 1.6 `omg update` command
**What**: `omg` currently has `setup, doctor, state, cancel, resume, wiki, hud, lsp, interview, goal, accept, integrate, worker, review, qa, autopilot, ulw, ralph, ralplan, ask, pipeline, dual-review` — no `update`. Add `omg update` that runs `git -C <install-root> fetch --tags && git checkout <latest-tag>`, then calls the 1.1 refresh logic (`grok plugin update`), then `omg doctor`.
**Why**: OMC has `/oh-my-claudecode:omc-setup` re-run + auto 24h check; OMX has `omx update` + passive launch-time checker. OMG has neither — the user must know to `cd` into the clone and `git pull` manually, which is also the exact bug in 1.1.
**Exact files**: new `omg_cli/update_cmd.py`, wire into `omg_cli/main.py` subparsers, `README.md` Upgrade row simplifies to `omg update`.

### 1.7 Uninstall parity
**What**: `omg uninstall` (currently absent) that: `grok plugin uninstall oh-my-grok`, removes `~/.grok/hooks/omg-pretool-deny.json`, removes the `bin/omg` symlink, optionally prompts before deleting `.omg/` state directories in known project roots (never silently deletes user data).
**Why**: README's Uninstall row today is a single ambiguous `grok plugin uninstall oh-my-grok` (ambiguous per FIX LIST #7 when duplicates exist) with no CLI-side or hook-file cleanup — OMC ships `scripts/uninstall.sh`, OMX ships `omx uninstall`; OMG has neither for the CLI half.
**Exact files**: new `omg_cli/uninstall_cmd.py`, `omg_cli/main.py`, `README.md`.

---

## 2. GLOBAL GUIDANCE INJECTION DESIGN

### 2.1 Target file(s)
Grok Build's rules-loading order (recon `grok-host` (h)) is the direct analog of `~/.claude/CLAUDE.md` / `~/.codex/AGENTS.md`:

- **Home-scope, always loaded**: `$GROK_HOME/rules/*.md` — i.e. **`~/.grok/rules/omg.md`**. This is the correct target: it is scanned every session regardless of cwd, is Grok-native (not a Claude/Cursor compat shim, so it's on by default with no `[compat.*]` toggle dependency), and multiple files here all load (not first-match-wins), so `omg.md` coexists cleanly with any user files.
- **Project-scope, optional**: `<repo>/AGENTS.md` (or `Agents.md`/`AGENT.md` — Grok loads all recognized names in a dir) for a project that wants OMG conventions committed to the repo. Lower priority than a hand-written home rule (deeper files win) — this is a feature: a project's own `AGENTS.md` naturally overrides the global OMG defaults for that project.
- **Not `CLAUDE.md`**: writing to `~/.claude/CLAUDE.md` would piggyback on Claude-compat loading (`[compat.claude].rules`, default on but user-togglable and off entirely if the user disables Claude compat) and would conflate OMG's identity with OMC's file — wrong home even though it happens to load today. Confirmed anti-pattern per the `omg-current` recon's warning about "magic keywords in `~/.claude/CLAUDE.md`" already causing WARN-level cross-tool bleed on this machine.

**Recommendation**: `omg setup` writes/reconciles **`~/.grok/rules/omg.md`** (global) and optionally `<project>/AGENTS.md` fragment (project, via `--local`, mirroring OMC's `--local`/`--global` flags) — never touches `~/.claude/*` or `~/.cursor/*`.

### 2.2 Marker contract
Direct port of the OMC/OMX bounded-region pattern, sized to Grok's rules semantics:

```
<!-- OMG:START -->
<!-- OMG:VERSION:0.3.2 -->
...generated content (below)...
<!-- OMG:END -->
<!-- USER:OMG:POLICY:START -->
...(preserved verbatim across regen, like OMX's USER:OMX:POLICY block)...
<!-- USER:OMG:POLICY:END -->
```

- **Idempotent reconciliation** (not naive overwrite): `omg setup` parses existing `~/.grok/rules/omg.md` line-by-line for `OMG:START`/`OMG:END`. If found, replace only that bounded region (OMX's `upsertManagedAgentsBlock`). If the file exists but has no markers (foreign/hand-written), append the managed block at the end, never overwrite (OMX's "wholly user-owned file" fallback). If a `USER:OMG:POLICY:START/END` block exists anywhere in the file, extract and re-append it after regeneration even if the fresh template doesn't already include it (OMX's `preserveUserOmxPolicyBlocks`).
- **Corruption detection**: reject and refuse to proceed (print actionable error, do not silently duplicate) on `nested-or-duplicate-start`, `unmatched-end`, `unmatched-start` — same three states OMC's coordinator detects.
- **Hash handshake**: `omg setup` computes SHA-256 of the canonical template (`templates/omg-rules.md` in the repo) and compares against a `<!-- OMG:SOURCE-HASH:<sha> -->` line stored just after `OMG:VERSION`; if the on-disk block's hash doesn't match what `omg setup`'s own template-render would currently produce for that version, it's flagged as user-hand-edited-inside-the-markers drift (rare but matches OMC's "fails closed on hash mismatch" philosophy) and `omg doctor` reports it rather than silently clobbering.
- **Backup before every write**: write a timestamped copy to `.omg/backups/setup/<timestamp>/omg.md` before mutating (OMX's `ensureSnapshotBackup` pattern — exclusive-create + post-write lstat symlink rejection is overkill for a single rules file but the backup-before-overwrite habit is cheap and directly matches this repo's own `.omg/backups/setup/` convention already implied by the `.omg/` layout in `omg-current` recon).
**Exact files**: `templates/omg-rules.md` (new canonical source, analogous to OMC's `docs/CLAUDE.md`), `omg_cli/setup_cmd.py` (add the marker-parse/splice/backup logic — currently `setup_cmd.py` only scaffolds `.omg/` + writes `templates/AGENTS.fragment.md`, per recon `omg-current` (a); this needs the actual reconciler, which appears to not exist yet), `omg_cli/doctor.py` (new check: `OMG:START/END` present, version matches, no corruption states).

### 2.3 Draft content (concise — this loads every session)

Design choices below are each tied to a specific recon fact:

- **No "ask before every risky thing" softness** — grok45-web (c) reconstructs xAI's own guidance as preferring **imperative, command-style rules** over soft phrasing ("forbid X" not "try to avoid X"), because soft phrasing lets the model invent "compliant" loopholes. The draft uses imperative sentences throughout.
- **Explicit change-boundary framing** — grok45-web (b) flags "scope creep / assumption-filling" as Grok's most-cited failure mode (third-party summary, treated as directional not proven) — the draft opens with an explicit scope-boundary rule before anything else.
- **No Stop-hook language** — grok-host (a)/(i) confirm only `PreToolUse` blocks; `Stop` is passive. The draft never tells the model "a hook will stop you" (that would be a lie the model could discover mid-session and lose trust in the whole file) — instead it tells the model to *self-check* completion criteria, matching the actual mechanism (`omg ralph`/`omg pipeline` outer-loop, not an in-session veto).
- **Plan Mode acknowledgment** — grok45-web (b) confirms Grok Build defaults to Plan Mode (blocks writes until plan approved). The draft tells the model this is expected/fine, not something to fight, and to route non-trivial work through `omg ralplan`/`omg interview` which produce the plan Grok's own Plan Mode wants anyway — this reframes a host default as reinforcement rather than friction.
- **`capability_mode` as the real isolation primitive** — grok-host (d)/(f) confirm `capability_mode` (read-only/read-write/execute/all) is the actual host lever, not a fictional `disallowedTools` agent-file field (documented as a **gap** — no schema exists). The draft tells the model to pass `capability_mode` explicitly on every `spawn_subagent` call rather than assuming a default, since the recon confirms the default is agent-type-dependent and unverified.
- **Depth-limit awareness** — grok-host (f): hard cap of 1 (a subagent cannot spawn subagents). The draft explicitly warns against attempting nested delegation, which would otherwise waste a turn on a guaranteed host-level error.
- **`--rules`/`--system-prompt-override` awareness for headless flows** — grok-host (e): `--rules` appends, `--system-prompt-override` replaces entirely (skips this file). The draft notes `omg`'s outer-loop CLI calls use `--rules` (append) specifically so this file's contract survives headless `omg ralph`/`omg pipeline` sessions — worth stating so the model understands why headless runs still see these rules.
- **Kept short** — OMC's injected block is 72 lines; OMX's is 193 lines but that's a large-team catalog. OMG's skill roster is 15 (vs 41), so the draft below stays under ~55 lines, deferring the full skill/agent list to `docs/skills.md` (lazy-loaded, same pattern as OMC's `omc-reference` skill and OMX's `docs/guidance-schema.md`).

```markdown
<!-- OMG:START -->
<!-- OMG:VERSION:0.3.2 -->
<!-- OMG:SOURCE-HASH:<sha256 of templates/omg-rules.md> -->
# oh-my-grok (OMG) — operating contract

OMG orchestrates Grok Build with evidence-gated CLI workflows. This file is the
always-loaded contract; the full skill/agent/CLI catalog is in docs/skills.md
(read on demand, not preloaded).

<scope_boundary>
Change only what the task requires. Do not refactor, rename, or "clean up"
files outside the requested change. Do not add dependencies not explicitly
requested. If the task is ambiguous, ask one direct question — do not guess
and proceed on an invented interpretation.
</scope_boundary>

<workflow_routing>
Non-trivial work (multi-file, new feature, unclear requirements): run
`omg interview` then `omg ralplan` before writing code — this produces the
plan Grok's own Plan Mode will ask for anyway, so route through it instead of
freehand-approving Plan Mode step by step.
Trivial work (single command, one-line fix, clarification): do it directly.
Long/autonomous runs: `omg autopilot`, `omg ralph`, or `omg pipeline` —
these are outer CLI loops that re-invoke `grok --session-id/--resume`
between turns. They do not rely on an in-session Stop hook (Grok's Stop
event cannot block); do not assume a hook will catch you if you stop early —
check `omg autopilot status` / the run's evidence file yourself before
declaring done.
</workflow_routing>

<subagents>
Pass `capability_mode` explicitly on every `spawn_subagent` call
(read-only for exploration, read-write for edits, execute for shell,
all only when truly needed) — do not rely on an assumed default.
Subagents cannot spawn subagents (host hard depth limit = 1); do not
attempt nested delegation, plan single-level fan-out instead.
</subagents>

<verification>
Before claiming a task done: re-run the relevant tests/build, and for
anything gated by OMG (review/QA/acceptance), check the actual evidence
file under .omg/state/ — a clean run must have a fresh, run_id-bound
verdict, not a stale or unrelated stamp. Never report "done" from
memory of what you intended to do.
</verification>

<state>
Durable OMG state lives under .omg/ (state/, plans/, wiki/, backups/) —
check .omg/state/RESUME.md at session start if present. This file
persists across compaction; OMG hooks do not inject chat-level
reinforcement (Grok's non-PreToolUse hooks are passive), so re-read
.omg/state/ yourself after a resume/compaction instead of assuming
context carried over.
</state>

## Cancel
`omg cancel` clears any active OMG mode state (autopilot/ralph/ralplan/etc).

## Setup
Say "omg setup" or run `omg setup` after any `omg update`.
<!-- OMG:END -->
```

Length check: ~48 lines of contract body — comparable to OMC's 72-line block, well under OMX's 193-line template (justified there by a much larger role/team catalog OMG doesn't have).

---

## 3. GAP MATRIX

| Mechanism (OMC/OMX) | Purpose | OMG today | Proposed Grok-native action | Priority |
|---|---|---|---|---|
| **Guidance injection** (`~/.claude/CLAUDE.md` / `~/.codex/AGENTS.md`) | Always-loaded operating contract, hash-verified idempotent regen | `templates/AGENTS.fragment.md` exists but no reconciler confirmed wired into `setup_cmd.py`; no marker-splice/backup logic found | Ship §2 design: `~/.grok/rules/omg.md` + marker splice + hash handshake | **P0** |
| **Keyword auto-triggers** (`keyword-detector.mjs`/`.ts`, `UserPromptSubmit`) | Natural-language phrase → skill routing without explicit `/name` | None — OMG skills are user-invocable only, no `UserPromptSubmit` hook exists in `hooks/hooks.json` (only `SessionStart`, `SubagentStop`, `Stop`, `PreToolUse`) | Add `UserPromptSubmit` hook script (`hooks/bin/user_prompt_submit.py`) that regex-matches a small keyword table (ralph/ralplan/autopilot/cancel/deep-interview) and injects `hookSpecificOutput.additionalContext` naming the matching `omg` skill — Grok's `UserPromptSubmit` docs (grok-host a) confirm this event exists and supports `additionalContext`-style injection like OMC's. Keep the table small (OMG has 15 skills, not 41) | **P1** |
| **Session-start context** (project memory/wiki load at `SessionStart`) | Re-establish state after a fresh session | `hooks/bin/session_start.py` writes `.omg/state/RESUME.md` (confirmed) | Already ships the *purpose*; extend to also surface last goal-ledger story status and last dual-review verdict summary inline, not just a resume pointer | **P2** |
| **Statusline/HUD** (native `statusLine` polling `.omc/state/`) | Live progress visibility in the TUI chrome | `omg hud` CLI + skill exist (v0.3.0) — but Grok has **no documented `statusLine` API** equivalent to Claude Code's, so this is a CLI-side pack, not in-TUI | Confirm via `grok inspect`/docs whether any TUI status-surface exists; if not, keep as CLI-native substitute (already right-sized) — do not claim in-TUI HUD | **P2 (already substituted correctly)** |
| **Notifications** (Telegram/Discord/Slack webhooks on Stop/SessionEnd) | Push completion pings to external channels | Genuinely absent — confirmed nowhere in CLI/skills/CHANGELOG | Add opt-in webhook POST from `omg`'s outer-loop completion points (`autopilot complete`, `ralph` terminal states) — this is CLI-native (no hook needed) since OMG already controls the outer loop process lifecycle | **P2** |
| **Magic keyword injection text** (`[MAGIC KEYWORD: NAME]` reinforcement) | Make the model explicitly aware a workflow was triggered | N/A (no keyword detector yet — see above) | Ships as part of the `UserPromptSubmit` hook above; format as `[OMG SKILL: <name>]` in `additionalContext` | **P1 (bundled with above)** |
| **Cancel** (`/oh-my-claudecode:cancel`, clears all mode state files) | Standard way to exit any active mode | `omg cancel` exists (confirmed in CLI list) | Verify it covers all mode state files written by `autopilot.py`/`ralplan.py`/`workers.py` (goal ledger, ownership manifests) — audit for completeness, not a new mechanism | **P2 (audit only)** |
| **Memory/notepad** (`.omc/notepad.md`, `project-memory.json`, 7-day/permanent tags) | Compaction-resistant scratch + permanent project knowledge | `.omg/state/` exists for mode state; no equivalent of `notepad_write_priority`/`project_memory_add_directive` tools confirmed | Add `omg note` CLI subcommand (skill `note` already listed in the skills tool list seen this session — verify it's an OMG skill and wire a backing `.omg/notepad.md` with the same TTL convention: `<remember>`=7d, `<remember priority>`=permanent, so guidance §2.3 can reference it consistently | **P1** |
| **Teams/tmux multi-CLI** | N coordinated agent workers | `omg worker {own,prepare,seal,join}` — a real ownership-manifest-based worker model (confirmed in FIX LIST context, `workers.py`) | **OUT_OF_SCOPE per council** — `omg --madmax` explicitly not a team FSM. Keep as-is; do not build tmux control plane | **WONTFIX (council)** |
| **MCP tools equivalents** (`lsp_*`, `ast_grep_*`, `notepad_*`, `wiki_*`, `state_*` — ~54 tools via `bridge/mcp-server.cjs`) | Rich in-session structured tool access | `omg lsp` = local pyright probe only (confirmed NEVER-scope for full bridge); `omg wiki` exists as CLI-native substitute | Keep `omg lsp` scoped as a probe, not a bridge (per council). Consider whether Grok's own `[mcp_servers.<name>]` config surface (confirmed in grok-host g) could host a *thin* MCP server exposing `.omg/state/*` reads (state_get_status equivalent) without claiming full LSP/AST parity — genuinely new option not yet triaged by council, flag for discussion rather than commit | **P2 (needs council input)** |
| **Update flow** (`omc update`/auto 24h check; `omx update` + passive checker) | Keep install current without manual git ops | None — confirmed gap, §1.6 | Ship `omg update` (§1.6) | **P0** |
| **Kill switches** (`DISABLE_OMC`, `OMC_SKIP_HOOKS`) | Per-hook and global opt-out | Not confirmed present in `hooks/bin/*.py` — recon doesn't show env-var gating in the 4 OMG hook scripts | Add `DISABLE_OMG=1` global check + `OMG_SKIP_HOOKS="name1,name2"` per-script check at the top of `session_start.py`, `subagent_stop.py`, `stop.py`, `pre_tool_use_deny.py` (defense-in-depth, matches OMC's per-script pattern) | **P1** |
| **Stop-hook blocking / "boulder never stops"** | Prevent premature termination mid-workflow | Explicitly NEVER (host-incapable); substituted by outer CLI loop (`omg ralph`/`pipeline` + `--session-id`/`--resume`) | Already correctly substituted (v0.3.0 `omg resume`) — no further action, just keep messaging honest (§2.3 draft already does this) | **Done / WONTFIX (host)** |
| **Agent-file frontmatter schema / `disallowedTools`** | Per-agent tool restriction | Grok has no documented schema for this (confirmed gap in grok-host recon); OMG's 8 `agents/omg-*.md` presumably rely on `capability_mode` at spawn time | Standardize: every `omg_cli` code path that calls `spawn_subagent` (or documents doing so) must pass explicit `capability_mode`; add a doctor check that greps agent docs for capability_mode guidance presence | **P1** |
| **Version drift detection** (OMX capabilities lock) | Prove installed == shipped | Confirmed absent — root cause of FIX LIST #6/#7 | §1.5 `omg_capabilities.lock.json` | **P0** |
| **Docs/CLI drift CI check** | Catch stale command docs before merge | Confirmed absent — root cause of FIX LIST #5 (`omg goal start` vs `start-story`) | New test: diff every `` `omg <cmd> <sub>` `` string in `docs/skills.md`/`docs/skills.zh-Hant.md` against `omg_cli/main.py` argparse choices | **P0 (bundled with FIX LIST #5)** |

---

## 4. FIX LIST (ordered by severity, confirmed defects only)

### Critical

**4.1 — `deny.py` external-CLI block bypassed by multi-line commands** (`omg_cli/deny.py:14`)
`_CMD_POS` regex has no `re.MULTILINE` and no `\n` in its start-of-command character class, so `^` only anchors the whole string. A Bash/`run_terminal_command` payload with the denied binary (`claude`/`codex`/`omx`/`agy`/`cursor-agent`/`kimi`) on its own line — no leading `;`/`&`/`|`/backtick/`&&`/`||` — sails through `should_deny_command()` undetected (verified: newline-joined command returns `False`, semicolon-joined equivalent correctly returns `True`).
**Fix**: compile `_CMD_POS` and every regex built from it with `re.MULTILINE`, and add `\n` to the command-position character class (`(?:^|[;&|(`\n]|\|\||&&)`), or normalize the command string by splitting on `\n` and checking each line independently before falling back to the joined-string check.

**4.2 — `verdict.py` run_id binding bypassable by an unrelated JSON blob without a `run_id` key** (`omg_cli/verdict.py:117`)
`_json_verdict()` only enforces `expected_run_id` when the candidate object itself contains a `run_id` key. `_extract_json_objects()` harvests every fenced ```json block plus the first bare balanced-brace object anywhere in the artifact text; `parse_verdict()` returns on the first candidate yielding a non-None verdict. A stray/example JSON snippet with `{"verdict":"APPROVE"}` and no `run_id` field is accepted even when the real, correctly-bound verdict for that `run_id` is `FAILED`/stale (verified live). This defeats `dual_review.py:401` and `ralplan.py:300-302`'s anti-replay gate.
**Fix**: make `expected_run_id is not None` unconditionally require a matching `run_id` key on *every* candidate object considered — reject (skip, don't accept) any candidate that both lacks `run_id` and `expected_run_id` was supplied, regardless of `strict`/`schema_version`. Optionally also require the *last* (not first) qualifying candidate to win, and strip fenced ```json blocks that are clearly example/prose context (e.g. preceded by "for example" / "the format looks like") before scanning — but the run_id requirement alone closes the hole.

**4.3 — Autopilot FSM: `blocked→implement→blocked` round-trip skips invalidation of stale review/QA stamps** (`omg_cli/autopilot.py:26`)
`invalidate_quality_stages()` is only called on entry to `ralplan` (from review/qa), entry to `rework`, and entry to `review` (from rework/implement) — never on entry to `implement` from any source, including `blocked`. `stage_review_is_clean()`/`stage_qa_is_clean()` only check on-disk `clean`/`invalidated` flags, never re-derive against current git diff/product hash. Since `qa→blocked→implement→blocked→qa→acceptance` is a legal transition path with no invalidation trigger anywhere in it, new unreviewed code written during the second `implement` visit reaches `verified=True` via `omg autopilot complete` without ever passing through a fresh code-review or QA cycle.
**Fix**: call `invalidate_quality_stages()` on every transition **into** `implement`, regardless of source phase. Longer-term (recommended, not just the minimal patch): bind `stage_review_is_clean`/`stage_qa_is_clean` to a live diff/product hash captured at gate-check time (compare against `review.py`'s `diff_hash` / `qa.py`'s `product_hash(root)` at the moment of the gate check, not just at stamp-write time) so staleness is structurally impossible rather than enumerated per-transition.

**4.4 — `docs/skills.md` documents non-existent `omg goal start`/`omg goal complete` (real: `start-story`/`complete-story`)** (`docs/skills.md:212`, duplicated `docs/skills.zh-Hant.md:210`)
Verified live: `./bin/omg goal start --help` errors with the real choice list (`init, status, link-run, start-story, checkpoint, block-story, resume-story, complete-story, verify, repair`); the doc also omits `block-story`/`resume-story` entirely. No CI check cross-validates doc command strings against argparse.
**Fix**: correct both doc files' command lists; add the CI drift check described in GAP MATRIX row "Docs/CLI drift CI check."

**4.5 — `install-plugin.sh`'s "upgrade" flow (`git pull && ./scripts/install-plugin.sh`) does not refresh the installed plugin snapshot; `omg doctor` never detects the resulting staleness** (`scripts/install-plugin.sh:21`, `omg_cli/doctor.py`)
`grok plugin install` copies a real git-checkout snapshot into `~/.grok/installed-plugins/<repo_key>/`, not a symlink. `install-plugin.sh` treats "already installed" as a soft warning and never calls `grok plugin update`. Live-verified on this machine: installed snapshot is pinned at commit `7958cab` (v0.3.1) while the working tree is at `46b3489`/v0.3.2, with real content divergence (`skills/omg-autopilot/SKILL.md` QA-freeze fix present in repo, absent from what Grok actually loads). `omg doctor`'s `_summarize_plugin_payload()` prints `[OK]` for both the `plugin.json` version line and the mismatched installed-inventory version line — never compares them, so the entire run reports "all hard checks passed" despite the drift.
**Fix**: §1.1 above (install-plugin.sh calls `grok plugin update` on any non-clean-install outcome; doctor hard-compares versions).

**4.6 — `grok plugin install` creates duplicate same-named plugin entries when the source path string differs (e.g. `.` vs absolute path); relocate/upgrade flow never reconciles them** (`scripts/install-plugin.sh:18`)
Live-verified: two `oh-my-grok`-named entries exist for the identical directory (`.../oh-my-grok/.` vs `.../oh-my-grok`), both stale. README's relocate/uninstall instructions don't account for this, and `doctor.py`'s plugin-inventory probe takes the first name match with no path-based disambiguation, so it can report on an arbitrary (possibly orphaned) install.
**Fix**: §1.1 (dedupe-before-install) + doctor path-matching change described there.

### Major

**4.7 — `command_policy.py` `--no-allowlist` break-glass path permits code execution via bundled short flags like `-ic`, contradicting the documented "hard floor, never liftable" guarantee** (`omg_cli/command_policy.py:666`)
Under `no_allowlist=True`, `check_command_policy()` only runs `_has_flag(cmd, "-c", "-e")` and returns immediately — `_check_python_argv()` (which has real per-token flag validation) is never invoked. `_has_flag`'s glued-short-option match requires the token to literally start with `-c`/`-e`; `-ic` (real CPython: `-i` + `-c` combined) starts with `-i` and is missed. Verified: non-break-glass `python3 -ic 'print(1)'` is correctly denied; break-glass `python3 -ic 'import os; os.system("id")'` is allowed.
**Fix**: In the `no_allowlist` branch, still call a floor-only variant of `_check_python_argv` (or extend `_has_flag`'s glued-option detection to recognize any short-flag cluster containing `c`/`e` after a leading `-i`/other combinable single-char CPython flags) so the "-c/-e is never liftable" guarantee actually holds under break-glass.

### Minor

**4.8 — Ownership join silently skips file-ownership enforcement when a manifest task has empty `owned_files`** (`omg_cli/workers.py:531`)
`foreign = sorted(changed_norm - owned) if owned else []` makes `foreign` unconditionally empty when `owned` is falsy, so `ownership_violation` can never trigger for that task. `build_ownership_manifest()` rejects empty `owned_files` at construction time, but `load_ownership_manifest()` never re-validates on load, so a hand-edited or malformed on-disk manifest silently disables the cross-task ownership guard for that task.
**Fix**: treat empty `owned_files` as fail-closed in `join_worker_results` (flag `ownership_violation` for any non-empty `changed_files` when `owned` is empty), or reject such manifests at `load_ownership_manifest()` time, mirroring `build_ownership_manifest`'s own non-empty requirement.

**4.9 — README's plugin-only install path doesn't remind the reader at point-of-use that `omg` commands require the Full install** (`README.md:92`)
The "Plugin-only (half surface)" caveat is stated once, but "Smoke after install" and "Recommended default flow" (both using `omg ...` commands) don't re-flag the dependency, so a plugin-only reader hits `command not found: omg` with no nearby pointer back.
**Fix**: add a one-line callout directly above "Smoke after install"/"Recommended default flow": "Requires Full install (`omg` on PATH) — plugin-only skips this."

---

## 5. TEST PLAN

### 5.1 Unit / hermetic (no host dependency)
```bash
.venv/bin/python -m pytest -q -m 'not live'
```
Baseline: 468 passed. After applying fixes 4.1–4.3, 4.7, 4.8, add targeted regression tests before/alongside the fix (do not just patch and rerun the existing suite — the existing suite passed *with* these bugs present):
- `tests/test_deny.py::test_multiline_bypass` — `should_deny_command('echo start\nclaude -p "hi"')` must return `True` post-fix.
- `tests/test_verdict.py::test_run_id_binding_rejects_unbound_candidate` — reproduce the exact repro from 4.2 (stale correctly-bound `FAILED` + unrelated unbound `APPROVE` blob) and assert `parse_verdict(..., expected_run_id=...)` no longer returns `APPROVE`.
- `tests/test_autopilot.py::test_blocked_implement_blocked_invalidates_stamps` — walk `qa→blocked→implement→blocked→qa`, assert `stage_review_is_clean`/`stage_qa_is_clean` are `False` after the round-trip.
- `tests/test_command_policy.py::test_no_allowlist_blocks_combined_short_flags` — `check_command_policy(['python3','-ic','...'], no_allowlist=True)` must raise post-fix.
- `tests/test_workers.py::test_empty_owned_files_fails_closed` — manifest with `owned_files: []` + non-empty `changed_files` must produce `ownership_violation`.
- New `scripts/check_docs_commands.py` (or extend `scripts/check_docs_links.py`) run as a pytest case: parse every `` `omg <verb> <subverb>` `` occurrence in `docs/skills.md` / `docs/skills.zh-Hant.md`, cross-check against `omg_cli/main.py`'s registered argparse choices; fails on any mismatch (closes 4.4 permanently, not just this one occurrence).

Run full suite again after fixes, confirm 468+N passed, 0 failed, no new skips/xfails introduced (per user's failure-mode-guard: no `test.skip`/`.only` as a substitute for a real fix).

### 5.2 Plugin manifest validation
```bash
grok plugin validate .
```
Expect: "Plugin manifest is valid." with correct skill/agent/hook counts (currently 1 skill dir — note: recon says 15 `omg-*` skill directories exist under `skills/`; `grok plugin validate .` reporting "1 skill dir" needs a follow-up check — likely `grok plugin validate` counts top-level `skills/` as one dir, not per-skill; confirm this isn't itself a packaging bug worth a FIX LIST follow-up before shipping §1's install changes).

### 5.3 Hermetic e2e
```bash
OMG_E2E=1 bash scripts/smoke.sh
```
Expect terminal line `ALL_REAL_E2E_OK`. Re-run after every fix batch, not just once at the end — this is the "keep a running baseline" pattern from the task's own operating rules.

### 5.4 Install-flow verification (new, covers §1 + FIX LIST 4.5/4.6)
Since the confirmed defects are about installer/doctor drift detection, testing them requires an actual install cycle, not just unit tests:
```bash
# Clean-slate simulation (do NOT run against the real ~/.grok/installed-plugins without backing up first)
grok plugin list --json | jq '.[] | select(.name=="oh-my-grok")'   # capture pre-state, confirm duplicate entries exist (reproduces 4.6)
./scripts/install-plugin.sh                                          # after fix: should dedupe + refresh, not create a 3rd entry
grok plugin list --json | jq '.[] | select(.name=="oh-my-grok")'   # confirm exactly ONE entry, version matches plugin.json
./bin/omg doctor --strict                                            # after fix: version-mismatch check must be able to FAIL (test by reverting local plugin.json version by one patch level and re-running — confirm doctor now flags it, then restore)
```

### 5.5 Guidance-injection verification (§2, new mechanism — needs a real test, not just code review)
```bash
./bin/omg setup                     # first run: creates ~/.grok/rules/omg.md
diff <(sha256sum ~/.grok/rules/omg.md) <(...)   # confirm OMG:START/END present, version line matches plugin.json
# Idempotency check:
echo '<!-- USER:OMG:POLICY:START -->\nmy custom rule\n<!-- USER:OMG:POLICY:END -->' >> ~/.grok/rules/omg.md
./bin/omg setup                     # second run
grep -A2 'USER:OMG:POLICY:START' ~/.grok/rules/omg.md   # "my custom rule" must survive
# Corruption-detection check:
sed -i '' 's/OMG:END/OMG:END\n<!-- OMG:START -->/' ~/.grok/rules/omg.md   # inject duplicate START
./bin/omg setup                     # must refuse/error, not silently proceed
```

### 5.6 Live Grok session checks (skills/hooks actually firing, rules injection visibility)
These require an interactive or headless real `grok` invocation — the class of check the parity-council explicitly wants ("live suite green ≠ product trustworthy" applies to unit tests; these are the complement):
```bash
# Rules injection visibility:
grok inspect   # confirm ~/.grok/rules/omg.md is listed as a loaded rules source, and (if AGENTS.md fragment also written) confirm project AGENTS.md is listed too

# Skill auto-appearance as slash commands:
grok -p "/help" --output-format json   # or interactively type "/" and confirm omg-* skills appear as /omg-ralph, /omg-ralplan etc. (per grok-host (c), user-invocable skills auto-become /name)

# Hook firing (PreToolUse deny — the one blocking event Grok supports):
grok -p "run: echo start; claude -p 'test'" --yolo --output-format json   # must be denied by pre_tool_use_deny.py; confirm JSON stopReason/text reflects the deny, not that claude actually ran
grok -p "run: echo start\nclaude -p 'test'" --yolo --output-format json   # THE regression test for FIX 4.1 — multi-line variant must ALSO be denied post-fix; pre-fix this is expected to slip through (use this exact repro to confirm the fix before merging)

# SessionStart hook / RESUME.md side effect:
grok -p "what does .omg/state/RESUME.md say" -r   # resume a session in a project with prior .omg/ state, confirm the model can see/read the resume nudge

# Plugin enable/trust end-to-end (closes 1.2):
grep 'oh-my-grok' ~/.grok/config.toml   # confirm [plugins].enabled contains oh-my-grok after install-plugin.sh runs (post-fix)
```

### 5.7 Regression guard for prior parity-council findings (don't re-break what's fixed)
Per `omc-parity-council/STATUS.md`, P0-4/P0-5 are Partial and P0-6 is Open — do not claim these are closed by this fix batch. Re-run only the specific live suite already covering L-DUAL-1 (`dual_review.py`) to confirm 4.2's fix doesn't regress it:
```bash
.venv/bin/python -m pytest -q -m live -k dual_review
```
Do not extend this claim to ralplan/pipeline L2 (still genuinely Partial/Open per STATUS.md) without new evidence — report status accurately per the council's Forbidden-claims list ("dual-review/ralplan is a trustworthy gate" remains forbidden until P0-4/P0-5/P0-6 are actually closed, not just until 4.2 is patched).

### 5.8 Exit criteria
- Pytest: 468+N passed, 0 failed (N = new regression tests from 5.1).
- `grok plugin validate .`: PASS.
- `omg doctor --strict`: passes on a clean install; correctly FAILs when version/enabled-flag drift is deliberately introduced (5.4/5.6).
- `OMG_E2E=1 bash scripts/smoke.sh`: `ALL_REAL_E2E_OK`.
- Live 4.1 multi-line-deny repro (5.6): denied post-fix, confirmed slips through pre-fix (documents the fix actually changed behavior, not just added an inert test).
- `grok inspect` (5.6): `~/.grok/rules/omg.md` visible as a loaded rules source post-§2 implementation.
- No new `test.skip`/`.only`/TODO placeholders introduced by this batch (grep changed test files before calling done).
