# Grok Build host compatibility (#105 PR2)

Operator note for **runtime** host capability probing and backward-compatible
feature gates. Catalogue / pin governance lives in
[`docs/parity/generated/host-baseline.md`](parity/generated/host-baseline.md)
and [`docs/parity/upstream-snapshots/grok-build.json`](parity/upstream-snapshots/grok-build.json).

## Pin ≠ forced minimum

The repository `GROK_BUILD` pin tracks the **catalogue** host baseline
(currently `0.2.121` / `a5589e9…`). That pin does **not** force every install
to require v0.2.121.

- Stop gate floor remains **≥0.2.107** (unchanged).
- Doctor reports the **active** host version, tested range
  (`0.2.107`…`0.2.121`), compatibility, and usable capability set.
- Downstream features must **gate** on probed capabilities, not on the pin
  string alone.

## Capability truth order

Highest priority first:

1. **Behavior map** (explicit support / denial for named caps)
2. **ACP capability advertisement**
3. **CLI inspect JSON**
4. **Version fallback** (last resort)

Rules that prevent version false-greens:

- If an **advertisement** object is present, only explicitly advertised methods
  count. Missing methods (including `methods: []`) are **not available** —
  version must not fill them.
- If **inspect** is present (and no advertisement layer), omitted capability
  keys are fail-closed the same way.
- Version fallback runs only when neither advertisement nor inspect was
  provided (and layers were not malformed).

A host that *claims* `0.2.121` but whose behavior/inspect/ad deny or omit
`session/resume` is reported as **no resume**.

### Probe honesty (current scope)

Live collection today uses `grok version` / `grok --version` / `grok inspect`
when available. **Behavior** and **ACP advertisement** layers accept hermetic
fixtures and optional env injection (`OMG_HOST_BEHAVIOR_JSON`,
`OMG_HOST_ACP_ADVERTISEMENT_JSON`) for tests and break-glass. A real ACP
handshake / live behavior probe is later work (#105 sequence E+) — doctor does
not claim one exists yet.

## Feature gates (three states)

| State | Meaning |
|-------|---------|
| `AVAILABLE` | Capability observed; safe to use the modern path |
| `LEGACY` | Capability absent; documented fallback is allowed |
| `BLOCKED` | Capability absent; refuse rather than silent success |

Default absent policy (when the operation is not explicitly required):

| Capability | Absent → |
|------------|----------|
| `session_resume` | `LEGACY` (conversation load only; never implies code restore) |
| `session_close` | `BLOCKED` |
| `restore_code_explicit` | `BLOCKED` (resume ≠ restore) |
| `uuid_search` | `LEGACY` (current-directory lookup only) |

When a caller marks a capability **required** and it is missing, the gate is
always `BLOCKED` with an actionable `next_action`.

## Doctor JSON

```bash
omg doctor --json
```

Emits the existing schema_version-1 envelope with:

- `host` — version, tested range, compatibility, capability booleans, sources,
  gates, observations. The **host** block never includes session ids, auth
  tokens, transcripts, cwd, or home paths.
- `project_root` — selected project root for this doctor run (`path` + optional
  `source`). Home-directory prefixes in `path` are scrubbed to
  `[REDACTED_PATH]` in JSON (human tables still show the real path).

## Legacy path

Hosts in the tested window below `0.2.121` are `compatibility=legacy`:

- Prefer documented conversation load over ACP `session/resume`.
- Do not call ACP `session/close` or explicit code restore.
- UUID search stays directory-local until the host advertises cross-directory
  search.

Upgrade to ≥0.2.121 (or any host that **advertises** the needed methods) to
unlock the modern gates. See issue #105 for catalogue rows and downstream
adoption (`#103`, `#74`, …) — this document does not claim live verification.

## Team resume consumer (#105 PR3)

`omg team resume --provider-session` is the first OMG consumer of
`FeatureGateResult`:

- CLI runs `probe_host()` → `evaluate_feature_gate("session_resume", …,
  required=True)` and **injects** the gate into `resume_with_view`.
- Runtime / view planner never re-parse host versions.
- JSON outcomes stay three-way: `reconcile` / `provider_session` / `view`.
  A successful tmux attach or select is **not** provider-session success.
- `provider_session.status`: `not_requested` | `available` | `legacy` |
  `blocked`. BLOCKED (required) → fail closed. LEGACY keeps an actionable
  `next_action` and must not be reported as AVAILABLE.
- Real ACP `session/resume` transport is still unwired (`transport_wired:
  false` when AVAILABLE); this slice only consumes the gate.

Related trackers `#103` / `#68` / `#69` / `#74` **consume** host gates here
but are **not** completed by this PR.
