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

1. **Behavior probe** (observed support / denial)
2. **ACP capability advertisement**
3. **CLI inspect JSON**
4. **Version fallback** (last resort)

A host that *claims* `0.2.121` but whose behavior/inspect deny `session/resume`
is reported as **no resume** — version never false-greens.

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

Emits the existing schema_version-1 envelope with a `host` object:

- `version`, `tested_min`, `tested_max`, `compatibility`
- `capabilities` (booleans)
- `capability_sources` / `gates` / `observations`

Never includes session ids, auth tokens, transcripts, cwd, or home paths.

## Legacy path

Hosts in the tested window below `0.2.121` are `compatibility=legacy`:

- Prefer documented conversation load over ACP `session/resume`.
- Do not call ACP `session/close` or explicit code restore.
- UUID search stays directory-local until the host advertises cross-directory
  search.

Upgrade to ≥0.2.121 (or any host that **advertises** the needed methods) to
unlock the modern gates. See issue #105 for catalogue rows and downstream
adoption (`#103`, `#74`, …) — this document does not claim live verification.
