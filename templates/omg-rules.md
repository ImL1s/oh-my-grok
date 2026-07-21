<!-- OMG:START -->
<!-- OMG:VERSION:{{VERSION}} -->
<!-- OMG:SOURCE-HASH:{{SOURCE_HASH}} -->
# oh-my-grok (OMG) — operating contract

OMG orchestrates Grok Build with evidence-gated CLI workflows. This file is the
always-loaded contract. The full skill / agent / CLI catalog is in the plugin's
docs/skills.md — read it on demand, do not preload it.

<scope_boundary>
Change only what the task requires. Do not refactor, rename, reformat, or "clean
up" files outside the requested change. Do not add dependencies that were not
explicitly requested. If the task is ambiguous, ask ONE direct question — never
guess an interpretation and build on it.
</scope_boundary>

<workflow_routing>
Trivial work (one command, a one-line fix, a clarification): do it directly.
Non-trivial work (multi-file, a new feature, unclear requirements): run
`omg interview` then `omg ralplan` before writing product code — that produces
the plan Grok's Plan Mode asks for anyway, so route through it instead of
free-approving Plan Mode step by step. Plan Mode blocking your writes until a
plan is approved is expected, not an obstacle.
Long or autonomous runs: `omg autopilot`, `omg ralph`, or `omg pipeline`. These
are outer CLI loops that re-invoke `grok` with `--rules` (so this contract
survives every headless turn) and `--session-id` / `--resume` between turns.
</workflow_routing>

<subagents>
Fan out only via Grok `spawn_subagent`. Pass `capability_mode` explicitly on
EVERY call — read-only for explore/plan/critic/verifier, read-write for
implementers (general-purpose / omg-executor). Never `execute` or `all` for
default workers, and never assume a default mode.
Subagents cannot spawn subagents (host depth limit = 1). Do not attempt nested
delegation; plan single-level fan-out.
If a spawn is DENIED for a missing/wrong `capability_mode`, RETRY IMMEDIATELY in
the same turn with the required mode. Do not fall back to solo work over one deny.
</subagents>

<verification>
Before claiming a task done: re-run the relevant tests/build yourself, and for
anything gated by OMG (review / QA / acceptance) read the actual evidence file
under `.omg/state/` — a clean run needs a fresh, run-bound verdict, not a stale
or unrelated stamp. Only the `omg` CLI may set `passes` / `verified`; agents
write proposals under `.omg/artifacts/` only. Never report "done" from memory of
what you intended to do.
</verification>

<state>
Durable OMG state lives under `.omg/` (state/, plans/, wiki/, backups/). At
session start, read `.omg/state/RESUME.md` if it exists. OMG's non-PreToolUse
hooks are PASSIVE (Grok only lets PreToolUse block or inject) — nothing re-pushes
context into the chat for you, so after a resume or compaction re-read `.omg/`
yourself instead of assuming context carried over.
</state>

## Cancel
`omg cancel` clears any active OMG mode (autopilot / ralph / ralplan / …) — never
self-matching `pkill -f`.

## Setup
Run `omg setup` after any `omg update` to refresh this contract and `.omg/` state.
<!-- OMG:END -->
