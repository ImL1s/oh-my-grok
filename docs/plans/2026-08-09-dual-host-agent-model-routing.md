# Dual-host agent model routing — delivery sequence

**Status:** Non-normative implementation sequence for #131. This file is **not**
architecture. English canonical page is the only hand-maintained normative source.

**Canonical architecture:** [architecture/agent-model-routing.md](../architecture/agent-model-routing.md)

That page is the only hand-maintained normative source. Follow these section
anchors. Do not copy their tables, ownership lists, or capability catalogs here:

- [Decision](../architecture/agent-model-routing.md#decision-mandatory-baseline)
- [Support states](../architecture/agent-model-routing.md#support-states)
- [Capability negotiation outcomes](../architecture/agent-model-routing.md#capability-negotiation-outcomes)
- [Normative support matrix](../architecture/agent-model-routing.md#normative-support-matrix)
- [Ownership boundary](../architecture/agent-model-routing.md#ownership-boundary)
- [Native vs external executor](../architecture/agent-model-routing.md#native-model-route-vs-external-executor)
- [Advisory plane vs task execution](../architecture/agent-model-routing.md#advisory-plane-vs-task-execution)
- [Selection / retry / fallback / replacement](../architecture/agent-model-routing.md#initial-selection-retry-route-fallback-worker-replacement)
- [CLI / UX honesty](../architecture/agent-model-routing.md#cli--ux-surfaces-honesty)
- [Installation / compatibility](../architecture/agent-model-routing.md#installation-and-compatibility-language)
- [Non-goals](../architecture/agent-model-routing.md#non-goals)

**Tracking issues** (stable GitHub URLs only):

- https://github.com/ImL1s/oh-my-grok/issues/133 (architecture/docs)
- https://github.com/ImL1s/oh-my-grok/issues/131 (implementation)
- https://github.com/ImL1s/oh-my-grok/issues/134 (UX)
- https://github.com/ImL1s/oh-my-grok/issues/138 (consultation / advisory plane; not a Team executor)
- https://github.com/ImL1s/medley/issues/287
- https://github.com/ImL1s/medley/issues/289

Optional related:

- https://github.com/ImL1s/medley/issues/290
- https://github.com/ImL1s/medley/pull/288
- https://github.com/ImL1s/oh-my-grok/issues/71
- https://github.com/ImL1s/oh-my-grok/issues/69

## Why a sequence exists

OMG owns portable agent and profile policy. Native hosts own execution and
child-session lifecycle. Medley is an optional enhancement path and never a
hard dependency for baseline OMG.

This companion exists only to order later implementation. Read the canonical
[Decision](../architecture/agent-model-routing.md#decision-mandatory-baseline)
and [Ownership boundary](../architecture/agent-model-routing.md#ownership-boundary)
sections instead of treating this file as a second architecture.

## Delivery sequence

The five steps below match the current program order. Each item is
**sequencing only**. Treat later steps as **planned / not shipped** until
#131 lands evidence.

1. **Schema and catalog.** Dual-host policy schema and the #71 read-only
   catalog, plus host-neutral explain and list contract surfaces and docs.
   No execution change. **Planned / not shipped.**

2. **Grok Build baseline MVP.** Exact or inherit only where the stock host
   already exposes them. Install, setup, existing doctor, and agent smoke
   with Medley absent. This is the release gate for later work.
   **Planned / not shipped** as routing implementation.

3. **Medley exact-route.** Consume host-owned exact-route facts without
   duplicating route logic. Stock Grok Build remains unchanged.
   **Planned / not shipped.**

4. **Ordered candidates and receipts.** Consume Medley #287. Link policy
   and route digests. Preserve resume identity.
   **Planned / not shipped.**

5. **Profiles, fallback, Team typing, and UX.** Host-neutral prompt
   profiles, Medley #18 fallback admission, Team route typing, executor
   naming migration, and #134 host-neutral UX parity.
   **Planned / not shipped.**

## Related owners (pointers only)

- [#71](https://github.com/ImL1s/oh-my-grok/issues/71) — agent taxonomy.
- [#69](https://github.com/ImL1s/oh-my-grok/issues/69) — Team runtime.
- [#68](https://github.com/ImL1s/oh-my-grok/issues/68) — durable jobs.
- [#67](https://github.com/ImL1s/oh-my-grok/issues/67) — Antigravity adapter.
- [Medley #19](https://github.com/ImL1s/medley/issues/19) — eligibility facts.
- [Medley #18](https://github.com/ImL1s/medley/issues/18) — fallback admission.
- [Medley #287](https://github.com/ImL1s/medley/issues/287) — request/receipt extension.

Do not create a second registry beside #71, and do not bury this work in #69.

## Program done-when

- See the canonical [Drift and validation expectations](../architecture/agent-model-routing.md#drift-and-validation-expectations)
  and [Non-goals](../architecture/agent-model-routing.md#non-goals) sections.
- The #133 docs contract is separate from the #131 runtime. Docs-only work
  does not ship selection, receipts, Medley negotiation, or routing-aware
  doctor behavior.
- Do not copy the long architecture checklist into this file.

## Non-goals

- No second architecture, support matrix, or capability catalog in this plan.
- No Medley hard dependency for baseline OMG.
- No runtime, schemas, or new CLI verbs in this docs-only #133 rewrite.
- No present-tense claim that Medley negotiation or routing-aware doctor
  is shipped.
- No policy YAML, route-kind definitions, or fallback rules restated here.

Normative non-goals live on the architecture page.
