# OMC parity audit brief — oh-my-grok (multi-Grok council)

**date_utc:** 2026-07-20  
**repo:** `<repo-root>`  
**HEAD note:** ~`60d0882` + Option A spawn fail-closed `8f3bef4`  
**version:** 0.2.5 Option B (plugin + `omg` CLI, no Rust fork, no tmux v1)

## User question (answer this)

1. **Does oh-my-grok already have the basic OMC functionality?** Honest matrix, not marketing.
2. **How should “don’t stop until done” work on Grok host** given Stop is non-blocking (only PreToolUse blocks)?
3. **What is still missing** for real product parity (functional, not just renamed skills)?
4. **Priority roadmap** for 0.3.x (what to build vs never build).

## Hard facts already decided (do not re-litigate without new host evidence)

- Stop continuation **DO NOT BUILD** for 0.3.x — see `docs/research/stop-continuation/CONSENSUS.md`
- Grok host: only `PreToolUse` is blocking; Stop is passive
- Persistence = **CLI outer loop** (`omg ralph` / `omg pipeline`), not chat Stop reinject
- Workers = Grok-native `spawn_subagent` only (no claude/codex/omc team as default workers)
- PreToolUse deny is **fail-open honest**; primary isolation = `capability_mode` (no Execute)
- `verified` is CLI-only (`omg accept`); dual-review does not set verified

## OMG surface today (inventory baseline)

**CLI:** `setup doctor state cancel accept integrate worker ulw ralph ralplan ask pipeline dual-review`

**Skills:** omg-using, omg-ultrawork, omg-ralph, omg-ralplan, omg-pipeline, omg-dual-review, omg-cancel, omg-ask

**Agents:** omg-orchestrator, omg-executor, omg-critic, omg-verifier

**Hooks:** SessionStart, PreToolUse (deny external agent CLIs + spawn capability_mode gate), Stop/SubagentStop (passive)

**OMC 4.15.5 skills (reference list, not goals to clone blindly):**  
autopilot, ralph, ralplan, ultrawork, ultragoal, ultraqa, team, omc-teams, plan, deep-interview, deep-dive, ask, ccg, cancel, hud, wiki, verify, visual-verdict, configure-notifications, remember, skillify, sciomc, self-improve, project-session-manager, merge-readiness, autoresearch, …

## Compare also

- OMX (oh-my-codex) Stop `decision:block` + ralph  
- omo / openagent idle injectContinuation / todo-enforcer (if present on disk under ~/.config or plugins)

## Output rules for each agent

- Write markdown to the path given in your task
- Evidence-first: cite file paths / CLI commands / hook behavior
- Use labels: **HAVE** | **PARTIAL** | **MISSING** | **NEVER** (host impossible) | **OUT_OF_SCOPE**
- No magic bare keywords that trigger other products: say `AUTO_PILOT_SKILL` not autopilot bare if quoting triggers; prefer plain “OMC autopilot skill”
- Do NOT edit product source; research + write report only under `docs/research/omc-parity-council/`
- Do NOT shell out to claude/codex/agy as workers

## Shared feature rows (fill these)

| Feature | OMC | OMG | Status | Notes |
|---------|-----|-----|--------|-------|
| Parallel fan-out (ulw) | | | | |
| Persistence loop (ralph) | | | | |
| Plan consensus (ralplan) | | | | |
| Full auto pipeline (autopilot) | | | | |
| Dual / multi review | | | | |
| Ask external advisors | | | | |
| Team / tmux multi-process | | | | |
| Stop pin / force continue | | | | |
| Context pack / resume | | | | |
| Doctor / setup | | | | |
| Cancel | | | | |
| Acceptance / verified gate | | | | |
| HUD | | | | |
| Wiki | | | | |
| Notifications | | | | |
| Deep interview | | | | |
| UltraQA | | | | |
| Ultragoal durable goals | | | | |
| Skill management | | | | |
| Capability isolation | | | | |
| PreToolUse canary | | | | |
