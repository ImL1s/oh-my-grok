---
name: omg-lsp
description: >
  Honest host-owned LSP registration surface for OMG. Use to inspect or explain
  repository .lsp.json configuration without claiming semantic proxy tools.
---

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
```

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
