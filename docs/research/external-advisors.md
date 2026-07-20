# External advisors — Codex + Claude (Fable) ops notes

**date_utc:** 2026-07-20  
**Purpose:** Repo-local copy of process rules used when fanning out to **Codex CLI** and **Claude Code (Fable)** for oh-my-grok audits/reviews.  
**Source skills (global):** `~/.agents/skills/dual-review/SKILL.md`, `~/.agents/skills/multi-llm-council/SKILL.md`  
**Related:** [`omc-parity-council/STATUS.md`](./omc-parity-council/STATUS.md), [`omc-parity-council/09-fable.md`](./omc-parity-council/09-fable.md)

This document is **operational**, not product marketing. Follow it when re-running external seats.

---

## Roster (default)

| Seat | CLI | Model / effort | Notes |
|------|-----|----------------|--------|
| A | `codex exec` | `gpt-5.6-sol` + `model_reasoning_effort=max` | Free explore or code review |
| B | `claude -p` | `claude-fable-5` + `--effort xhigh` | Never substitute older Opus without explicit override |

---

## 1. Keyword sanitization (HARD — 2026-07-19)

OMC / OMX / oh-my-* **UserPromptSubmit** hooks may hijack sessions if brief/argv contains bare workflow words.

| Dangerous bare token | Safe stand-in |
|----------------------|---------------|
| `autopilot` | `AUTO_PILOT_SKILL` |
| `ultrawork` / `ulw` | `ULTRA_WORK_SKILL` / `ULW_ALIAS` |
| `ralph` | `RALPH_SKILL` |
| `ralplan` | `RAL_PLAN_SKILL` |
| `ultragoal` | `ULTRA_GOAL_SKILL` |

**Required packaging:**

1. Write full brief to disk (`<<'EOF'` quoted).  
2. Regex-sanitize → `*-safe.md`.  
3. Launcher prompt = **short path-only** string + “No workflow modes”.  
4. Fixed answer path under repo or `/tmp`.

```bash
# ❌ hijack risk
codex exec "… $(cat /tmp/brief-with-ralph-ulw.md)"

# ✅
SAFE=/path/to/external-brief-safe.md
OUT=/path/to/report.md
# prompt only references $SAFE and $OUT
```

Sanitize example:

```bash
python3 <<'PY'
from pathlib import Path
import re
src = Path("docs/research/omc-parity-council/external-brief-full.md").read_text()
for pat, rep in [
    (r"(?i)ultrawork", "ULTRA_WORK_SKILL"),
    (r"(?i)ultragoal", "ULTRA_GOAL_SKILL"),
    (r"(?i)autopilot", "AUTO_PILOT_SKILL"),
    (r"(?i)ralplan", "RAL_PLAN_SKILL"),
    (r"(?i)\bralph\b", "RALPH_SKILL"),
    (r"(?i)\bulw\b", "ULW_ALIAS"),
]:
    src = re.sub(pat, rep, src)
header = (
    "# SANITIZED BRIEF\n"
    "DO NOT activate orchestration workflow modes.\n"
    "Map: ULW_ALIAS=parallel, ULTRA_WORK_SKILL=parallel-engine, "
    "RALPH_SKILL=persist-loop, RAL_PLAN_SKILL=plan-consensus, "
    "AUTO_PILOT_SKILL=full-pipeline, ULTRA_GOAL_SKILL=durable-goals.\n\n"
)
Path("docs/research/omc-parity-council/external-brief-safe.md").write_text(header + src)
PY
```

---

## 2. Claude / Fable `claude -p` argv contract (HARD — 2026-07-20)

CLI shape: `claude [options...] [prompt]`.

With `-p` / `--print`, prompt must come from:

- **last positional argument**, **or**
- **stdin**

Both empty →:

```text
Error: Input must be provided either through stdin or as a prompt argument when using --print
```

### Correct

```bash
echo '{"mcpServers":{}}' > /tmp/empty-mcp.json

# A) options first, short prompt last
claude -p \
  --model claude-fable-5 \
  --effort xhigh \
  --dangerously-skip-permissions \
  --strict-mcp-config \
  --mcp-config /tmp/empty-mcp.json \
  --no-session-persistence \
  "Read ONLY $SAFE. Write complete report to $OUT. No workflow modes."

# B) multi-line via stdin
claude -p \
  --model claude-fable-5 \
  --effort xhigh \
  --dangerously-skip-permissions \
  --strict-mcp-config \
  --mcp-config /tmp/empty-mcp.json \
  --no-session-persistence \
  < /tmp/fable-prompt.txt
```

### Wrong

```bash
# prompt before flags
claude -p "$PROMPT" --model claude-fable-5 --effort xhigh ...

# empty stdin + no trailing prompt
claude -p --model claude-fable-5 </dev/null

# --bare → Not logged in (skip for authenticated review)
claude -p --bare ...

# plan mode when Write is required
claude -p --permission-mode plan "write report to path..."
```

### Symptom table

| Symptom | Cause | Fix |
|---------|-------|-----|
| `Permission allow rule Write(/**)` spam only | settings noise + MCP cold start | empty MCP + strict; wait; not auto-fail |
| `Input must be provided…` | prompt lost / flag order | options first or stdin prompt |
| log ~1.5KB, 0% CPU, minutes | hang / bad start | kill PID file; relaunch with contract |
| `Not logged in` | `--bare` | never for review |
| No answer file | plan mode / no Write / OMC cancel | force Write path; skip plan mode |

Prefer: Fable **Write report to fixed path** + orchestrator reads file; stdout redirect is backup.

---

## 3. Codex reliability

| Symptom | Fix |
|---------|-----|
| `writing is blocked by read-only sandbox` | Report under repo → `-s workspace-write`; forbid product code edits in prompt |
| Hijacked by `$autopilot` / `$ultrawork` | Keyword sanitize + short launcher |
| Skill budget / stripped skills noise | Ignore |
| Wrong effort | Always `model_reasoning_effort=max` with `gpt-5.6-sol` |

```bash
codex exec \
  --dangerously-bypass-approvals-and-sandbox \
  --skip-git-repo-check \
  -s workspace-write \
  --model gpt-5.6-sol \
  -c model_reasoning_effort="max" \
  --cd "$PROJECT" \
  "Read ONLY $SAFE. Write complete Traditional Chinese report to $OUT. No product code edits. No workflow modes." \
  </dev/null
```

---

## 4. Process safety (PID kill, not pkill -f)

**Never** self-matching `pkill -f` / `pgrep -f` with long patterns inside the same shell that launches workers (Grok/Claude bash wrappers may match themselves).

Use PID files:

```bash
( codex exec … >"$OUT/codex.log" 2>&1 ) &
echo $! > /tmp/council-codex.pid

( claude -p … >"$OUT/fable.log" 2>&1 ) &
echo $! > /tmp/council-fable.pid

# stop
kill "$(cat /tmp/council-fable.pid)" 2>/dev/null || true
```

OK: `pkill -x claude` (basename only) or a **separate** short command that only kills a numeric PID.

---

## 5. Parallel dispatch checklist

1. Write `BRIEF` + `*-safe.md`  
2. Start N workers with PID files  
3. Poll **answer file size / DONE**, not long `pgrep -f`  
4. Timeout: `kill $(cat pidfile)` only  
5. Synthesis: **strictest wins**  
6. BLOCKED seat → stub file (`BLOCKED` header) so synthesis can abstain  

---

## 6. 2026-07-20 seat outcomes (oh-my-grok)

| Seat | Outcome | Artifact |
|------|---------|----------|
| Codex free audit | **Done** | `omc-parity-council/08-codex.md` |
| Fable free audit | **BLOCKED** | `omc-parity-council/09-fable.md` |
| Grok multi-agent council | Done | `omc-parity-council/01`–`07`, `SYNTHESIS.md` |

Re-run Fable: see `09-fable.md` § “How to complete this seat later” + §2 of this file.

---

## 7. Anti-patterns

- Mega-script that launches and `pkill -f`s the same names  
- `$(cat huge-brief)` as sole prompt  
- Codex `-s read-only` when must write report under repo  
- Fable `--permission-mode plan` when must Write  
- Treating permission-rule spam alone as success/failure  
- Claiming Fable voted when only BLOCKED stub exists  
