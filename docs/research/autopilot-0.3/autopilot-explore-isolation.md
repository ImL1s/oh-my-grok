# Worker isolation — how it ACTUALLY works today

**Repo:** `<repo-root>`  
**HEAD context:** explore pass for product decisions (isolation / fail-closed spawn)  
**Date:** 2026-07-20  
**Scope:** code + agent/skill contracts + live residual evidence (not aspirational plans)

---

## Current architecture (text diagram)

```text
                         ┌─────────────────────────────────────┐
                         │  Operator / outer omg CLI            │
                         │  omg ulw | ralph | ralplan | accept  │
                         └──────────────┬──────────────────────┘
                                        │
                    build_prompt / build_grok_argv (modes.py)
                    skill body + HARD_RULES_REMINDER
                    + "Capability spawn contract" (text only)
                                        │
                                        ▼
                         ┌─────────────────────────────────────┐
                         │  LEADER Grok process (full toolset)  │
                         │  • shell KEEP by default             │
                         │  • spawn_subagent allowed            │
                         │  • PreToolUse soft-gate (fail-open)  │
                         │  • may run omg accept / tests        │
                         └──────────────┬──────────────────────┘
                                        │
              model chooses spawn_subagent args  ◄── NO omg CLI interceptor
                                        │
          ┌─────────────────────────────┼─────────────────────────────┐
          ▼                             ▼                             ▼
  capability_mode             capability_mode                OMITTED / wrong
  = read-write                = read-only                    mode / agent
  (omg-executor preferred)    (critic/verifier/explore)      (e.g. bare
          │                             │                     general-purpose)
          ▼                             ▼                             ▼
  Host tool-kind filter        Host RO filter              Agent defaults
  NO Execute → no shell        no write + no Execute       general-purpose
  + agent disallowedTools      + agent disallowedTools     ≈ FULL tools
  (spawn + shell banned        (spawn+shell+write ban      (incl. shell)
   on omg-executor)             on critic/verifier)
          │                             │                             │
          └─────────────┬───────────────┘                             │
                        ▼                                             ▼
              depth=1 leaf (prompt + frontmatter)          ESCAPE PATH
              may still have Task/spawn residual             interpreter /
              unless disallowedTools honored                 external CLI

  ─ ─ ─ ─ ─ EXPERIMENTAL BRANCH (not default ULW) ─ ─ ─ ─ ─
  omg ulw --fanout process + OMG_EXPERIMENTAL_PROCESS_FANOUT=1
       → N× independent grok -p (OS PIDs)
       → capability_mode is PROMPT-LEVEL only
       → disallow_shell=False by design
```

**Default isolation path:** skill-driven `spawn_subagent` inside one leader (`--fanout skill`).  
**Process fanout:** multi-PID supervisor only; not the product isolation story.

---

## Evidence by investigation item

### 1. How ulw / ralph build prompts and spawn contracts

| Piece | Where | What it does |
|-------|--------|--------------|
| Skill body load | `omg_cli/modes.py` `MODE_SKILL_REL` + `load_skill_body` | Injects full `skills/omg-ultrawork/SKILL.md` or `omg-ralph/SKILL.md` into `-p` / `--prompt-file` |
| HARD RULES block | `HARD_RULES_REMINDER` L42–52 | Text: depth=1, no external agent CLIs, **MUST** spawn implementers RW / critic RO, accept via CLI |
| Capability spawn contract | `build_prompt` L248–254 | Extra section titled *"hard — host-enforced when set"* — still **prompt text** |
| Ralph extras | `ralph_context_pack` L99–196 + L223–244 | Story/iteration/acceptance path; repeats RW implementer MUST |
| Argv | `build_grok_argv` L292–382 | `grok --cwd … --output-format plain [-p\|--prompt-file]`; yolo→`bypassPermissions`; **safe**→`plan` |
| Shell clamp on leaders | L265–327, L367–371 | **`disallow_shell` default False** for ulw/ralph leaders; comment: *workers rely on capability_mode* |

**Skill-level spawn contract** (`skills/omg-ultrawork/SKILL.md` L60–64, `skills/omg-ralph/SKILL.md` L66–70):

- Implementers (`general-purpose`, `omg-executor`): **MUST** `capability_mode: read-write` (edit, **no Execute/shell**).
- Explore / plan / critic / verifier: **MUST** `capability_mode: read-only`.
- Child prompt should include explicit `capability_mode`; children must not spawn.
- Explicitly: do not rely on PreToolUse alone.

**There is no code path that builds a structured `spawn_subagent` JSON contract.** Isolation depends on the leader model following the injected skill + HARD RULES when it emits tool calls.

### 2. Agent frontmatter (`agents/`)

#### `agents/omg-executor.md` (L1–12)

```yaml
promptMode: extend
permissionMode: default
capabilityMode: read-write
disallowedTools:
  - spawn_subagent
  - run_terminal_command
  - run_terminal_cmd
```

- Leaf implementer; body restates parents MUST spawn with `capability_mode=read-write`.
- Host honors `disallowedTools` **when this agent type is selected** (hard-when-honored).

#### `agents/omg-orchestrator.md` (L1–7)

```yaml
promptMode: extend
permissionMode: default
agentsMd: true
# NO capabilityMode
# NO disallowedTools
```

- Leader-shaped agent: **may** `spawn_subagent` and shell (body L51 lists `run_terminal_command`).
- Body says if *spawned as child*, do not fan-out further — **convention only**.

#### `agents/omg-critic.md` / `omg-verifier.md` (frontmatter)

```yaml
permissionMode: plan
capabilityMode: read-only
disallowedTools:
  - spawn_subagent
  - search_replace
  - run_terminal_command
  - run_terminal_cmd
```

### 3. `modes.py` HARD_RULES / capability injection

| Constant / function | Lines | Role |
|---------------------|-------|------|
| `HARD_RULES_REMINDER` | 42–52 | Shared injection for ulw/ralph/ralplan/dual-review/fanout prompts |
| `build_prompt` | 199–262 | skill + HARD_RULES + mode header + **Capability spawn contract** + goal |
| `DISALLOW_SHELL_TOOLS` | 269 | `"run_terminal_command,run_terminal_cmd"` |
| `build_grok_argv(disallow_shell=…)` | 310–371 | Opt-in / env `OMG_DISALLOW_SHELL=1` / stage-specific only |
| `run_mode` | 607+ | Creates run, launches leader; **never sets verified** without acceptance |

**Critical honesty:** labels say *"host-enforced when set"* (`build_prompt` L248). That means: **if** the leader passes `capability_mode` on `spawn_subagent`, Grok host filters tool kinds. **omg CLI never validates or rewrites spawn args.**

### 4. Host-level vs prompt-only enforcement

| Layer | Enforced by | Hardness | What omg code does |
|-------|-------------|----------|-------------------|
| **`capability_mode` on spawn** | **Grok host** (tool-kind filter) | Hard-ish **when leader sets it** | Documents MUST; **no spawn interceptor** |
| **Agent `disallowedTools` / `capabilityMode` frontmatter** | Grok host when agent type loads | Hard when honored | Ships agent MD files only |
| **`--disallowed-tools` on process argv** | Grok headless flag | Hard when flag honored | Used for dual-review + ralplan RO stages (`disallow_shell=True`); **not** ulw/ralph leaders/workers by default |
| **`--permission-mode plan`** | Grok permissions | Gate, not full tool removal | dual-review always; ralplan RO stages; `safe=True` |
| **PreToolUse deny** | `hooks/bin/pre_tool_use_deny.py` → `omg_cli/deny.py` | **Soft fail-open** | Only `run_terminal_command` / Shell; command-position external agent CLIs; exception → **allow** |
| **Acceptance policy** | `omg_cli/command_policy.py` + `omg accept` | Hard for **verified** stamp | Not worker sandbox |
| **Prompt HARD RULES / skills** | Model compliance | Convention only | Always injected for mode launches |

**`deny.py` does not look at `spawn_subagent` at all** (L69–96: only shell tool names). Hooks never fail-closed spawn without capability_mode.

**Live residual (quota-heavy 2026-07-19):** L-CAP-SPAWN with explicit `capability_mode=read-write` + prefer `omg-executor` → child reported **no** `run_terminal_command` in toolset (`docs/research/live/cap-spawn-20260719T190456Z.txt`). That proves host filter **when args are correct**, not that leaders always pass them.

**Omitted mode:** `docs/security-model.md` L11 residual: *“Omitted mode falls back to agent defaults (`general-purpose` ≈ full).”*

### 5. Process fanout isolation (`omg_cli/fanout.py`)

| Behavior | Evidence |
|----------|----------|
| Not default | Module docstring L3–7; `main.py` L97–106 requires `OMG_EXPERIMENTAL_PROCESS_FANOUT=1` else exit 2 |
| Isolation = OS processes | N× `subprocess.Popen` + `start_new_session`, per-worker `workers/wNN.pid.json` |
| Prompts | `build_worker_prompt` L83–119: ulw skill + HARD_RULES + process-fanout contract |
| Shell | L305–317: **`disallow_shell=False`**; comment: *capability_mode is prompt-level* |
| Nested process fanout | Prompt forbids re-invoking `omg ulw --fanout process` (soft) |
| Verified | Never auto-verifies without acceptance (L420–431) |
| Cap | `HARD_CAP_WORKERS=8`, env `OMG_MAX_WORKERS` |

**Product implication:** process fanout is concurrency + cancel multi-PID, **not** stronger capability isolation than skill spawn. Each worker is a full Grok session with shell unless env/`safe` clamps it.

### 6. Leader spawns `general-purpose` without `capability_mode`

| Outcome | Confidence | Basis |
|---------|------------|--------|
| Host does **not** force RW from omg | **HIGH** | No Python/hook path inspects spawn args |
| Child may get **full** toolset (incl. Execute/shell) | **HIGH** | security-model residual; general-purpose not constrained like `omg-executor` frontmatter |
| Prompt/skills say MUST set RW | **HIGH** | HARD_RULES + skill text always in leader prompt for `omg ulw`/`ralph` |
| Model may ignore text | **HIGH** | security-model layer 8; no fail-closed spawn |
| PreToolUse may still soft-deny `claude`/`codex` in cmd position | **MED** | inherit hooks when global install; fail-open; **does not** block `python -c` / `npx` if shell present |
| Using `omg-executor` type without explicit mode | **MED** | frontmatter `capabilityMode: read-write` may apply as agent default if host merges agent defaults — **not proven fail-closed in unit tests**; live proof used explicit mode + prefer executor |

**Net:** bare `general-purpose` without `capability_mode` is the **primary escape path** in the skill-spawn architecture.

---

## Hard guarantees vs soft conventions

### Hard / hard-ish (when mechanisms engaged)

1. **Grok `capability_mode=read-write`** → no Execute tool kind → no worker shell (live L-CAP-SPAWN evidence when correctly set).
2. **Grok `capability_mode=read-only` / plan** + critic/verifier frontmatter → no write/shell/spawn tools (host + agent file).
3. **`omg-executor` `disallowedTools`** → spawn + shell tool ids denied **if** that agent type is used and host honors frontmatter.
4. **`omg accept` / command_policy** → only allowlisted argv can produce CLI-stamped acceptance; models cannot legitimately set `verified` via CLI API (state token path).
5. **dual-review / ralplan RO stages** → process-level `safe=True` + `--disallowed-tools` shell clamp (`dual_review.py` L269–279, `ralplan.py` L344–358).
6. **Process-fanout opt-in gate** → exit 2 without `OMG_EXPERIMENTAL_PROCESS_FANOUT=1`.
7. **Cancel kill path fail-closed** on pid starttime (`state.py` — separate from capability isolation).

### Soft conventions (model/prompt / fail-open)

1. Leader **must** pass `capability_mode` on every spawn (prompt-only; no omg interceptor).
2. Depth=1 “children must not spawn” (prompt + executor disallowedTools; orchestrator/general-purpose can still have spawn if mis-spawned).
3. No external agent CLIs as workers (prompt + PreToolUse soft-gate).
4. Isolation worktrees / result envelopes (skills + `omg worker prepare/seal`; workers without shell **cannot** git-commit themselves — leader/CLI owns seal).
5. Process-fanout “one slice, no re-fanout” (prompt).
6. PreToolUse deny list (fail-open on timeout/crash/malformed; only external-agent bins, not full interpreter lockdown).

---

## Top 5 concrete code hooks for **fail-closed** spawn

These are the highest-leverage places to convert “MUST in prompt” into refuse-or-rewrite (product decision backlog):

| # | Hook site | Mechanism | Fail-closed behavior |
|---|-----------|-----------|----------------------|
| **1** | **PreToolUse (or host middleware) on `spawn_subagent`** | Extend `omg_cli/deny.py` / new decision path for toolName `spawn_subagent` | **Deny** if `capability_mode` missing, or implementer agent with `execute`/`all`, or RO role with RW/execute; reason string for live oracle |
| **2** | **Default agent capability merge** | Host or plugin agent registry: map `omg-executor`→RW no-shell, critic/verifier→RO; refuse unknown write agents | Spawn without mode inherits **safe** defaults instead of general-purpose full |
| **3** | **Ban unconstrained `general-purpose` for write slices** | Policy in same interceptor + skill update | Require `omg-executor` (or explicit RW + disallowedTools shell+spawn) for any write path |
| **4** | **Nested-spawn hard deny at depth≥1** | Host already may have depth; product: PreToolUse deny `spawn_subagent` when `subagent_type`/depth indicates child | Enforce depth=1 even if child ignores prompt (executor already lists disallowed; general-purpose does not) |
| **5** | **Leader argv / session policy for pure implementer sessions** | Optional `build_grok_argv(disallow_shell=True)` for worker-only launches; process fanout default clamp when role=implementer | Close process-fanout residual where `disallow_shell=False` today (`fanout.py` L305–317) |

**Non-goals for fail-closed spawn:** relying on PreToolUse regex alone for interpreter escapes; marketing hooks as sandbox; claiming leader shell is isolated (leader is privileged by design — `docs/research/live-gates-2026-07-20-suite.md` residual).

**Supporting already-hard paths to keep:**

- dual-review/ralplan RO `disallow_shell` pattern as template for worker sessions.
- `omg-executor` frontmatter as the preferred implementer type.
- `omg worker prepare/seal` so RW-no-shell workers can still produce envelopes without Execute.

---

## Risks (confidence)

| Risk | Confidence | Notes |
|------|------------|-------|
| Leader omits `capability_mode` on `general-purpose` → child gets shell → interpreter/`npx`/agent CLI | **HIGH** | Documented residual; no spawn gate |
| Leader has shell by design → can always run external CLIs unless PreToolUse + discipline | **HIGH** | Intentional; isolation proof is on **spawned implementer** |
| PreToolUse fail-open / missing global hook install → soft-gate silent miss | **HIGH** | Live canary showed plugin-only hooks insufficient; global install required |
| Process fanout workers ≈ full leaders (shell + no host capability injection) | **HIGH** | Explicit in fanout.py comments |
| `read-write` still allows Task/spawn residual unless disallowedTools | **MED** | security-model L11; executor bans spawn; general-purpose may not |
| Agent frontmatter `capabilityMode` alone without spawn arg | **MED** | Likely host merge when agent type set; not unit-tested as fail-closed in omg suite |
| Wrong tool id for `--disallowed-tools` / TUI ignore headless flags | **MED** | security-model layer 2 residual |
| Model follows HARD RULES reliably in practice | **LOW–MED** | Depends on model; product must not assume |
| Nested orchestrator child fans out again | **MED** | Prompt forbids; only executor frontmatter hard-bans spawn |
| `OMG_ALLOW_EXTERNAL_CLI=1` on parent process disables PreToolUse denylist | **HIGH** | `deny.py` L83–84 process-env only (good design; footgun if exported widely) |

---

## Implications for product decisions

1. **Primary isolation today is host `capability_mode` + correct agent type**, not omg CLI enforcement and not PreToolUse.
2. **omg’s contribution is prompt injection + agent files + RO-stage argv clamps + acceptance ownership + optional process supervisor** — not a spawn sandbox.
3. **Any claim “workers cannot shell” must be conditioned on:** leader actually set `capability_mode=read-write` (or RO) **and** host honored it (or agent disallowedTools applied). Without fail-closed spawn hook #1–3 above, this is **probabilistic**.
4. **Process fanout is not a security upgrade** over skill spawn; treat as experimental concurrency.
5. **Next isolation P0** (if product wants hard guarantee): intercept `spawn_subagent` fail-closed; prefer banning bare full `general-purpose` for write work.

---

## Key file index (absolute)

| Path | Role |
|------|------|
| `<repo-root>/omg_cli/modes.py` | Prompt/HARD_RULES/argv; leader shell kept |
| `<repo-root>/omg_cli/fanout.py` | Experimental multi-PID; capability prompt-only |
| `<repo-root>/omg_cli/main.py` | Process fanout env gate |
| `<repo-root>/omg_cli/deny.py` | PreToolUse soft-gate (shell + external bins only) |
| `<repo-root>/omg_cli/dual_review.py` | RO sequential headless + disallow_shell |
| `<repo-root>/omg_cli/ralplan.py` | RO stages force plan + disallow_shell |
| `<repo-root>/omg_cli/workers.py` | prepare/seal for no-shell implementers |
| `<repo-root>/agents/omg-executor.md` | RW + ban shell/spawn |
| `<repo-root>/agents/omg-orchestrator.md` | Leader; no capability frontmatter |
| `<repo-root>/agents/omg-critic.md` | RO leaf |
| `<repo-root>/agents/omg-verifier.md` | RO leaf |
| `<repo-root>/skills/omg-ultrawork/SKILL.md` | ULW spawn contract (prompt) |
| `<repo-root>/skills/omg-ralph/SKILL.md` | Ralph spawn contract (prompt) |
| `<repo-root>/docs/security-model.md` | Canonical hardness table |
| `<repo-root>/docs/research/subagent-pretooluse-spike.md` | Inherit hooks + primary=capability |
| `<repo-root>/docs/research/live/cap-spawn-20260719T190456Z.txt` | Live RW no-shell when correctly spawned |

---

*End of explore report. No code changes beyond this research artifact.*
