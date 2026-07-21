---
name: omg-lsp
description: >
  Honest language-intelligence surface for OMG. Use when user says LSP, go to
  definition, hover, symbols, or semantic rename. Prefer Grok read_file/grep;
  optional local pyright probe and stdlib ast symbols/diagnostics via omg lsp.
---

# omg-lsp — optional local probes (no host LSP MCP)

## Honesty

| OMC | OMG |
|-----|-----|
| MCP LSP/AST tools | **No host MCP LSP** — local probe only |
| Default | Grok `read_file` / `grep` / `list_dir` |
| Optional | `omg lsp status` / `check` / `symbols` / `diagnostics` |

Do **not** claim semantic rename, goto-def, hover, or workspace-wide language
server features unless a local tool truly provides them. Prefer host tools.

## Playbook

```bash
omg lsp status                          # which local CLIs are on PATH
omg lsp check path/to/file.py           # pyright/basedpyright if installed
omg lsp symbols path/to/file.py         # stdlib ast: functions/classes/imports
omg lsp diagnostics path/to/file.py     # stdlib ast.parse syntax errors only
```

- **`symbols` / `diagnostics`** — pure Python `ast` (always available; Python
  source only). Not a language server.
- **`check`** — optional subprocess to pyright when installed.
- Fallback: grep for symbol, read definitions, small safe edits.

## Anti-patterns

- Inventing LSP results
- Blocking workflow when pyright absent
- Treating `omg lsp diagnostics` as type-checking (it is **syntax-only**
  via `ast.parse`; no types, no imports resolution, no semantic analysis)
- Claiming goto-def / hover / rename / find-references from these probes
