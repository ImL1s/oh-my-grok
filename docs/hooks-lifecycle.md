# Lifecycle hooks (oh-my-grok)

English. **Registry:** [`hooks/registry.json`](../hooks/registry.json)
(loader `omg_cli/hooks_registry.py`).

This is a **first cut** of [#72](https://github.com/ImL1s/oh-my-grok/issues/72).
It documents host mappings and dispatches in-process. It does **not** claim
OMC-style lifecycle coverage on Grok Build.

## Grok honesty

| Canonical event | Grok | Notes |
|-----------------|------|-------|
| `tool.pre` | native blocking | `PreToolUse` soft-gate (`deny.py`). Fail-open. Not a sandbox. |
| `stop.request` / `idle` | native blocking | `Stop` pin on grok **≥0.2.107**, cap **8**/turn, fail-open. |
| `session.start` | native **passive** | stdout ignored. May refresh `RESUME.md`. Never verifies. |
| `subagent.stop` | native **passive** | stdout ignored. Must not `decision:block`. |
| `prompt.submit` | **unsupported** | `UserPromptSubmit` stdout is ignored. Routing lives in `~/.grok/rules/omg.md`. OMG does **not** inject. |
| `tool.post`, `session.end`, `compact.pre` (host) | unsupported / wrapper | No Grok inject. Compaction handoff is CLI-owned and ids-only. |

## Kill switches

```text
DISABLE_OMG=1           # Grok plugin hooks off (existing)
OMG_DISABLE_HOOKS=1     # in-process bus off (alias of DISABLE_OMG for the bus)
OMG_SKIP_HOOKS=a,b,c    # skip named hook ids or events
```

Disabled Antigravity plugins are not executed from these projection files —
the projection is documentation, not an install.

## Continuation

Exactly one loop may own continuation. `resolve_continuation` returns
`refuse`, `adopt_existing`, `artifact_only`, or `none`. Cancel/using always
adopt. This does not set `verified`.

## Privacy / recovery

- Hook output is untrusted: size-bounded, no emitted commands, cannot set
  `verified` / `passes`.
- Advisory hooks fail open. Continuation guard may fail closed without
  writing run state.
- Compact handoff stores run/session/goal/task ids only — never a transcript.

Antigravity projection:
[`docs/parity/projections/antigravity/hooks/`](./parity/projections/antigravity/hooks/)
(**not** live AG evidence).
