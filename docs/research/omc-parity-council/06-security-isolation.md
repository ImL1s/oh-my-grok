# 06 — Security / Isolation Stack Auditor

**Role:** Grok advisor #6 (SECURITY)  
**Date (UTC):** 2026-07-20  
**Repo:** `<repo-root>`  
**Baseline:** plugin **0.2.5** · spawn Option A **`8f3bef4`** · evidence through live suite `20260719T190456Z`  
**Scope:** isolation layers, fail-open honesty, OMC-class parity for **security only**  
**Method:** read-only code + docs + unit tests + dated live canary/cap-spawn (no product edits)

---

## Executive risk level

**Overall isolation risk (honest product use): MEDIUM**  
**Marketing overclaim risk if wording is sloppy: HIGH**

Primary isolation (**host `capability_mode` without Execute on implementers**) has **live evidence**. Soft-gates (PreToolUse) work when **global** hooks are installed, but remain **fail-open**. Leader shell and process-fanout are intentional residual blast surfaces.

---

## 1. Isolation layers that actually work (evidence)

Ordered strongest → weakest. “Works” means: mechanism exists in code **and** has unit and/or live evidence, not merely skill prose.

| # | Layer | Hardness | Evidence | What it actually stops |
|---|--------|----------|----------|------------------------|
| **L1** | Host `capability_mode` on `spawn_subagent` | **Hard-ish (host toolset)** | Live **L-CAP-SPAWN** `docs/research/live/cap-spawn-20260719T190456Z.txt` + suite note: `DENIED_OR_RAN=denied`, child `omg-executor` / `read-write` reported **no** `run_terminal_command` in toolset (`CHILD_ID=019f7bc8-…`). Documented in `docs/security-model.md` §Layer 1, `docs/research/live-gates-2026-07-20-suite.md`. | Implementer **cannot** get shell tool → no `python -c` / `npx` / agent CLI **from that worker**. Critic/verifier with `read-only`: no write + no Execute. |
| **L2** | Agent frontmatter `disallowedTools` | **Hard when host honors agent defs** | `agents/omg-executor.md`: `disallowedTools: spawn_subagent, run_terminal_command, run_terminal_cmd` + `capabilityMode: read-write`. Critic/verifier ban spawn + shell (+ critic bans `search_replace`). | Depth=1 leaf + double-deny shell/spawn even if capability mis-set partially. |
| **L3** | Parent CLI `--disallowed-tools` / `disallow_shell` | **Hard when argv injected** | `omg_cli/modes.py` `build_grok_argv(disallow_shell=…)`; `OMG_DISALLOW_SHELL=1`. Used by **dual-review** + **ralplan RO** stages (`dual_review.py`, `ralplan.py`). **Not** default on ulw/ralph leaders. | Headless RO sessions strip shell tool IDs from available set (gate, not OS sandbox). |
| **L4** | PreToolUse command-position deny | **Soft (fail-open)** but **live-proven when global** | `omg_cli/deny.py` `should_deny_command` + `decide_pre_tool_use`; hook `hooks/bin/pre_tool_use_deny.py`; matcher in `hooks/hooks.json`. Live canary `docs/research/canary-pretool-latest.json`: **`DENIED_PARENT_AND_CHILD`**, exit 0, `parent_host_signature` + `child_host_signature` true, marker absent. Spike: `docs/research/subagent-pretooluse-spike.md` (host source: subagents inherit PreToolUse). Unit: `tests/test_deny.py` (wrappers, `sh -c`, env-prefix in cmd string still deny). | Blocks `claude`/`codex`/`omc team`/… as **command head** when hook healthy + host honors deny. |
| **L5** | Spawn capability_mode soft fail-closed (Option A / `8f3bef4`) | **Soft policy gate** (still host fail-open) | `decide_spawn_subagent` in `omg_cli/deny.py`: missing mode → deny; `execute`/`all` → deny; role table mismatch → deny. Matcher includes `spawn_subagent\|Task`. Doctor hard-fails if matcher missing (`check_pre_tool_use`, `check_global_pretool_hook`). Unit: `test_spawn_missing_capability_mode_denied`, executor RW allow, explore RO, Task alias camelCase. | Reduces “forgot capability_mode → general-purpose full tools” footgun **when PreToolUse runs**. |
| **L6** | Acceptance semantic command policy | **CLI hard gate (operator path)** | `omg_cli/command_policy.py` `POLICY_VERSION=2`; floors: shells, agent CLIs, `npx`/`uvx`, `python -c/-e`, git mutate, make/cargo/go/dart/flutter grammar. `tests/test_command_policy.py` extensive. Live accept gate in suite → `verified=true` only via CLI. | Only frozen runner families execute under `omg accept`; not a sandbox for approved pytest/repo code. |
| **L7** | CLI-only `verified` + process-local acceptance token | **Hard against disk forge in same process model** | `omg_cli/acceptance.py`: `_CLI_ACCEPTANCE_TOKENS`; `is_cli_acceptance_result(require_token=True)`; agent-forged `writer`+`passed`+sha **without** in-process token cannot `set_verified`. Integrate explicitly does not set verified. | Models cannot legitimately stamp `verified` / passes via file write alone in normal CLI flow. |
| **L8** | `omg ask` child-only env + stdin prompt | **User-invoked path isolation** | `omg_cli/ask/broker.py` `child_env_for_ask`: sets `OMG_ALLOW_EXTERNAL_CLI=1` **only** in child env; parent restored. Default `OMG_ASK_STDIN=1` keeps prompt out of argv. `providers.validate_extra` rejects freeform unless `OMG_ASK_ALLOW_EXTRA=1`. | External advisors never default-injected into workers; allow flag not sticky on parent shell. |
| **L9** | Worker prepare/seal + integrate path allowlist | **CLI structural** | `omg_cli/workers.py` prepare/seal (no-shell implementer bridge). `integrate.worktree_path_allowed`: only project root or `.omg/worktrees`. Ancestry + merge-commit reject + optional `changed_files` / `--require-squash`. | Path injection (`worktree_path: /etc`) blocked; multi-commit merge noise reduced. |
| **L10** | Doctor inventory + soft-gate footer | **Ops honesty** | `omg doctor`: hard check **global PreToolUse soft-gate** (`~/.grok/hooks/omg-pretool-deny.json` + spawn matcher + readable deny script). Footer: *“PreToolUse is fail-open soft-gate; not hard guarantee.”* `tests/test_doctor.py`. Install: `scripts/install-plugin.sh` writes global hook. | Prevents silent “plugin installed but soft-gate dead” (live 18:04/18:08 REAL_CLI_RAN without global hook). |

### What does **not** count as proven hard isolation

- Skill HARD RULES / prompt reminders alone (convention; models can ignore).  
- OS `--sandbox` (documented default **off**).  
- Permission `--allow`/`--deny` as a general allowlist engine.  
- PreToolUse without global install (live counter-evidence: REAL_CLI_RAN).

---

## 2. Fail-open surfaces (honest gaps)

| Surface | Failure mode | Blast radius | Severity |
|---------|--------------|--------------|----------|
| **PreToolUse timeout / crash / missing binary / malformed JSON** | Host runs tool anyway (`decide_pre_tool_use` except path returns allow on exception; host policy fail-open) | Leader (or any shell-capable session) can run external agent CLIs / arbitrary cmds | **HIGH** if relied on as primary |
| **Plugin-only hooks** | Live 2026-07-19: plugin `hooks/hooks.json` may not appear in `hook_execution`; only global/settings fired | Soft-gate **dead** until `install-plugin.sh` / global JSON | **HIGH** until doctor green |
| **Leader retains shell** | By design (R1); isolation proof is on **spawned implementer** | Compromised/over-eager leader = full shell + can spawn poorly | **HIGH** (operator trust) |
| **`omg ulw --fanout process`** | `fanout.py` sets `disallow_shell=False` for process workers | N× independent `grok -p` **with shell**; conflicts with “spawn-only workers” narrative | **HIGH** if used as default |
| **Interpreter escapes when shell present** | Deny is command-position regex for agent bins — **not** `python -c` / `node -e` on PreToolUse path | Leader (or process fanout) can escape soft-gate | **HIGH** without capability_mode |
| **`OMG_ALLOW_EXTERNAL_CLI=1` process env** | Explicit bypass of PreToolUse deny | Any process that inherits env can run external CLIs | **MEDIUM** (must stay child-only) |
| **`OMG_ALLOW_UNSAFE_SPAWN=1`** | Bypasses spawn capability_mode gate | Spawns without mode / wrong mode allowed by soft-gate | **MEDIUM** |
| **Unknown `subagent_type` with explicit RW/RO** | `required_capability_mode` → `None` → allow if mode present | Role-mismatched unknown agents still get tools host grants | **LOW–MEDIUM** |
| **Omitted capability_mode host fallback** | Host defaults (`general-purpose` ≈ full) if soft-gate also fails | Full toolset child | **CRITICAL** if both L1 omitted **and** L5 soft-gate dead |
| **Acceptance allowlist ≠ sandbox** | Frozen `pytest` / project `.py` still runs **repo code** | Malicious tests in-tree execute under operator approval | **MEDIUM** (documented) |
| **`--no-allowlist` TTY break-glass** | Floors remain; positive allowlist skipped | Broader bins (`curl`, …) under operator | **LOW–MEDIUM** |
| **Integrate path allowlist is prefix, not full git-worktree identity** | Does not require `git worktree list` / common-dir identity | Nested non-worktree dirs under project still allowed | **LOW** |
| **Stop / SubagentStop hooks** | Passive only on Grok (non-blocking) | No isolation contribution; cannot pin continue | **N/A** (not a gate) |
| **Dual-review sequential headless** | Not native spawn; relies on `disallow_shell` + prompts | Weaker than RO spawn critic/verifier path | **MEDIUM** product gap |

---

## 3. Spawn `capability_mode` fail-closed status **post `8f3bef4`**

### Status: **IMPLEMENTED as soft fail-closed (Option A) — NOT host hard fail-closed**

| Check | Result | Evidence |
|-------|--------|----------|
| Code path | **Yes** | `omg_cli/deny.py` `decide_spawn_subagent` + `decide_pre_tool_use` maps `Task`→`spawn_subagent` |
| Hook matcher | **Yes** | `hooks/hooks.json` matcher: `run_terminal_command\|Bash\|Shell\|spawn_subagent\|Task` |
| Global install template | **Yes** | `scripts/install-plugin.sh` writes same matcher to `~/.grok/hooks/omg-pretool-deny.json` |
| Doctor hard checks | **Yes** | Plugin hooks + global soft-gate both require spawn matcher |
| Unit tests | **Yes** | Missing mode deny; GP+RO deny; explore+RW deny; execute deny; executor RW allow; camelCase Task |
| Live proof of **spawn gate itself** | **Partial** | L-CAP-SPAWN proves **host capability_mode toolset** (child lacked shell). It does **not** separately prove PreToolUse denied a spawn missing `capability_mode`. Canary proves PreToolUse on **shell** for parent+child. |
| Escape hatch | Process env only | `OMG_ALLOW_UNSAFE_SPAWN=1` → allow |
| Residual wording | Correct in security-model | “Still a **soft-gate** (host fail-open on hook crash/timeout). Primary isolation remains host `capability_mode` when correctly set.” |

### Policy table (implemented)

| `subagent_type` (normalized) | Required mode | Notes |
|------------------------------|---------------|-------|
| `omg-executor`, `general-purpose`, `*executor*`, OMC executor alias | `read-write` | No Execute on host when honored |
| `explore`, `plan`, `omg-critic`, `omg-verifier`, OMC explore/critic/… | `read-only` | Substring `critic`/`verifier`/`explore` also RO |
| Unknown type | Any valid `read-write`/`read-only` | Mode **must** still be present |
| Any + `execute`/`all` | **Deny** | Never allowed for default workers |
| Missing mode | **Deny** | Soft-gate only |

### Verdict for roadmap language

- **Say:** “Spawn soft fail-closed for missing/wrong `capability_mode` when PreToolUse is healthy + globally installed; primary isolation is host capability_mode.”  
- **Do not say:** “Spawns are hard-enforced fail-closed by the host” or “workers cannot get shell because PreToolUse blocks spawn.”

---

## 4. What OMC does differently for isolation (known from skills / product surface)

OMC (oh-my-claudecode class, Claude Code host) is **not** a stricter OS sandbox by default; it is a **different host permission + orchestration** stack. From OMC operating docs / skill surface (Claude Code ecosystem) vs OMG evidence:

| Dimension | OMC-class (Claude Code) | oh-my-grok (Grok Build) |
|-----------|-------------------------|-------------------------|
| Fan-out primitive | `Task` / multi-agent + optional **`/team` tmux multi-process** | **Only** `spawn_subagent` depth=1 (process fanout experimental/controversial) |
| Permission model | Claude permission modes (`plan`, allow/deny rules, often session prompts); `--dangerously-skip-permissions` is a known **elevation** footgun called out in operator docs | Host **`capability_mode`** (RO / RW / execute) is first-class product contract + live canary |
| Hook blocking | PreToolUse-class gates exist; Stop `decision:block` used by some peers (e.g. OMX ralph patterns) for **persistence**, not pure isolation | Grok: **only PreToolUse blocks**; Stop passive → persistence is **CLI outer loop**, not Stop pin |
| External multi-LLM workers | Culture of dispatching codex/claude/gemini as workers (operator SOP) | **Default deny** external agent CLIs; advisors only via **`omg ask`** user path |
| State / verified | Project conventions (`.omc/state`, skills) vary; less single-writer token model in open docs | Explicit **CLI-only** `verified` + process token anti-forge |
| Honesty posture | Often markets deep automation; isolation residual less centralized | Canonical `docs/security-model.md` + doctor soft-gate footer + canary exit taxonomy |
| Isolation worktrees | Common skill guidance | Same + **`omg worker prepare/seal`** + integrate allowlist |
| Team isolation | Separate panes/processes — blast radius multiplies with shell per pane | Explicitly **OUT_OF_SCOPE / NEVER** for v1 (no tmux control plane) |

**Implication for parity claims:** OMG is **not** “less secure than OMC” on paper; OMG is **more explicit** about fail-open hooks and **more opinionated** about capability_mode + CLI acceptance ownership. OMC’s breadth (team/autopilot/notifications) increases **operational** surface, not necessarily kernel hardness.

---

## 5. Security-related parity matrix (HAVE / PARTIAL / MISSING)

Labels: **HAVE** | **PARTIAL** | **MISSING** | **NEVER** (host impossible) | **OUT_OF_SCOPE**

| Feature | OMC-class | OMG | Status | Notes |
|---------|-----------|-----|--------|-------|
| Capability isolation (workers without shell) | Permission modes + agent defs | `capability_mode` RO/RW + agent disallowedTools | **HAVE** | Live L-CAP-SPAWN |
| Spawn requires capability_mode | Convention / host-dependent | Soft PreToolUse gate post-`8f3bef4` | **PARTIAL** | Soft, not host-hard |
| PreToolUse external-CLI deny | Varies | Command-position deny + canary | **HAVE** (soft) | Needs global hook |
| PreToolUse parent+child inheritance | Host-dependent | Host source + live canary | **HAVE** (soft) | Fail-open residual |
| PreToolUse canary (shim, no real CLI) | Rare as product | `scripts/canary_pretool.py` + dated JSON | **HAVE** | Pass = host signature deny both |
| Doctor soft-gate install check | Partial | Hard fail if global hook missing | **HAVE** | Better than silent miss |
| Acceptance / verified single-writer | Weak/varied | `omg accept` + token + stamp | **HAVE** | Strong OMG advantage |
| Semantic acceptance argv policy | N/A or ad hoc | `command_policy` v2 floors | **HAVE** | Not a sandbox |
| Ask external advisors isolation | Often freeform | Child env + stdin + extra deny | **HAVE** | User-invoked only |
| Worker prepare/seal no-shell bridge | Skill/git culture | `omg worker prepare/seal` | **HAVE** | Closed in 0.2.4 track |
| Integrate path / ancestry guards | Manual | Path allowlist + ancestor + no merge | **HAVE** | Identity not full git-worktree cert |
| Leader shell locked by default | No (operator) | No (by design) | **PARTIAL** both | Document residual |
| Process multi-agent with shell (team/tmux or N× CLI) | **HAVE** (team) | Experimental process fanout | OMG: **OUT_OF_SCOPE** preferred; process path **PARTIAL risk** | Do not claim spawn-only if process advertised |
| OS/kernel sandbox default on | Optional | Optional, default off | **MISSING** as default | Never market as on |
| Hard “workers cannot run external CLIs” absolute | Overclaim risk | Explicitly forbidden claim without residual | OMG docs **honest** | Keep |
| Stop pin / force-continue isolation | OMX-style block | **NEVER** on Grok host | **NEVER** | Not security layer |
| HUD / wiki / notifications isolation | Present | Absent | **OUT_OF_SCOPE** | Not isolation core |
| Dual-review RO isolation native | Task RO agents | Sequential headless + disallow_shell; skill prefers native spawn | **PARTIAL** | Native spawn dual-review not shipped as CLI |

### Shared BRIEF rows (security-relevant subset)

| Feature | OMC | OMG | Status | Notes |
|---------|-----|-----|--------|-------|
| Capability isolation | permission modes | capability_mode + soft spawn gate | **HAVE** | Primary OMG contract |
| PreToolUse canary | uncommon productized | dated live + classify | **HAVE** | Re-run after Grok upgrades |
| Acceptance / verified gate | weak | strong CLI token | **HAVE** | OMG ahead |
| Ask external advisors | freeform culture | broker isolation | **HAVE** | |
| Team / tmux multi-process | yes | no | **OUT_OF_SCOPE** | Correct non-goal |
| Stop pin / force continue | peers yes | host NEVER | **NEVER** | Not isolation |

---

## 6. Top residual risks (severity × exploitability × blast radius)

### R1 — Leader shell + fail-open PreToolUse (CRITICAL if marketed wrong; HIGH residual)

- **Exploitability:** Remote unauth N/A; **local model/tool** on leader session.  
- **Blast:** Full host shell, external agent CLI, interpreter escapes, state dir if writable.  
- **Mitigation in force:** Operator discipline; optional `OMG_DISALLOW_SHELL`; do not claim leader is sandboxed.  
- **Remediation direction:** Keep honest; optional session clamp for high-risk runs; never elevate soft-gate to primary.

### R2 — Soft-gate dead without global hook (HIGH)

- **Evidence:** REAL_CLI_RAN at 18:04/18:08 vs DENIED_PARENT_AND_CHILD after global install.  
- **Blast:** Entire deny + spawn-mode gate inactive.  
- **Mitigation:** `install-plugin.sh` + `omg doctor` hard check.  
- **Residual:** User skips install; doctor ignored.

### R3 — Process fanout workers with shell (HIGH if enabled)

- **Location:** `omg_cli/fanout.py` `disallow_shell=False`.  
- **Blast:** Parallel full-shell Grok processes outside capability_mode leaf story.  
- **Remediation:** Keep feature-gated / experimental; never default; prefer native spawn.

### R4 — Dual dependency: capability_mode omitted **and** soft-gate fail-open (CRITICAL compound)

- Host falls back to rich tools; hook timeout → no deny.  
- **Mitigation:** Skills MUST set mode; Option A soft gate; executor agent defaults `capabilityMode: read-write`.  
- **Gap:** Live negative test “spawn without mode denied by PreToolUse” not separately archived like canary.

### R5 — Approved acceptance runners execute untrusted repo code (MEDIUM)

- `pytest` / project scripts are intentional.  
- Pair with code review + dual-review; not an isolation bug, but residual.

### R6 — Env bypass stickiness if parent polluted (MEDIUM)

- `OMG_ALLOW_EXTERNAL_CLI` / `OMG_ALLOW_UNSAFE_SPAWN` if exported in shell profile.  
- Ask broker carefully avoids parent sticky allow; operators can still export.

### R7 — Host upgrade drift (MEDIUM)

- Canary / capability_mode semantics may change with Grok versions.  
- **Contract:** Re-run `canary_pretool.py --live` and L-CAP-SPAWN after upgrades; never “pass once = forever.”

### R8 — Integrate `git fetch` from worktree path (LOW–MEDIUM)

- After allowlist, objects are fetched from worker path.  
- Mitigated by path prefix allowlist; still trusts objects under project `.omg/worktrees`.

---

## Security checklist (this audit)

- [x] Secrets scan of isolation surface: no hardcoded API keys/tokens in deny/policy/hooks (env flags only).  
- [x] Injection / command policy reviewed (`command_policy` + deny regex).  
- [x] AuthN/AuthZ analogue: capability_mode + CLI verified ownership.  
- [x] Dependency audit: **N/A product** (stdlib-first Python plugin); no npm/cargo runtime deps for isolation path.  
- [x] Fail-open honesty vs marketing language checked against `docs/security-model.md` “Do not claim”.  
- [x] Live evidence cited (canary + cap-spawn + suite).  
- [ ] Optional follow-up: dedicated live canary for **spawn missing capability_mode** deny signature (unit-covered only today).

---

## Summary counts

| Severity | Count (residual themes) |
|----------|-------------------------|
| CRITICAL (compound / marketing) | 1 compound (R4) + wording risk |
| HIGH | 3 (R1, R2, R3) |
| MEDIUM | 3 (R5–R7) |
| LOW | 1 (R8) |

**Critical product issues in code (absolute):** none that contradict the **documented** model when global hooks + capability_mode are used as prescribed.  
**Critical process issues:** overclaiming soft-gates as hard sandboxes; shipping process fanout as “parity with OMC team” without shell story.

---

## Bottom line (answers 1–6 compressed)

1. **Working layers:** host capability_mode (live), agent disallowedTools, RO CLI shell strip, PreToolUse soft deny (live with global), spawn soft fail-closed (unit+doctor), acceptance policy+token, ask child env, worker seal, integrate path guards.  
2. **Fail-open:** hooks timeout/crash, plugin-only install, leader shell, process fanout, interpreter escapes when shell present, unsafe env flags.  
3. **Post-`8f3bef4`:** soft fail-closed for spawn **is in tree and tested**; still not host-hard; primary remains capability_mode.  
4. **OMC:** broader multi-process team culture + Claude permission UX; OMG tighter CLI verified ownership and clearer soft-gate honesty; neither is a default kernel sandbox.  
5. **Parity:** core isolation **HAVE**; spawn soft-gate **PARTIAL**; team/tmux **OUT_OF_SCOPE**; hard absolute isolation **MISSING/NEVER** as claim.  
6. **Top risks:** leader shell, dead soft-gate without global hook, process fanout shell, compound omit-mode+fail-open, host upgrade drift.

---

## References (absolute under repo)

- `docs/security-model.md`  
- `docs/research/subagent-pretooluse-spike.md`  
- `docs/research/canary-pretool-latest.json`  
- `docs/research/live-gates-2026-07-20-suite.md`  
- `docs/research/live/cap-spawn-20260719T190456Z.txt`  
- `omg_cli/deny.py`, `command_policy.py`, `acceptance.py`, `ask/broker.py`, `doctor.py`, `workers.py`, `integrate.py`, `fanout.py`  
- `hooks/hooks.json`, `hooks/bin/pre_tool_use_deny.py`  
- `agents/omg-executor.md`, `agents/omg-critic.md`  
- `tests/test_deny.py`, `tests/test_command_policy.py`, `tests/test_doctor.py`  
- `scripts/install-plugin.sh`, `scripts/canary_pretool.py`
