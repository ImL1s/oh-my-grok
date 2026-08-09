# Medley-native agent model routing

**Status:** Proposed  
**Date:** 2026-08-09  
**Target branch:** `main`  
**Tracking issue:** [ImL1s/oh-my-grok#131](https://github.com/ImL1s/oh-my-grok/issues/131)  
**Host contract:** [ImL1s/medley#287](https://github.com/ImL1s/medley/issues/287)  
**Counterpart plan:** [Medley plugin-facing native route contract](https://github.com/ImL1s/medley/blob/providers/docs/plans/2026-08-09-plugin-facing-native-subagent-route-contract.md)

## Decision

oh-my-grok owns the **product policy** that maps OMG agents and categories to preferred native Medley catalog routes and model-family prompt profiles. Medley owns the **execution truth** that determines whether a route is ready and eligible, creates the native child session, resolves credential/access identity, records the route receipt, and admits or refuses runtime fallback.

```text
OMG agent/profile policy
    role + ordered catalog IDs + prompt profile + hard requirements
                         │
                         ▼
Medley native route contract
    eligibility + effective route + child session + route receipt
                         │
                         ▼
OMG workflow evidence
    policy digest + Medley receipt digest + attempt lineage
```

This plan is the architecture summary. The complete implementation scope, test matrix, acceptance criteria, and PR slices live in OMG #131.

## Why the policy belongs in OMG

OMG already defines:

- orchestrator, executor, critic, verifier, debugger, designer, writer, and reviewer behavior;
- role-specific capability and tool floors;
- depth-1 leaf-worker rules;
- ULW, Ralph, Ralplan, Autopilot, Team, evidence, and acceptance semantics;
- external CLI worker adapters and Team routing.

Choosing which model family and candidate order best fits an OMG role is therefore product policy. Putting those names and rankings in Medley would bind the host runtime to one orchestration product.

Conversely, OMG must not recreate Medley's provider catalog, endpoint/auth logic, readiness, access-profile identity, or child sampling client.

## Related work

This work is separate from but coordinated with:

- [#71](https://github.com/ImL1s/oh-my-grok/issues/71): canonical agent taxonomy, aliases/tiers, and Grok/Antigravity projections;
- [#69](https://github.com/ImL1s/oh-my-grok/issues/69): Team tasks, mailbox, members, worktrees, recovery, and worker topology;
- [#68](https://github.com/ImL1s/oh-my-grok/issues/68): durable jobs;
- [#67](https://github.com/ImL1s/oh-my-grok/issues/67): Antigravity adapter;
- [Medley #19](https://github.com/ImL1s/medley/issues/19): generic capability-aware candidate eligibility;
- [Medley #18](https://github.com/ImL1s/medley/issues/18): replay-safe provider/model failover;
- [Medley #287](https://github.com/ImL1s/medley/issues/287): the generic native request/receipt contract.

#71 decides which agent/profile contracts exist. #131 decides how those contracts carry native model policy. #69 consumes the resulting route kind and receipts but remains the owner of Team state.

## Responsibility boundary

### OMG owns

- canonical agent/profile/category identity;
- ordered Medley catalog preferences;
- model-family prompt and reasoning preferences;
- workflow-specific policy selection;
- project/user overrides of OMG policy;
- policy provenance and user-facing explanation;
- external CLI executor selection and process topology;
- migration from ambiguous existing routing terms.

### Medley owns

- catalog IDs and duplicate-wire-slug disambiguation;
- final provider/backend/origin/readiness;
- credentials, access profiles, billing/usage scope, and connection identity;
- capability, context, modality, local-only, and harness eligibility;
- native child sessions, persistence, resume, and usage;
- immutable route receipts and replay-safety admission.

OMG references catalog IDs and receipt digests. It does not accept or store provider credential material.

## Native and external routes

OMG currently routes external Team panes using a field named `provider`. That represents an executor CLI, not a Medley API provider.

The product schema should distinguish:

```text
NativeAgentRoute
  kind: native
  agent_profile: omg-verifier
  catalog_candidates:
    - review-primary
    - review-fallback
  prompt_profile: gpt-family

ExternalExecutorRoute
  kind: external_executor
  executor: codex
  model: optional executor-specific model flag
```

Compatibility requirements:

- existing external `--routing` input continues to work through a versioned migration/alias;
- new persisted state uses `executor`, not an ambiguous `provider`, for CLI workers;
- native catalog IDs are not passed through external argv builders without an explicit adapter mapping;
- external executor names are never interpreted as Medley provider/access identities;
- status and JSON identify native route and external executor separately.

## Canonical agent policy source

Do not create another registry beside #71.

The #71 canonical manifest should either contain the native policy or reference one schema-versioned policy file. It remains the single relationship between:

- canonical agent/profile ID and aliases;
- role/category/tier;
- capability/tool floor;
- native model policy ID;
- ordered Medley catalog candidates;
- model-family prompt profile;
- reasoning/effort preference;
- fallback mode/budget;
- result/evidence contract;
- Grok and Antigravity projections.

Generated prompts may name policy/profile IDs and bounded behavioral instructions. They must not contain credentials, endpoints, machine paths, or mutable availability snapshots.

Conceptually:

```yaml
schemaVersion: 1
agents:
  omg-verifier:
    capabilityMode: read-only
    nativeRoute:
      policyId: verifier.default
      candidates:
        - catalog: review-primary
          promptProfile: gpt-family
          reasoning: xhigh
        - catalog: review-fallback
          promptProfile: gemini-family
          reasoning: high
      requirements:
        structuredOutput: true
```

The final fields must match the generic declarative schema established by Medley #287.

## Model-family prompt profiles

Prompt adaptation remains OMG policy because it changes role behavior, not transport.

The initial profile set should be explicit and versioned:

```text
claude-family
gpt-family
gemini-family
generic
```

Rules:

- model family is taken from qualified route/model metadata, not guessed only from provider name;
- gateways serving the same model family normally use the same prompt profile;
- unknown/unqualified families use `generic` or fail when a qualified profile is mandatory;
- a prompt profile cannot widen tools, capability, harness, or access scope;
- policy/profile changes alter the OMG policy digest;
- unsupported models are reported honestly rather than presented as equivalent because a prompt exists.

## Policy precedence

OMG resolves policy intent in this order:

1. explicit operator/per-run exact catalog override;
2. project-scoped OMG override;
3. user-scoped OMG override;
4. canonical agent/profile policy;
5. parent inheritance only when the winning policy explicitly declares `inherit`.

Medley then applies route semantics:

- exact route: exact or fail closed;
- ordered candidates: first fully eligible catalog route;
- inherit: parent route only because inheritance was explicit;
- resume: source route/model and receipt remain pinned;
- capability/readiness/harness/access requirements are never relaxed by OMG.

A missing exact verifier route must not silently become the orchestrator's model.

## Override rollout

Read-only built-in policy diagnostics should ship before mutation UX.

User/project override requirements are fixed even if the final file location waits on Medley #287:

- one documented user layer and one project layer;
- project policy may select configured catalog IDs but cannot define credentials/endpoints or weaken access ownership;
- unknown agent/profile/policy/catalog IDs fail visibly;
- exact `model` and ordered `models` syntax cannot conflict silently;
- empty chains are invalid;
- provenance is visible in `omg agents explain` and run evidence;
- setup/migration is idempotent and preserves explicit user blocks;
- no secret is written to `.omg/`, generated agents, logs, policy snapshots, or receipts.

## Runtime fallback boundary

Candidate order is OMG policy. Replay safety is Medley execution truth.

Initial selection chooses the first currently eligible candidate before work starts. Runtime fallback may advance only after Medley #18 proves the failed attempt is replay-safe.

Fallback is refused after:

- visible assistant output was committed;
- a tool call was emitted without explicit replay-safety proof;
- a filesystem, process, or network side effect began;
- auth/config/policy failure requires user correction;
- the next route violates capability, harness, local-only, access scope, or billing constraints.

Each attempt resolves an independent Medley route and credential/access identity. OMG never copies credentials from the prior attempt and never implements a generic `429 → resend` loop.

## Evidence and resume

OMG evidence records only secret-free product facts:

```text
agent/profile ID
policy ID and digest
prompt profile and version
declared catalog candidates
selected catalog ID
Medley route receipt/digest
rejected candidate reasons
selection provenance
attempt and fallback admission result
capability mode and harness
```

Medley's receipt remains authoritative for provider/backend/origin/access identity.

Resume preserves:

- source child/session identity;
- source Medley route receipt/digest;
- source OMG policy/prompt-profile digest;
- prior attempt/evidence lineage.

Changing route or prompt policy during resume requires an explicit future migration/new-attempt protocol.

## CLI and UX

Read-only surfaces come first:

```text
omg agents list [--json]
omg agents explain <agent-or-profile> [--json]
omg doctor [--strict]
omg team status --json
```

`omg agents explain` shows:

- canonical profile and aliases;
- capability/tool floor;
- policy source and precedence winner;
- ordered catalog candidates;
- prompt profile and reasoning preference;
- selected route when Medley can resolve it;
- rejected/unready candidates and Medley reasons;
- exact, candidate, inherit, and resume semantics;
- qualification state and residual unknowns.

No output contains credential/header/query values, authorization URLs, account IDs, or raw prompts.

Mutation commands such as `omg agents set-route` are added only after one canonical override format can be written transactionally and round-tripped safely.

## Team integration

#69 remains the Team owner.

Team consumes the model-policy work as follows:

- a native member references an OMG agent/profile policy and Medley route receipt;
- an external member uses an OMG executor adapter and process identity;
- Team state persists route kind explicitly;
- role/capability floors apply to both kinds;
- worker replacement creates a new attempt and preserves prior evidence;
- Team completion never sets OMG `verified`.

Exact native per-agent routes may ship before the full #69 operation catalog is complete.

## Delivery sequence

1. **Policy schema and read-only catalog** — extend/reference #71, add schema/golden/drift checks, and expose `omg agents list/explain` without changing execution.
2. **Exact native catalog MVP** — bind orchestrator, executor, verifier, and explore/read-only profiles to distinct exact Medley catalog IDs using the current host surface.
3. **Ordered candidates and receipts** — consume Medley #287, preserve resume identity, and link OMG policy digest to the Medley route receipt.
4. **Model-family prompt profiles** — add qualified Claude/GPT/Gemini/generic profiles with adversarial fixtures.
5. **Replay-safe fallback and route typing** — integrate Medley #18 and migrate external `provider` terminology to versioned `executor` compatibility.

The detailed file map, hermetic/live test gates, and acceptance checklist are maintained in [OMG #131](https://github.com/ImL1s/oh-my-grok/issues/131).

## Security invariants

- OMG state and generated artifacts contain no provider credentials or endpoints;
- capability/tool floors remain runtime-enforced;
- policy cannot weaken Medley readiness/access constraints;
- exact routes do not silently inherit or cross billing/access boundaries;
- unknown model families are not silently presented as qualified;
- native and external route schemas cannot be confused;
- worktrees and Team remain integration isolation, not an execution sandbox;
- only the OMG CLI may write authoritative acceptance/`verified` state.

## Non-goals

- no second Medley model catalog;
- no provider auth/key storage in OMG;
- no runtime benchmark downloader or opaque quality score;
- no unrestricted model-supplied candidate chain;
- no implicit subscription-to-PAYG or local-to-cloud fallback;
- no replacement of #69 Team state or #71 agent taxonomy.
