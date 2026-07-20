# External free audit — Fable (Claude Code)

## Status: BLOCKED

**date_utc:** 2026-07-20  
**role:** Independent free-exploration product audit (same brief as Codex `08-codex.md`)  
**outcome:** **No independent long-form report this round.**

---

## Why blocked

Claude Code headless (`claude -p --model claude-fable-5 --effort xhigh`) failed or hung across multiple launches:

| Attempt | Symptom |
|---------|---------|
| Restricted brief, short launcher | Log stuck on `Permission allow rule Write(/**)` noise; no report file |
| Free prompt **before** flags (`claude -p "$PROMPT" --model …`) | `Error: Input must be provided either through stdin or as a prompt argument when using --print` |
| Flags before prompt + `</dev/null` | Same Input must be provided (stdin emptied, prompt lost in some wrappers) |
| Stdin-only prompt file | Process alive minutes, log ~1.5KB permission rules only, 0 tool activity |

**Root cause (ops):** `claude -p` argv contract — **all options before the prompt string**, or feed prompt via **stdin** only. See global skills:

- `~/.agents/skills/dual-review/SKILL.md` § Fable argv contract (2026-07-20)
- `~/.agents/skills/multi-llm-council/SKILL.md` §4c

Also used: empty MCP (`--strict-mcp-config` + empty servers), `OMC_SKIP_HOOKS=1` / `DISABLE_OMC=1` to reduce hijack risk.

---

## Vote for synthesis

**ABSTAIN / BLOCKED** — do **not** count Fable toward multi-advisor consensus this round.

Primary external evidence: **[`08-codex.md`](./08-codex.md)** + multi-Grok `01`–`07` + [`STATUS.md`](./STATUS.md).

---

## How to complete this seat later

```bash
# Correct pattern (options first; prompt last OR stdin)
SAFE=docs/research/omc-parity-council/external-brief-safe.md
OUT=docs/research/omc-parity-council/09-fable.md
echo '{"mcpServers":{}}' > /tmp/empty-mcp.json

claude -p \
  --model claude-fable-5 \
  --effort xhigh \
  --dangerously-skip-permissions \
  --strict-mcp-config \
  --mcp-config /tmp/empty-mcp.json \
  --no-session-persistence \
  --add-dir "$PWD" \
  --add-dir ~/.claude/plugins/cache/omc \
  --add-dir ~/.grok/docs \
  "FREE free-exploration product audit. No workflow modes. Read $SAFE then freely explore the oh-my-grok repo, OMC install, Grok docs, live evidence. Challenge SYNTHESIS with evidence. Write COMPLETE Traditional Chinese report to $OUT using Write/Edit. Do not edit product source except that path."
```

Replace this BLOCKED stub with the real report when the run succeeds.

---

## Related shipped work (not Fable’s report)

Product P0 from Codex + Grok council still shipped without Fable’s vote — see [`STATUS.md`](./STATUS.md) §3–4.
