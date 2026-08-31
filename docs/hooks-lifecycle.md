# Lifecycle hooks (oh-my-grok)

English. **Registry:** [`hooks/registry.json`](../hooks/registry.json)
(loader `omg_cli/hooks_registry.py`).

This implements the bounded host mappings for
[#72](https://github.com/ImL1s/oh-my-grok/issues/72). It does **not** claim
OMC-style lifecycle coverage where either host lacks an event. Remaining
honesty: no UserPromptSubmit inject and registry timeouts are post-hoc; host
command timeouts are enforced by Grok/Agy.

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

## Antigravity plugin hooks

The plugin root [`hooks.json`](../hooks.json) is the actual Agy plugin
manifest. It registers only documented Agy events:

| Agy hook | Canonical event | Behavior |
|----------|-----------------|----------|
| `PreToolUse` | `tool.pre` | Translates `run_command` / `CommandLine` and reuses the canonical deny gate. Agy receives `allow` or `deny`. |
| `PostToolUse` | `tool.post` / `tool.failure` | Passive, redacted observation; Agy receives `{}`. |
| `Stop` | `stop.request` | Reuses the bounded autopilot stop gate; canonical `block` becomes Agy `continue`. Otherwise allows stop. |

[`hooks/bin/antigravity_hook.py`](../hooks/bin/antigravity_hook.py) reads no
transcript and persists no raw tool arguments or errors. Its journal source is
`antigravity-hook`; rows remain `verified=false`. Malformed/unbound input and
journal failures fail open. `UserPromptSubmit`, `PreInvocation`, and
`PostInvocation` are intentionally absent: Agy does not document
UserPromptSubmit, and OMG does not disguise another event as prompt injection.

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

## Antigravity static projection

[`docs/parity/projections/antigravity/hooks/`](./parity/projections/antigravity/hooks/)
contains a README plus a **hooks.json-shaped** historical parity document.
It is distinct from the root live-plugin manifest and remains non-executable.
Copy via `install_antigravity_hook_projection(dest)` only for parity inspection.
Neither a root manifest nor a projection proves installation or observation.
`omg doctor` reports `ag_configured`, `ag_loadable`, `ag_observed`, and
`ag_healthy` separately.
