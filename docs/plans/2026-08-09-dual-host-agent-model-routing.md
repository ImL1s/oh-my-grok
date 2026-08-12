# Dual-host agent model routing for Grok Build and Medley

**Status:** Proposed (implementation planning)

**Date:** 2026-08-09

**Target branch:** `main`

**Normative architecture (canonical):** [`docs/architecture/agent-model-routing.md`](../architecture/agent-model-routing.md) — **read that page for the support matrix, ownership boundary, route kinds, and fallback safety.** This plan remains the delivery-sequence / program-definition companion and must not diverge on baseline-vs-Medley dependency claims.

**Tracking issue:** [ImL1s/oh-my-grok#131](https://github.com/ImL1s/oh-my-grok/issues/131)

**Architecture/docs issue:** [ImL1s/oh-my-grok#133](https://github.com/ImL1s/oh-my-grok/issues/133)

**Dual-host UX:** [ImL1s/oh-my-grok#134](https://github.com/ImL1s/oh-my-grok/issues/134)

**Optional Medley host contract:** [ImL1s/medley#287](https://github.com/ImL1s/medley/issues/287)

**Medley architecture/docs:** [ImL1s/medley#289](https://github.com/ImL1s/medley/issues/289)

**Medley TUI/UX:** [ImL1s/medley#290](https://github.com/ImL1s/medley/issues/290)

**Counterpart plan PR:** [ImL1s/medley#288](https://github.com/ImL1s/medley/pull/288)

## Decision

oh-my-grok owns one portable, versioned **agent/profile model-policy layer** that works across two native host tiers:

```text
Tier A — original Grok Build
  required, first-class baseline
  agents/workflows remain useful without Medley installed

Tier B — Medley
  optional enhanced host
  catalog-aware ordered candidates, effective-route receipts,
  access/readiness facts, and replay-safe fallback admission
```

OMG owns agent identity, role/category semantics, model preference order, model-family prompt policy, workflow selection, external executor topology, evidence, and acceptance. The active native host owns model execution and child-session lifecycle. Medley additionally owns its multi-provider route, credential/access, readiness, receipt, and replay-safety truth.

Medley is never an undeclared OMG dependency. Original Grok Build is not a legacy or degraded mode.

## Why the policy belongs in OMG

OMG already defines:

- orchestrator, executor, critic, verifier, debugger, designer, writer, and reviewer behavior;
- role-specific capability/tool floors and depth-1 leaf rules;
- ULW, Ralph, Ralplan, Autopilot, Team, evidence, and acceptance semantics;
- external CLI worker adapters and process topology.

Choosing which model intent and prompt family fits an OMG role is therefore product policy. Putting OMG role names or preference order in Medley would bind the host runtime to one orchestration product.

Conversely, OMG must not recreate a host model catalog, provider endpoint/auth logic, readiness, access-profile identity, child sampling client, or native resume implementation.

## Related ownership

- [#71](https://github.com/ImL1s/oh-my-grok/issues/71) remains the single canonical agent taxonomy, alias/tier, and projection source.
- [#69](https://github.com/ImL1s/oh-my-grok/issues/69) remains the Team task/mailbox/member/worktree/recovery owner.
- [#68](https://github.com/ImL1s/oh-my-grok/issues/68) remains the durable jobs owner.
- [#67](https://github.com/ImL1s/oh-my-grok/issues/67) remains the Antigravity adapter owner.
- [Medley #19](https://github.com/ImL1s/medley/issues/19) owns generic capability-aware candidate eligibility and rejection facts.
- [Medley #18](https://github.com/ImL1s/medley/issues/18) owns replay-safe native cross-route fallback admission.
- [Medley #287](https://github.com/ImL1s/medley/issues/287) owns the optional generic request/receipt extension.

Do not create a second agent registry beside #71, and do not bury this work inside #69.

## Responsibility boundary

### OMG owns on every host

- canonical agent/profile/category/tier identity;
- baseline exact/inherit policy and optional enhanced candidate policy;
- model preference order, reasoning/effort preference, and attempt budget;
- model-family prompt profiles;
- user/project/per-run policy precedence;
- workflow-specific policy selection;
- policy provenance, digest, and user-facing explanation;
- capability/tool floors that may narrow but never widen host permissions;
- external CLI executor adapters, PTY/process identity, and Team topology;
- evidence and the sole authority over OMG `verified` state.

### Original Grok Build or another compatible native host owns

- its actual model inventory and exact/inherit semantics;
- native child-session creation, persistence, cancellation, resume, and usage;
- native tool/session behavior exposed through its public contract;
- model execution and lifecycle truth.

### Medley additionally owns when the extension is supported

- catalog-ID and duplicate-wire-slug route identity;
- final provider/backend/origin/readiness/access-profile facts;
- ordered candidate eligibility and typed rejection reasons;
- immutable route receipts and attempt lifecycle observations;
- replay-safe cross-route fallback admission;
- canonical route/usage projections.

### OMG never owns

- API keys, OAuth tokens, raw headers, query values, endpoint secrets, or credential material;
- a second copy of the host model catalog;
- guessed provider readiness, access product, entitlement, or billing identity;
- unsafe replay after visible output, tool calls, or side effects;
- implicit subscription-to-PAYG, local-to-cloud, gateway, or cross-credential fallback.

## Host capability negotiation

Do not branch only on executable name, state directory, branding, or a loose version string. Use one versioned machine-readable capability profile.

Conceptual capabilities:

```text
host.native-agent.v1
host.native-exact-model.v1
host.native-inherit-model.v1
medley.native-ordered-candidates.v1
medley.native-route-receipt.v1
medley.native-model-family-metadata.v1
medley.native-replay-safe-fallback.v1
```

Final identifiers must be single-source across implementation, generated schemas, capability lock, doctor, JSON, docs, and tests. The `host.*` versus `medley.*` distinction prevents exact/inherit baseline support from being mistaken for a Medley-only capability.

Required states:

```text
supported
unsupported
unavailable
incompatible
unknown
```

Only `supported` authorizes use. `unsupported` keeps Tier A usable; `unavailable` means the capability exists but required config/evidence is missing; `incompatible` means the schema cannot be consumed safely; `unknown` never counts as support.

Missing Medley-only capabilities on stock Grok Build are not installation failures.

## Canonical policy source

Extend or reference #71's schema-versioned manifest. One portable role should normally produce host-specific projections rather than separate Grok Build and Medley taxonomies.

Conceptual shape:

```yaml
schemaVersion: 1
agents:
  omg-verifier:
    capabilityMode: read-only
    modelPolicy:
      policyId: verifier.default
      baseline:
        mode: inherit
      extensions:
        medley.native-ordered-candidates.v1:
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

Rules:

- the baseline is separately declared as exact, inherit, or `requiresCapability`;
- unsupported enhanced policy is never flattened to its first Medley catalog ID;
- Medley-only fields are stripped from stock Grok Build projections;
- generated prompts contain no credentials, endpoints, machine paths, or mutable readiness snapshots;
- projection goldens prove that host-specific fields do not leak across tiers.

## Policy resolution precedence

OMG resolves policy intent before the host resolves execution:

1. explicit operator/per-run exact override;
2. project-scoped OMG agent/profile override;
3. user-scoped OMG agent/profile override;
4. canonical OMG agent/profile policy;
5. parent inheritance only when explicitly declared.

### Tier A — original Grok Build

- exact is used only when the host advertises a safe exact-model contract;
- inherit explicitly uses the parent model;
- an enhanced policy uses only its separately declared baseline behavior;
- `requiresCapability` fails with an actionable explanation;
- unavailable provider/backend/access facts are shown as unavailable, never fabricated.

### Tier B — Medley

- exact catalog route means exact-or-error;
- ordered candidates select the first fully eligible route in declared order;
- inherit uses the parent route only because it was explicit;
- resume pins the source route/model and receipt lineage;
- capability, harness, local-only, readiness, access, and billing constraints are never relaxed by OMG.

A missing verifier route must not silently become the orchestrator or parent model on either tier.

## Model-family prompt profiles

OMG owns prompt adaptation because it is role/product behavior, not provider transport.

Initial explicit profiles:

```text
claude-family
gpt-family
gemini-family
generic
```

Rules:

- use explicit mapping or qualified host metadata, not provider-name guessing;
- gateways serving the same qualified family may use the same profile;
- unknown family uses `generic` or fails when qualification is mandatory;
- prompt profile never widens tools, capability, harness, access, or credentials;
- profile and version participate in the OMG policy digest;
- a prompt does not make an intrinsically incompatible model supported.

## Native route and external executor are different types

```text
NativeAgentRoute
  kind: native
  host capability/model-policy reference

ExternalExecutorRoute
  kind: external_executor
  executor: grok | codex | agy | cursor | gemini
  optional executor-specific model flag
  PTY/startup/process identity
```

Compatibility requirements:

- preserve legacy external `--routing` behavior;
- migrate/alias the external `provider` key to `executor` through a versioned schema;
- native host model/catalog references are not passed to external argv builders without adapter-owned mapping;
- external executor names are never interpreted as Medley provider/access identities;
- Team JSON, status, and evidence label route kind explicitly;
- both native host tiers share the same external executor plane.

## Initial selection, retry, fallback, resume, and replacement

Keep five operations distinct:

1. OMG policy resolution;
2. deterministic initial native model selection;
3. retry within one effective route;
4. fallback to another native route;
5. external worker replacement.

Stock Grok Build receives no cross-route replay-safe fallback claim unless it exposes an equivalent versioned admission contract. Host-native retries remain host behavior.

On Medley, cross-route fallback is refused after:

- visible assistant output is committed;
- a tool call is emitted unless replay safety is explicitly proven;
- filesystem, process, or network side effects begin;
- auth/config/policy failures require operator correction;
- another candidate violates capability, harness, local-only, access, or billing constraints.

Every attempt resolves its own route and credential/access identity. OMG never copies credential material and never implements a generic `429 -> resend through another provider` loop.

## Evidence and resume

OMG records only secret-free product facts:

```text
host compatibility tier and observed capability versions
agent/profile ID
policy ID/digest and precedence winner
baseline versus extension path
prompt profile/version
declared native model references/candidates
selected model/catalog ID when known
host route receipt/digest when available
rejected candidates/reasons when available
attempt and fallback/admission result
capability mode and harness
```

On stock Grok Build, OMG does not invent provider/backend/access facts. On Medley, its route receipt is authoritative and OMG does not reconstruct it independently.

Resume preserves source session/model identity, source policy digest, source receipt when available, and attempt lineage. Changing model or prompt policy during resume requires an explicit new-attempt/migration protocol.

## CLI and UI/UX contract

OMG does not own a separate terminal renderer on stock Grok Build. It provides portable typed policy data, generated agent metadata, and CLI/JSON surfaces; the native host renders its own TUI.

Required host-neutral surfaces:

```text
omg agents list [--json]
omg agents explain <agent-or-profile> [--json]
omg doctor [--strict]
omg team status [--json]
```

`omg agents explain` shows:

- canonical identity, aliases, category, and tier;
- capability/tool floor;
- policy source and precedence winner;
- Tier A exact/inherit/requires-capability behavior;
- capability-gated Medley policy;
- prompt profile/reasoning preference;
- selected model/route only when host facts support it;
- supported/unsupported/unavailable/incompatible/unknown facts honestly;
- exact, inherit, candidate, resume, and attempt semantics.

### Stock Grok Build UX

- generated OMG agents remain visible through the host's existing agent UI where that public surface is available;
- enhanced route fields appear as unavailable/unsupported, not blank success;
- CLI remains the authoritative detailed policy/explanation surface;
- no Medley binary, path, config, or state is required.

### Medley enhanced TUI

- Medley may project the canonical OMG policy and route receipt into its `/agents` modal and subagent lifecycle views;
- the UI consumes typed policy/route snapshots rather than parsing OMG prose;
- compact rows preserve agent identity, selected model/route state, and readiness before optional detail;
- expanded details show policy provenance, candidate order, rejection reasons, prompt profile, capability floor, route/access summary, and receipt/attempt lineage;
- opening the modal performs no inference or paid provider probe;
- actions are generation-bound and revalidated before mutation;
- narrow/normal/wide, no-color, keyboard-only, mouse, resize, and large-catalog behavior are tested;
- no secret value, raw header/query, account identifier, prompt, or full response appears.

[#134](https://github.com/ImL1s/oh-my-grok/issues/134) owns the host-neutral policy/CLI projection; [Medley #290](https://github.com/ImL1s/medley/issues/290) owns the native `/agents` and lifecycle TUI projection.

## Team integration

#69 remains the Team owner.

- a native member references OMG policy and only the route facts available at its host tier;
- stock Grok Build state records baseline model identity without Medley-only claims;
- Medley state may reference an immutable route receipt;
- an external member uses an OMG executor adapter and process identity;
- replacement creates a new attempt and preserves prior evidence;
- Team completion never sets OMG `verified`.

## Delivery sequence

1. **Dual-host schema and read-only policy catalog** — capability registry, #71 manifest integration, `agents list/explain`, docs, no execution change.
2. **Original Grok Build baseline MVP** — exact/inherit where supported, stock-host install/setup/doctor/agent smoke with Medley absent. This is a release gate for all later work.
3. **Medley exact-route enhancement** — consume existing host facts without duplicating route logic; Tier A remains unchanged.
4. **Medley ordered candidates and receipts** — consume #287, link policy and route digests, preserve resume identity.
5. **Prompt profiles, replay-safe fallback, Team route typing, and UX completion** — host-neutral prompt fixtures, Medley #18 integration, external `executor` migration, TUI/CLI/JSON parity.

## Program definition of done

### Core routing complete

- stock Grok Build works with Medley absent: install, setup, doctor, agents, core workflows, evidence, acceptance, and bounded live native-agent smoke;
- at least orchestrator, executor, verifier, and explore/read-only profiles have deterministic Tier A policies;
- Medley exact and ordered candidate policies consume host-owned route truth;
- exact selections never silently inherit or switch route;
- resume preserves model/policy/receipt provenance;
- native and external route kinds are mechanically distinct;
- CLI/JSON explain the same policy and host capability state;
- no secret or unsupported route claim is emitted.

### Full routing program complete

- Medley route receipts and attempt lineage are persisted and visible;
- replay-safe fallback is proven with scripted providers and refused after visible output/tool/side effect;
- OMG and Medley TUI/CLI/JSON projections consume shared typed snapshots;
- narrow/no-color/keyboard/mouse/resize/large-catalog UX gates pass;
- Team and durable jobs preserve route kind and attempt history;
- English, zh-TW, and zh architecture/support docs are synchronized and drift-tested;
- no unresolved P0/P1 security, credential-isolation, exact-route, resume-identity, or unsafe-replay finding remains in this scope;
- all required hermetic CI gates pass, plus bounded stock Grok Build and Medley live evidence tied to exact SHAs.

## Non-goals

- no Medley requirement for baseline OMG operation;
- no provider auth/key storage in OMG;
- no second host model catalog;
- no runtime model-quality benchmark downloader;
- no silent prompt-family guessing presented as qualification;
- no unbounded model-generated candidate chain;
- no implicit cross-billing or local-to-cloud fallback;
- no execution-sandbox claim for worktrees/Team;
- no replacement of #69 Team state or #71 taxonomy with a parallel runtime.
