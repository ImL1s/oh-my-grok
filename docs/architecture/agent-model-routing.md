# Dual-host agent model routing

English (canonical) · [简体中文（投影）](./agent-model-routing.zh.md) · [繁體中文（投影）](./agent-model-routing.zh-TW.md)

Localized indexes and locale pages are projections — do not fork the
architecture matrix. See [docs/README.md](../README.md).

**Status:** Normative architecture (documentation contract)

**Tracking:** [oh-my-grok#133](https://github.com/ImL1s/oh-my-grok/issues/133)

**Related implementation (not claimed by this page):** [oh-my-grok#131](https://github.com/ImL1s/oh-my-grok/issues/131)

**Related UX (not claimed by this page):** [oh-my-grok#134](https://github.com/ImL1s/oh-my-grok/issues/134)

**Related consultation plane (docs only; not a Team executor):** [oh-my-grok#138](https://github.com/ImL1s/oh-my-grok/issues/138)

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
  Hermetic evidence (Medley absent from env/config/PYTHONPATH; no `medley`
  package/import; fake/temp host/home; no credentials or live network) lives
  in [`tests/test_stock_host_medley_absent.py`](../../tests/test_stock_host_medley_absent.py).
  It exercises setup / package projection, current `omg doctor`, ordinary
  agent/profile discovery, and an ordinary workflow parser/inventory surface.
  It does **not** implement routing.
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
| **native model route** | Host-executed child session/model selection (`kind: native`) — a **policy route class**, not a default Team status `--json` field |
| **original Grok Build baseline** | Tier A — required stock host support |
| **Medley extension** | Tier B — optional enhanced native-route contract |
| **external executor route** | OMG-launched/supervised CLI worker (`kind: external_executor`) — a **policy route class**, not a default Team status `--json` field |
| **requested policy** | What OMG asked for (exact, inherit, candidates, floors) |
| **effective route** | What actually ran after host negotiation |
| **route receipt** | Secret-free host-owned proof of selected route/attempt facts |
| **selection/fallback attempt** | One bounded try: select, retry, fall back, or replace |

`kind: native` and `kind: external_executor` name **policy route classes** on
this page (the #131 documentation contract). They are **not** keys on the
default locked `omg team status --json` payload.

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

This table is the single hand-maintained normative matrix. Tests bind it to
[`tests/fixtures/docs/normative_support_matrix_v1.json`](../../tests/fixtures/docs/normative_support_matrix_v1.json)
until the #131 capability registry replaces this docs contract. Do not fork it
into locale pages.

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
- the advisory broker (`omg ask`) and its non-authoritative artifacts ([#138](https://github.com/ImL1s/oh-my-grok/issues/138)); they never set `verified`;
- acceptance/evidence policy and sole authority over `verified`.

### OMG never owns

- API keys, OAuth tokens, raw headers, endpoint secrets, or credential values;
- a second copy of a host model catalog;
- guessed provider readiness, billing, or access product truth;
- unsafe replay after visible output, tool calls, or side effects.

## Native model route vs external executor

Two **non-overlapping** **policy** route kinds (`native` and `external_executor`):

```text
native
  host executes a child session / model route

external_executor
  OMG launches and supervises a CLI (e.g. codex, agy, cursor, gemini)
```

`external_executor` is the **task_execution** policy class for OMG-launched
CLI **workers**. It does **not** include the advisory broker (`omg ask`).
See [Advisory plane vs task execution](#advisory-plane-vs-task-execution).

These are documentation-contract **policy route classes**. They are not default
Team status `--json` fields, and they do **not** invent a shipped native
execution runtime.

### Policy class vs shipped Team Presentation `route.kind`

| Policy route class (this page / #131 contract) | Shipped Team Presentation `route.kind` | Default `omg team status --json` |
|---|---|---|
| `native` | **not shipped as this string**. Closest exported value is `native_host_receipt` (optional receipt passthrough only; not native execution) | no route field |
| `external_executor` | `external_executor` (exported `ROUTE_KIND_EXTERNAL`) | no route field |
| (legacy / unlabeled) | `unknown` | no route field |

Rules:

- Do **not** treat policy `native` as equal to Presentation `native_host_receipt`.
- Do **not** invent `external_cli_executor` or `execution_kind`. There is **no**
  shipped `route.kind = "native"`.
- Default locked status JSON does **not** label route kind. Route labeling is
  Presentation State V1 (`omg team status --presentation`) and remains a
  dual-host **contract target** for evidence/status completeness.
- a Medley API **provider** is **not** an OMG external executor;
- an advisory `external_cli` (`omg ask`) is **not** an OMG external executor;
- an external CLI executable is **not** a Medley credential/access route;
- legacy `provider` naming is readable via the shipped Presentation migration
  rules below (not a #131 runtime);
- native catalog identifiers are **not** passed into external argv builders
  without an explicit adapter mapping.

Integration isolation (worktrees, seal, integrate) is **not** an execution
sandbox — see [team integration isolation](../security-model.md#team-integration-isolation).

### Legacy provider fields and route schema v1

This is **shipped** Team Presentation State V1 behavior (`omg_cli.team.presentation`),
not a future #131 registry.

- Route objects use **schema v1**. The exported constant is `ROUTE_SCHEMA = 1`.
  The discriminator is `route.kind`.
- Shipped `route.kind` values are only the exported constants:
  `external_executor` (`ROUTE_KIND_EXTERNAL`) | `unknown` (`ROUTE_KIND_UNKNOWN`)
  | `native_host_receipt` (`ROUTE_KIND_NATIVE_RECEIPT`).
- New start/scale **writers** stamp an explicit route (`stamp_route_on_task` /
  `build_external_route`) when they can. They do **not** infer `route.kind`
  from `provider` text.
- For compatibility, existing descriptor fields `executor` and `provider` may
  be **dual-carried** on the same route object. `provider` remains readable;
  it is not a kind.
- **Readers** that see a persisted task **without** a `route` object project
  `unknown_route()`: `kind` is `unknown`, and `executor` / `provider` / `role`
  / `posture` are `None`. Legacy unstamped rows stay unknown.
- Readers **never infer** `native` vs `external_executor` from `provider`
  text. `validate_route_descriptor` requires an explicit `kind` among the
  three shipped values and `route.schema == 1`.
- Readers **preserve** `unknown`. They do not upgrade it to external or native.
- Removing or replacing this schema requires a **separate versioned migration**.
  Until that migration exists, `route.schema` must remain `1`;
  any other schema is refused.

Shipped `unknown_route()` shape (fictional ids only; Python `None` as JSON `null`):

```json
{ "schema": 1, "kind": "unknown", "executor": null, "provider": null, "role": null, "posture": null }
```

Stamped `build_external_route` dual-carry shape (same keys as the later
Presentation example under [CLI / UX surfaces](#cli--ux-surfaces-honesty)):

```json
{ "schema": 1, "kind": "external_executor", "executor": "fixture", "provider": "fake", "role": "executor", "posture": "read-write" }
```

## Advisory plane vs task execution

Policy route classes `native` and `external_executor` classify **task
execution**. They do **not** classify every OMG-supervised CLI. Shipped
`omg ask` is an **advisory** broker: it is **not** an external Team
executor and **not** a native host route.

Issue [#138](https://github.com/ImL1s/oh-my-grok/issues/138) requires three
**orthogonal** dimensions. Combine them; do not collapse them into one
`external_executor` label.

| Dimension | Values | Meaning |
|-----------|--------|---------|
| `runtime_kind` | `native_host` \| `external_cli` | Who runs the process: the native host session, or an OMG-owned external CLI |
| `purpose` | `advisory` \| `task_execution` | Whether the process may change product state / implement work, or only produce non-authoritative advice |
| `lifecycle` | `foreground` \| `background_job` \| `team_member` | How the process is awaited: synchronous broker, durable job, or Team pane/member |

### Shipped advisory broker

Shipped `omg ask` is:

- `runtime_kind = external_cli`
- `purpose = advisory`
- `lifecycle = foreground` by default
- `lifecycle = background_job` when invoked as `omg ask <provider> --background` (durable-job seam; background admits `fake` and `agy` only)

Rules:

- `external_cli` **plus** `advisory` is **not** an external Team executor.
  Do **not** stamp Presentation `route.kind = external_executor` for ask.
- `omg ask` artifacts (`.omg/artifacts/ask-*.md` and sidecar `.meta.json`)
  are **advisory and non-authoritative**. They never write acceptance
  results and never set `verified` / `passes`.
- Durable **consultation / council** artifacts, when present under
  `.omg/artifacts/`, stay on the same advisory plane. This page does
  **not** claim a shipped council runtime or a Team-executor council.
- A Medley API / catalog / access **route** remains a host-owned native
  fact. It is not `omg ask`, not a Team executor, and not an advisory
  artifact.
- `team_member` is Team pane lifecycle (`purpose = task_execution`).
  Advisors are not Team members.

Foreground ask writes the artifact and returns. Background ask creates a
durable job and returns `job_id`; the job still has `purpose = advisory`
and **never** grants `verified`.

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

### Presentation ownership and accessibility

Routing / backend completion is **not** UI / TUI completion. A policy or
route contract does **not** ship a host renderer.

Presentation ownership (documentation contract; **not** a shipped TUI runtime):

- **Stock original Grok Build** presents through the host's **supported
  declarative Agents / Tasks / child-session surfaces**. OMG does **not**
  own an arbitrary stock-host renderer or a new stock Grok Build panel.
- **OMG** owns policy / Team / external-executor **projection** under
  [#134](https://github.com/ImL1s/oh-my-grok/issues/134). That work is
  **planned / contract-only**; this page does not ship `omg agents` (not
  registered / not runnable today) or a host-neutral TUI.
- **Medley** owns route-aware Agents / lifecycle TUI under
  [ImL1s/medley#290](https://github.com/ImL1s/medley/issues/290) and
  provider / route / statusline projection under
  [ImL1s/medley#207](https://github.com/ImL1s/medley/issues/207). Those
  Medley surfaces are **not** OMG runtime and are **not** claimed shipped
  here.

Enhanced fields stay **capability-gated**. On stock Grok Build a
Medley-only capability outcome is **unsupported**; route-specific facts
are **unavailable**. The stock host must report those honest states
rather than fabricating a panel.

Accessibility **contract targets** (not shipped runtime):

- narrow-width layouts remain readable;
- no-color / `NO_COLOR` environments remain usable;
- human and JSON views of the same facts stay equivalent.

No page may imply OMG can arbitrarily add a new stock Grok Build TUI
panel.

| Surface | Status relative to this architecture page |
|---------|-------------------------------------------|
| `omg doctor` / `omg doctor --strict` / `omg doctor --json` (also `omg --json doctor`) | **Shipped** — host/compat/probe honesty for **current host/session capabilities** only; does **not** negotiate Medley/model-routing enhancements today; enhanced fields must not false-green |
| `omg ask <provider>` / `omg ask <provider> --background` | **Shipped** — advisory broker only (`purpose = advisory`); not a Team executor; artifacts never set `verified` |
| `omg team status [--json]` | **Shipped** — locked `--json` view (no `route` / `route.kind`). Route kind labeling is a dual-host **contract target** and appears only on Presentation V1 (`--presentation`) |
| `omg agents list [--json]` | **Contract** — host-neutral agent/policy catalog; planned #131/#134; **not runnable today** |
| `omg agents explain <agent-or-profile> [--json]` | **Contract** — policy vs effective route/receipt; planned #131/#134; **not runnable today** |

Human and JSON views of the same facts must agree when those commands ship.
On original Grok Build, explain/list must show which enhanced facts are
unavailable. On Medley, they must separate **requested policy** from **effective
route** and receipt provenance. Opening diagnostics must not perform paid
provider probes or print secrets.

### Current vs contract examples (abbreviated)

Paired human + JSON sketches. Clearly abbreviated (`…`). Fictional ids only
(`run-example-1`, `t1`). No secrets, credential values, account ids, or
private origins.

#### `omg doctor` / `omg doctor --json` — SHIPPED

Human (resembles the real printer). Host capabilities today are
session/resume/close (plus `uuid_search`) only — **no** Medley/model-routing
registry:

```text
oh-my-grok doctor
------------------------------------------------
host: grok … compat=… tested=…
------------------------------------------------
[OK  ] grok on PATH: …
[OK  ] grok version (Stop gate): …
…
note: PreToolUse is fail-open soft-gate; not hard guarantee.
```

JSON success envelope is **top-level** (not nested under `data`):

```json
{
  "ok": true,
  "schema_version": 1,
  "command": "doctor",
  "strict": false,
  "failed": 0,
  "soft_warns": 0,
  "project_root": { "path": "…" },
  "host": {
    "binary": "grok",
    "compatibility": "…",
    "capabilities": {
      "session_resume": true,
      "session_close": true,
      "restore_code_explicit": false,
      "uuid_search": false
    }
  },
  "checks": [{ "name": "…", "ok": true, "detail": "…" }],
  "soft_checks": [{ "name": "…", "level": "ok", "detail": "…" }],
  "compat": { "has_risks": false, "strict_fail": false },
  "note": "PreToolUse is fail-open soft-gate; not hard guarantee."
}
```

#### `omg team status` / `omg team status --json` — SHIPPED (locked)

Human (resembles `format_status_table`):

```text
run_id:         run-example-1
session:        …
dry_run:        false
workspace_mode: worktree

task_id               win alive  status       worktree
------------------------------------------------------------------------
t1                       1 True   running      …
```

JSON via `emit_data` wraps the locked view. `data` contains **only** locked
keys — no `route`:

```json
{
  "ok": true,
  "schema_version": 1,
  "command": "team",
  "data": {
    "run_id": "run-example-1",
    "session": "…",
    "dry_run": false,
    "workspace_mode": "worktree",
    "tasks": [
      {
        "task_id": "t1",
        "window_index": 1,
        "worktree": "…",
        "status": "running",
        "alive": true
      }
    ]
  }
}
```

`omg team status --presentation` is Presentation State V1, **not** default
status. It may show `route.kind` of `external_executor` | `unknown` |
`native_host_receipt`. New start/scale stamps `external_executor`. Legacy rows
render `unknown`. `native_host_receipt` is optional receipt passthrough
(`receipt_ref` + sha256 digest), **not** a native execution runtime:

```json
{ "schema": 1, "kind": "external_executor", "executor": "fixture", "provider": "fake", "role": "executor", "posture": "read-write" }
```

#### `omg agents list [--json]` — CONTRACT-ONLY, planned #131/#134, **NOT runnable today**

**Not registered. Do not run.** Human + JSON below are **illustrative contract
targets** only. On a stock host, a Medley-only capability outcome is
**unsupported**; route-specific facts are **unavailable**.

```text
omg agents list   (not registered — contract target)

id                     requested policy     host facts
omg-verifier-example   inherit / exact      Medley-only capability: unsupported
                                            route-specific facts: unavailable
```

```json
{
  "ok": true,
  "schema_version": 1,
  "command": "agents.list",
  "data": {
    "agents": [
      {
        "id": "omg-verifier-example",
        "requested_policy": { "binding": "inherit" },
        "host_facts": {
          "medley_capability_outcome": "unsupported",
          "route_specific_facts": "unavailable"
        }
      }
    ]
  }
}
```

#### `omg agents explain <agent-or-profile> [--json]` — CONTRACT-ONLY, same honesty

**Not a current CLI.** Separate requested policy from effective route. Same
**unsupported** / **unavailable** distinction. Fictional id only.

```text
omg agents explain omg-verifier-example   (not registered — contract target)

requested policy: inherit parent model (no provider credentials in OMG state)
effective route:  (none labeled on stock host; not a shipped native runtime)
Medley-only capability outcome: unsupported
route-specific facts: unavailable
```

```json
{
  "ok": true,
  "schema_version": 1,
  "command": "agents.explain",
  "data": {
    "id": "omg-verifier-example",
    "requested_policy": { "binding": "inherit" },
    "effective_route": null,
    "host_facts": {
      "medley_capability_outcome": "unsupported",
      "route_specific_facts": "unavailable"
    }
  }
}
```

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
| OMG external consultation plane | [#138](https://github.com/ImL1s/oh-my-grok/issues/138) |
| OMG agent taxonomy | [#71](https://github.com/ImL1s/oh-my-grok/issues/71) |
| OMG Team runtime | [#69](https://github.com/ImL1s/oh-my-grok/issues/69) |
| Medley optional host-contract plan | [ImL1s/medley#287](https://github.com/ImL1s/medley/issues/287) |
| Medley architecture/docs counterpart | [ImL1s/medley#289](https://github.com/ImL1s/medley/issues/289) |
| Medley provider/route/statusline projection | [ImL1s/medley#207](https://github.com/ImL1s/medley/issues/207) |
| Medley Agents/lifecycle TUI counterpart (related, not claimed here) | [ImL1s/medley#290](https://github.com/ImL1s/medley/issues/290) |
| OMG security model | [security-model.md](../security-model.md) |
| OMG dual-host planning doc | [plans/2026-08-09-dual-host-agent-model-routing.md](../plans/2026-08-09-dual-host-agent-model-routing.md) |

Reciprocal links on the Medley side are owned by those Medley issues/PRs; this
repository only links to **public** counterparts and does not invent unpublished
paths.

## Drift and validation expectations

CI should eventually prove (and #133 acceptance requires):

- the eight-row Normative support matrix must match
  [`tests/fixtures/docs/normative_support_matrix_v1.json`](../../tests/fixtures/docs/normative_support_matrix_v1.json)
  (temporary docs contract until the #131 capability registry replaces it);
- support matrix / capability vocabulary stay aligned with the versioned registry
  when it lands (#131);
- documented **shipped** CLI names match registered commands;
- policy route-class names (`native`, `external_executor`) stay the
  documentation contract; do not equate policy `native` with Presentation
  `native_host_receipt`; shipped Presentation `route.kind` is only
  `external_executor` | `unknown` | `native_host_receipt`; default locked
  `omg team status --json` has no route field;
- Presentation route schema / kind / unknown / dual-carry wording stays
  bound to `omg_cli.team.presentation` exports (`ROUTE_SCHEMA`,
  `ROUTE_KIND_EXTERNAL`, `ROUTE_KIND_UNKNOWN`, `ROUTE_KIND_NATIVE_RECEIPT`,
  `unknown_route`, `build_external_route`);
- policy `external_executor` must stay distinct from advisory `omg ask`;
  the three dimension names (`runtime_kind`, `purpose`, `lifecycle`) must
  remain present;
- links to Medley #207 / #287 / #289 / #290 and OMG #131 / #133 / #138 remain present;
- maintained indexes and locales **point at** this page rather than forking the
  matrix into hand-maintained duplicates;
- no secret/header/query/account sentinel in examples;
- **no** statement that Medley is required for baseline OMG operation;
- hermetic Medley-absent stock-host smoke:
  [`tests/test_stock_host_medley_absent.py`](../../tests/test_stock_host_medley_absent.py)
  (setup / package projection, current `omg doctor`, ordinary agent/profile
  discovery, ordinary workflow parser/inventory; not a routing implementation).

Prefer generated snippets or managed markers for machine-owned tables once the
capability registry exists. Until then, this English page is the single
hand-maintained normative source.

## Non-goals

- no Medley provider setup guide duplication;
- no promise that stock Grok Build exposes Medley receipts or ordered candidates;
- no runtime benchmark/ranking claims;
- no claim that worktree/Team isolation is an execution sandbox;
- no implication of affiliation between OMG, Medley, or xAI;
- no runtime implementation of #131, #134, or #138 on this documentation page alone;
- no stock-host TUI / renderer or Medley #207 / #290 runtime shipped by this page.
