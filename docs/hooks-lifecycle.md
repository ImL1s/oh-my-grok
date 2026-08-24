# Lifecycle hooks (oh-my-grok)

English. **Registry:** [`hooks/registry.json`](../hooks/registry.json)
(loader `omg_cli/hooks_registry.py`).

This is a **first cut** of [#72](https://github.com/ImL1s/oh-my-grok/issues/72).
It documents host mappings and dispatches in-process. It does **not** claim
OMC-style lifecycle coverage on Grok Build. Remaining honesty: no
UserPromptSubmit inject, timeout is not preemptive, Antigravity files are
projections (not live AG).

## Grok honesty

| Canonical event | Grok | Notes |
|-----------------|------|-------|
| `tool.pre` | native blocking | `PreToolUse` soft-gate (`deny.py`). Fail-open. Not a sandbox. |
| `stop.request` / `idle` | native blocking | `Stop` pin on grok **≥0.2.107**, cap **8**/turn, fail-open. |
| `session.start` | native **passive** | stdout ignored. May refresh `RESUME.md`. Never verifies. |
| `subagent.stop` | native **passive** | stdout ignored. Must not `decision:block`. |
| `prompt.submit` | **unsupported** | `UserPromptSubmit` stdout is ignored. Routing lives in `~/.grok/rules/omg.md`. OMG does **not** inject. |
| `tool.post`, `session.end`, `compact.pre` (host) | unsupported / wrapper | No Grok inject. Compaction handoff is CLI-owned and ids-only. |
| `artifact.created`, `job.terminal`, `team.member.transition` | reconciled / wrapper | CLI `emit_wrapper_event` → journal `source=wrapper`. Not host inject. |

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
- **Timeouts** are cooperative: duration is recorded after the handler
  returns (`timeout_kind=post_hoc`). Python cannot preempt a stuck handler.
- **Event journal:** after `dispatch()`, a bounded redacted row is appended
  under `.omg/state/events/` (`source=omg-hooks-bus`) with a monotonic
  sequence per workspace. Journal write failures fail open and never crash
  the session. This is not a `passes` / `verified` stamp.
- **CLI wrapper events:** `emit_wrapper_event(kind, payload)` journals
  `artifact.created` (classified CLI artifact writes), `job.terminal`
  (jobs runtime reached a terminal status), and `team.member.transition`
  (team API heartbeat/shutdown-ack status change) with `source=wrapper`.
  Payload is redacted and field-bounded. Timeout remains post-hoc.
  `prompt.submit` stays unsupported (no UserPromptSubmit inject). Never
  live AG hook install. Never sets `verified`.

## Registry load (fail-closed)

- `HOST_HOOK_ALLOWLIST` maps each canonical event to allowed `host_hook`
  names. Unknown names fail closed. `host_hook: null` is always allowed
  (CLI wrapper / reconciled).
- Production load requires bundled security ids: `omg.pretool.deny`,
  `omg.stop.gate`, `omg.continuation.guard`. Test stubs may pass
  `allow_incomplete=True`.

## Antigravity projection

[`docs/parity/projections/antigravity/hooks/`](./parity/projections/antigravity/hooks/)
contains a README plus a **hooks.json-shaped** document. Copy via
`install_antigravity_hook_projection(dest)` (later #77 install manifest).
This is **not** live AG evidence and does **not** mean `agy` loaded hooks.
`omg doctor` reports the registry inspect tiers; `omg setup` still installs
Grok plugin hooks.
