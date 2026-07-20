# External free audit — Fable

## Status: BLOCKED

**date_utc:** 2026-07-20  
**reason:** Claude Fable CLI headless launches failed or hung repeatedly during free-exploration dispatch.

### Attempts
1. Restricted brief launch → hung (permission-rule spam only)
2. Free launch with prompt before flags → `Error: Input must be provided either through stdin or as a prompt argument when using --print`
3. Correct flag order + argv prompt + `</dev/null` → same Input must be provided
4. stdin-only prompt file → process alive ~3+ min, log stuck ~1496 bytes (permission rules only), 0 tool activity

### Process notes
- PID files + empty MCP + OMC_SKIP_HOOKS=1 used
- dual-review / multi-llm-council skills updated with 2026-07-20 argv contract after this failure
- Re-run when Fable CLI is responsive: short path-only prompt, options before any empty redirect, stdin prompt file preferred

### Vote
**ABSTAIN / BLOCKED** — do not count Fable toward consensus. Use Codex free audit (`08-codex.md`) + multi-Grok council as primary external evidence this round.
