# Dual-host agent model routing

English (canonical) · Localized indexes link here rather than maintaining a second
architecture matrix — see [docs/README.md](../README.md).

**Status:** Normative architecture (documentation contract)

**Tracking:** [oh-my-grok#133](https://github.com/ImL1s/oh-my-grok/issues/133)

**Related implementation (not claimed by this page):** [oh-my-grok#131](https://github.com/ImL1s/oh-my-grok/issues/131)

**Related UX (not claimed by this page):** [oh-my-grok#134](https://github.com/ImL1s/oh-my-grok/issues/134)

**Delivery sequence (non-normative):** [plans/2026-08-09-dual-host-agent-model-routing.md](../plans/2026-08-09-dual-host-agent-model-routing.md)

**Security honesty:** verified / OMG ownership — [primary product contract](../security-model.md#primary-product-contract) · [acceptance policy (summary)](../security-model.md#acceptance-policy-summary); Team isolation is not a sandbox — [team integration isolation](../security-model.md#team-integration-isolation); host probe notes: [host-compat.md](../host-compat.md)

This page is the **single canonical architecture** for how oh-my-grok (OMG)
agent/model **policy** relates to native hosts. It does **not** implement route
selection, Team execution, or credentials. Runtime behavior lands under #131;
host-neutral CLI/UX projection under #134.

## Decision (mandatory baseline)

OMG is a **Grok Build–first** product with an **optional** Medley enhancement path:

| Host tier | Role | Dependency on OMG install |
|-----------|------|---------------------------|
| **Original Grok Build** (stock) | **Required, first-class baseline** | Always; ordinary agents, skills, workflows, evidence, and acceptance work with Medley **absent** |
| **Medley** | **Optional enhanced host** (never a hard dependency) | Not required for baseline OMG; absence must not disable ordinary OMG operation |

Rules:

- Stock Grok Build is **supported**, not a legacy or degraded mode.
- Absence of Medley must **not** disable ordinary OMG operation.
- Installing OMG does **not** install Medley; installing Medley is **not**
  required for standard OMG agents/workflows.
- External Team CLI executors (codex, agy, cursor, gemini, …) are **separate**
  optional dependencies and are not Medley API/access routes.
- Unknown hosts negotiate by **capabilities**, not binary-name guessing.
  Current shipped `omg doctor` reports **current host/session capabilities**
  only. Medley-side negotiation and routing-aware doctor fields are
  **planned / contract-only** under #131, **not shipped**.
- This document does **not** claim affiliation between OMG, Medley, or xAI.

## Mandatory terminology

Use these terms consistently. Do **not** overload `provider` to mean API
backend, model catalog route, credential owner, billing product, **and**
external CLI executable at once.

| Term | Meaning |
|------|---------|
| **host runtime** | Process that runs native child sessions and exposes host APIs |
| **host capability** | Versioned, machine-negotiated feature the host advertises or probes |
| **OMG agent/profile policy** | Portable OMG intent: role, preference order, prompt family, floors |
| **native model route** | Host-executed child session/model selection (`kind: native`) |
| **original Grok Build baseline** | Tier A — required stock host support |
| **Medley extension** | Tier B — optional enhanced native-route contract |
| **external executor route** | OMG-launched/supervised CLI worker (`kind: external_executor`) |
| **requested policy** | What OMG asked for (exact, inherit, candidates, floors) |
| **effective route** | What actually ran after host negotiation |
| **route receipt** | Secret-free host-owned proof of selected route/attempt facts |
| **selection/fallback attempt** | One bounded try: select, retry, fall back, or replace |

## Support states

Two layers of vocabulary — do not collapse them.

### Host-tier roles

| State | Meaning |
|-------|---------|
| **baseline** | Required first-class support on original Grok Build (and any compatible baseline host) |
| **optional extension** | Medley (or equivalent) enhancement; missing extension leaves baseline usable |

### Capability negotiation outcomes

Hosts and OMG must negotiate **versioned capabilities**, not only names like
`medley` vs `grok`. Conceptual capability ids (final identifiers must match
implementation, generated schemas, `omg doctor`, and tests when #131 lands):

```text
host.native-agent.v1
host.native-exact-model.v1
host.native-inherit-model.v1
medley.native-ordered-candidates.v1
medley.native-route-receipt.v1
medley.native-model-family-metadata.v1
medley.native-replay-safe-fallback.v1
```

(Issue #133 also lists the conceptual family `native.exact-model.v1` /
`native.ordered-candidates.v1` / … — the `host.*` vs `medley.*` split above is
the dual-host disambiguation on **this** page so baseline exact/inherit is
never mistaken for a Medley-only feature. The planning file is sequence-only
and must not carry a second copy of these identifiers.)

| Outcome | Meaning | Baseline OMG usable? |
|---------|---------|----------------------|
| **supported** | Capability available and qualified; authorized to use | Yes |
| **unsupported** | Host does not expose it | Yes (enhanced path not claimed) |
| **unavailable** | Host claims it but runtime evidence/config is missing | Yes for baseline; enhanced path blocked |
| **incompatible** | Version/schema cannot be safely consumed | Yes for baseline; do not force-parse |
| **unknown** | Do not infer support | Yes for baseline; treat as not authorized |

Only **supported** authorizes use of that capability. Unsupported enhanced
fields must **not** be silently interpreted as baseline success. On stock
Grok Build, a Medley-only **capability outcome** is **unsupported**;
**route-specific facts** (receipts, ordered candidates, access/readiness)
are **unavailable**. Never interchange **unavailable** and **unsupported**
with a slash. Neither is installation **failed**.

## Normative support matrix

| Capability | Original Grok Build | Medley |
|------------|--------------------:|-------:|
| OMG agents, skills, workflows, acceptance | **Required** | **Required** |
| Native child sessions | Host baseline | Host baseline |
| Exact/inherit per-agent model binding | Supported when exposed by host | Supported |
| Ordered native catalog candidates | Not assumed | Via Medley native-route contract |
| Provider/access/readiness truth | Host-owned, limited to exposed facts | Medley-owned typed route |
| Secret-free route receipt | Not assumed | Medley extension |
| Replay-safe cross-route runtime fallback | Not claimed | Only through Medley admission contract |
| External CLI Team executors | OMG-owned | OMG-owned |

## Ownership boundary

### Original Grok Build / compatible host owns

- its model inventory and native child-session execution;
- the exact/inherit model semantics it exposes;
- capability/tool/session behavior on its public surface;
- host persistence, resume, cancellation, and native usage facts.

### Medley additionally owns (when extension is present)

- catalog-ID route identity;
- provider/backend/origin/readiness/access-profile truth;
- ordered candidate eligibility and typed rejection reasons;
- route receipts and attempt lifecycle facts;
- replay-safety admission for native cross-route fallback.

### OMG owns on every host

- agent/profile taxonomy (see [#71](https://github.com/ImL1s/oh-my-grok/issues/71));
- role/category semantics and capability floors that **never widen** host permissions;
- model preference order and prompt-family policy;
- workflow selection and explanation;
- external executor routing and Team topology ([#69](https://github.com/ImL1s/oh-my-grok/issues/69));
- acceptance/evidence policy and sole authority over `verified`.

### OMG never owns

- API keys, OAuth tokens, raw headers, endpoint secrets, or credential values;
- a second copy of a host model catalog;
- guessed provider readiness, billing, or access product truth;
- unsafe replay after visible output, tool calls, or side effects.

## Native model route vs external executor

Two **non-overlapping** route kinds:

```text
native
  host executes a child session / model route

external_executor
  OMG launches and supervises a CLI (e.g. codex, agy, cursor, gemini)
```

Rules:

- a Medley API **provider** is **not** an OMG external executor;
- an external CLI executable is **not** a Medley credential/access route;
- Team JSON/status and evidence must label **route kind**;
- legacy external `provider` naming remains readable through a documented
  migration path toward `executor` (schema-versioned; implementation #131/Team);
- native catalog identifiers are **not** passed into external argv builders
  without an explicit adapter mapping.

Integration isolation (worktrees, seal, integrate) is **not** an execution
sandbox — see [team integration isolation](../security-model.md#team-integration-isolation).

## Initial selection, retry, route fallback, worker replacement

Keep these operations **distinct** (never conflate):

1. **Initial candidate selection** — choose an effective route **before** execution;
2. **Retry within one route** — same route identity; host-native or policy-bounded retry;
3. **Fallback to another native route** — new route identity; requires capability +
   replay-safety admission where claimed;
4. **External worker replacement** — new external executor attempt; preserves prior
   evidence; does not set `verified`.

Automatic **native** route fallback is **prohibited** after:

- visible model output is committed;
- a tool call is emitted unless replay safety is **explicitly** proven;
- filesystem, process, or network side effects begin;
- auth/config/policy errors require operator correction;
- the next candidate would violate capability, local-only, access, or billing constraints.

**No documentation may imply that HTTP `429` alone authorizes resending the task
through another provider.** Stock Grok Build receives **no** cross-route
replay-safe fallback claim unless it exposes an equivalent versioned admission
contract.

## Policy examples (fictional identifiers only)

Examples use **clearly fictional** catalog/model ids. They contain **no** real
credentials, account IDs, private origins, or entitlement claims.

### Original Grok Build baseline

- exact model when the host advertises a safe exact-model contract, e.g.
  `model: grok-example-1` (fictional);
- explicit `inherit` for parent model;
- host-independent OMG agent/profile with **no** provider credentials in OMG state;
- diagnostics: Medley-only **capability outcomes** shown as **unsupported**;
  **route-specific facts** (receipts, ordered candidates, access/readiness)
  shown as **unavailable**; never fabricated success. Neither is installation
  failed.

### Medley extension

- ordered catalog candidates, e.g. `catalog: review-primary-example` then
  `catalog: review-fallback-example` (fictional);
- model-family prompt profile (`gpt-family` / `gemini-family` / `generic`);
- selected route plus rejected-candidate reasons from host facts;
- route receipt/digest **reference** (secret-free);
- **refused** unsafe fallback after output/tool/side-effect observation.

## CLI / UX surfaces (honesty)

OMG does **not** own a separate stock-Grok TUI. Portable surfaces are CLI/JSON
plus host-native projections.

| Surface | Status relative to this architecture page |
|---------|-------------------------------------------|
| `omg doctor [--strict]` | **Shipped** — host/compat/probe honesty for **current host/session capabilities**; does **not** negotiate Medley/model-routing enhancements today; enhanced fields must not false-green |
| `omg team status [--json]` | **Shipped** — Team status; route kind labeling is the dual-host contract target |
| `omg agents list [--json]` | **Contract** — host-neutral agent/policy catalog; delivery under #131 / #134 |
| `omg agents explain <id> [--json]` | **Contract** — policy vs effective route/receipt; delivery under #131 / #134 |

Human and JSON views of the same facts must agree when those commands ship.
On original Grok Build, explain/list must show which enhanced facts are
unavailable. On Medley, they must separate **requested policy** from **effective
route** and receipt provenance. Opening diagnostics must not perform paid
provider probes or print secrets.

## Installation and compatibility language

- Original Grok Build remains the **normal supported host**.
- Medley is an **optional compatible host**. Enhanced native routing and
  Medley-side negotiation are **planned (#131)**, not shipped.
- OMG install ≠ Medley install; Medley is not required for baseline OMG.
- External Team executors remain separate optional dependencies.
- Current `omg doctor` reports **current host/session capabilities** only; it
  does not negotiate Medley routing availability. Use capability negotiation,
  not branding guesses.

Avoid wording that makes Medley look upstream, official, or required by Grok Build.

## Cross-repository references

Use stable GitHub URLs for other repos; relative links inside this repository.

| Topic | Link |
|-------|------|
| OMG architecture/docs (this issue) | [#133](https://github.com/ImL1s/oh-my-grok/issues/133) |
| OMG implementation plan | [#131](https://github.com/ImL1s/oh-my-grok/issues/131) |
| OMG dual-host UX | [#134](https://github.com/ImL1s/oh-my-grok/issues/134) |
| OMG agent taxonomy | [#71](https://github.com/ImL1s/oh-my-grok/issues/71) |
| OMG Team runtime | [#69](https://github.com/ImL1s/oh-my-grok/issues/69) |
| Medley optional host-contract plan | [ImL1s/medley#287](https://github.com/ImL1s/medley/issues/287) |
| Medley architecture/docs counterpart | [ImL1s/medley#289](https://github.com/ImL1s/medley/issues/289) |
| Medley TUI counterpart (related, not claimed here) | [ImL1s/medley#290](https://github.com/ImL1s/medley/issues/290) |
| OMG security model | [security-model.md](../security-model.md) |
| OMG dual-host planning doc | [plans/2026-08-09-dual-host-agent-model-routing.md](../plans/2026-08-09-dual-host-agent-model-routing.md) |

Reciprocal links on the Medley side are owned by those Medley issues/PRs; this
repository only links to **public** counterparts and does not invent unpublished
paths.

## Drift and validation expectations

CI should eventually prove (and #133 acceptance requires):

- support matrix / capability vocabulary stay aligned with the versioned registry
  when it lands (#131);
- documented **shipped** CLI names match registered commands;
- route-kind names (`native`, `external_executor`) match schemas when present;
- links to Medley #287 / #289 and OMG #131 / #133 remain present;
- maintained indexes and locales **point at** this page rather than forking the
  matrix into hand-maintained duplicates;
- no secret/header/query/account sentinel in examples;
- **no** statement that Medley is required for baseline OMG operation.

Prefer generated snippets or managed markers for machine-owned tables once the
capability registry exists. Until then, this English page is the single
hand-maintained normative source.

## Non-goals

- no Medley provider setup guide duplication;
- no promise that stock Grok Build exposes Medley receipts or ordered candidates;
- no runtime benchmark/ranking claims;
- no claim that worktree/Team isolation is an execution sandbox;
- no implication of affiliation between OMG, Medley, or xAI;
- no runtime implementation of #131 or #134 on this documentation page alone.
