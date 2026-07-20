---
name: omg-lsp
description: >
  Honest language-intelligence surface for OMG. Use when user says LSP, go to
  definition, hover, symbols, or semantic rename. Prefer Grok read_file/grep;
  optional local pyright probe via omg lsp.
---

# omg-lsp — optional local probes (no host LSP MCP)

## Honesty

| OMC | OMG |
|-----|-----|
| MCP LSP/AST tools | **Missing host bridge** |
| Default | Grok `read_file` / `grep` / `list_dir` |
| Optional | `omg lsp status` / `omg lsp check PATH` if pyright on PATH |

Do **not** claim semantic rename or workspace-wide LSP unless a local tool reports available.

## Playbook

```bash
omg lsp status
omg lsp check path/to/file.py   # pyright/basedpyright if installed
```

Fallback: grep for symbol, read definitions, small safe edits.

## Anti-patterns

- Inventing LSP results
- Blocking workflow when pyright absent
