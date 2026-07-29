# CLI output and exit-code contract (#30)

English. This document freezes the **target** machine interface for `omg`
subcommands. Full migration of every family is staged; new surfaces should
follow this contract first.

**Related:** issue #30 (global JSON envelopes), #29 (command registry).

---

## Exit-code classes

| Code | Meaning |
|------|---------|
| `0` | Requested operation completed, or a query returned a **valid** state (including documented empty/missing) |
| `1` | Operational failure or negative result that prevents the requested operation |
| `2` | Usage / invalid input / policy-gate refusal **before** the operation runs |
| `126`/`127` | Executable permission / not-found only where conventional |
| signal-derived | Conventional shell signal exit |

Each command family must document whether empty/missing state is exit `0` or `1`.

### Documented examples (current)

| Surface | Success (0) | Failure (1) | Usage (2) |
|---------|-------------|-------------|-----------|
| `omg lsp status` | Always (probe JSON) | — | — |
| `omg lsp validate` | Valid `.lsp.json` | `E_LSP_MISSING` / `E_LSP_INVALID` | — |
| `omg lsp check\|symbols\|diagnostics` | never | always `E_LSP_HOST_OWNED` | — |
| `omg autopilot run` | terminal `verified`, or intentional pause (`await`/`interview`/`stall`) | `blocked`/`cancelled`/launch fail/`max_stall_relaunches` | bad argv |
| `omg team start --plan-only` | plan JSON printed; no mutation | parse/plan errors | missing `--tasks-json` |
| `omg team start` (live) | `startup_status=running` | `failed_start`/`degraded` | missing required flags |

---

## Stdout vs stderr

- **stdout:** machine-readable payload only when the command is scripted/JSON
  (autopilot run pauses, team plan-only, lsp validate/errors). No mixed prose.
- **stderr:** human hints, resume commands, “Team plan-only…”, deprecation notes.
- Interactive help and doctor tables may still use human text on stdout until
  a per-family migration lands.

---

## JSON envelope (schema_version 1)

### Success

```json
{
  "ok": true,
  "schema_version": 1,
  "command": "lsp.validate",
  "…domain fields…"
}
```

### Error

```json
{
  "ok": false,
  "schema_version": 1,
  "command": "lsp.validate",
  "error": "E_LSP_MISSING",
  "message": "human-readable summary",
  "next_action": "optional remediation hint"
}
```

Stable error codes already in use:

| Code | Surface |
|------|---------|
| `E_LSP_MISSING` | no `.lsp.json` |
| `E_LSP_INVALID` | parse / schema failure |
| `E_LSP_HOST_OWNED` | semantic proxy refused (#28) |
| `max_stall_relaunches` | autopilot unattended budget (#40) |

Prefer `error` as a string code for simple surfaces; nested
`error: {code,message,details,retryable,next_action}` is allowed for complex
families and is the long-term target for #30.

---

## Migration policy

1. **New / changed commands** in this release: follow schema_version 1 + exit table.
2. **Legacy human-only stdout** remains until that family is migrated; do not break
   existing scripts without a deprecation window.
3. **Global `--json` flag** (parse once, all subcommands) is Phase 2 of #30 and
   depends on command context from #29.
4. Secrets must never appear in envelopes (reuse `omg_cli.redaction`).

---

## Implementation status

| Phase | Status |
|-------|--------|
| This contract doc | **shipped** |
| Autopilot run machine JSON pauses | **shipped** (#40) |
| LSP validate / `E_LSP_*` | **shipped** (#28) |
| Team plan-only JSON | **shipped** (#27) |
| Shared envelope helpers (`omg_cli/cli_envelope.py`) | **shipped** (#29/#30) |
| Global `--json` / `--human` + `CommandContext` | **shipped** (partial migration) |
| Migrated under global `--json` via `emit_data` | **partial** — state, hud, wiki, lsp, notify, native-status, capabilities, parity, memory, tracker, compact, review, qa, autopilot |
| Golden contract tests for every family | deferred |
