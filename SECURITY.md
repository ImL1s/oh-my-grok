# Security Policy

## Product isolation model

Canonical honesty table: [`docs/security-model.md`](docs/security-model.md).

Short version:

- **Primary isolation** is Grok `capability_mode` on `spawn_subagent` (workers without Execute).
- **PreToolUse deny** is a **fail-open soft-guard**, not a hard sandbox.
- Escape hatches (`OMG_ALLOW_EXTERNAL_CLI`, `OMG_ALLOW_UNSAFE_SPAWN`, …) default **off** and must never be exported in shell profiles for normal use.
- Prefer `omg ask` for external advisors; do not use agent CLIs as default workers.

## Reporting a vulnerability

Please open a **private** security advisory on GitHub if available, or contact the maintainer via the GitHub profile linked from this repository.

Do **not** file public issues for unpatched RCE / secret-exfil paths until a fix or coordinated disclosure window exists.

## Scope

In scope: `omg` CLI, plugin hooks/skills/agents, documented install path, acceptance policy bypasses.

Out of scope: the host Grok Build runtime itself (report to xAI), third-party agent CLIs invoked by users, and research notes under `docs/research/` that are not product surface.
