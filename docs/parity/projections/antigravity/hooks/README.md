# Antigravity hook projection

**Status:** static parity projection for [#72](https://github.com/ImL1s/oh-my-grok/issues/72).

This is **not** an installed Antigravity plugin, not live AG evidence,
and not proof that `agy` hook discovery works.

Grok honesty:

- `PreToolUse` / `Stop` may block (Stop: grok >=0.2.107, cap 8/turn, fail-open).
- `SessionStart` / `SubagentStop` are **passive** (stdout ignored).
- `UserPromptSubmit` injection is **unsupported** — routing lives in the rules file.

| Hook | Event | Grok capability | Fail policy |
|------|-------|-----------------|-------------|
| `omg.compact.handoff` | `compact.pre` | `wrapper` | `fail-open` |
| `omg.prompt.submit.unsupported` | `prompt.submit` | `unsupported` | `fail-open` |
| `omg.session.start.observe` | `session.start` | `native_passive` | `fail-open` |
| `omg.stop.gate` | `stop.request` | `native_blocking` | `fail-open` |
| `omg.subagent.stop.observe` | `subagent.stop` | `native_passive` | `fail-open` |
| `omg.pretool.deny` | `tool.pre` | `native_blocking` | `fail-open` |
| `omg.continuation.guard` | `workflow.transition` | `reconciled` | `fail-closed` |

Kill switches: `DISABLE_OMG`, `OMG_DISABLE_HOOKS`, `OMG_SKIP_HOOKS`.
