---
name: omg-lsp
description: OMG skill projection (host_owned, read-only)
omg_projection: true
omg_classification: host_owned
omg_capability_mode: read-only
omg_source_skill: skills/omg-lsp/SKILL.md
---
# PROJECTION — not an installed Antigravity plugin

This file is a static parity projection of the Grok plugin skill
`skills/omg-lsp/SKILL.md`. It is not an installed Antigravity plugin,
not live AG evidence, and does not mean `agy` skill discovery works.

- Catalog: `skills/catalog.json`
- capability_mode: `read-only` (never `execute`/`all`)
- Playbook without runtime evidence stays `configured`, not `verified`.

# omg-lsp — host-owned registration and status

## Honesty

| OMC | OMG |
|-----|-----|
| LSP registration | Repository `.lsp.json`, interpreted by Grok Build |
| OMG semantic proxy tools | **None** |
| OMG observation | Validate registration and report observed host status |

OMG does not implement hover, symbols, diagnostics, goto-definition, rename,
references, or language-server subprocess proxies. Those semantics belong to
the host. A valid registration with no fresh host observation is
`configured_unobserved`, never `healthy`.

## Playbook

```bash
omg lsp status                          # registration + host-observation truth
omg lsp validate                        # validate .lsp.json shape (precise errors)
```

Legacy semantic action names exist only for deprecation (`E_LSP_HOST_OWNED`);
do not advertise them as OMG capabilities — use the host IDE / Grok LSP client.

The repository registration uses Grok's server mapping shape, for example:

```json
{
  "python": {
    "command": "pyright-langserver --stdio",
    "extensionToLanguage": {".py": "python"}
  }
}
```

Use Grok's native language features when the host reports them. Otherwise use
ordinary read/search tools and describe the limitation plainly.

## Anti-patterns

- Treating a valid `.lsp.json` as proof that a server started successfully
- Treating local command discovery as host health evidence
- Advertising or calling OMG semantic LSP proxy operations
- Inventing hover, diagnostics, symbol, rename, or reference results
