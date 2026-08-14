# Tools sidecar (oh-my-grok)

English. **Code:** [`omg_cli/tools_sidecar.py`](../omg_cli/tools_sidecar.py)
· CLI: `omg tools doctor|serve|lsp|ast|codegraph|research`

This is a **first cut** of [#73](https://github.com/ImL1s/oh-my-grok/issues/73).
It is an **OMG-owned sidecar**, not Grok-native LSP and not a live
Antigravity MCP install.

## Honesty

| Surface | What it is |
|---------|------------|
| `omg lsp status\|validate` | Unchanged host-owned `.lsp.json` probe. Semantic ops stay `E_LSP_HOST_OWNED`. |
| `omg mcp-server` | Unchanged focused workflow MCP. `lsp.*` names remain **forbidden**. |
| `omg tools …` | New sidecar. Semantic LSP talks to an explicit transport (`--fake-lsp` or `--lsp-command`). Missing ast-grep is **blocked**, not faked. |

## Commands

```text
omg tools doctor [--strict] [--root PATH]
omg tools serve --stdio
omg tools lsp servers|hover|definition|references|document_symbols|workspace_symbols|diagnostics|prepare_rename|rename|code_action|code_action_resolve
omg tools ast search|replace --pattern '...' --lang python
omg tools codegraph status|query --mode off|auto|shared|local
omg tools research status|search
```

- Rename / code-action **apply** and ast-grep **--write** require
  `--capability-mode read-write`. Default is preview / dry-run.
- Language servers are **never auto-installed**.
- Network research requires `OMG_TOOLS_NETWORK=1` and a configured provider
  (none is bundled; credentials are never shipped).
- CodeGraph `shared` results are **not** worktree-accurate.

## MCP image output

Sidecar results may carry a bounded **image descriptor** (`mime`,
`byte_length`, optional relative path / sha256). Raw image bytes and unbounded
base64 are rejected and must not be written into run/session state.

## Windows

Workspace confinement uses `Path.resolve()` + `relative_to` (supported on
Windows). This sidecar does **not** use POSIX `path_keys`. Live language-server
e2e on Windows is **untested** in this slice.

Antigravity projection:
[`docs/parity/projections/antigravity/mcp/`](./parity/projections/antigravity/mcp/)
(**not** live AG evidence).
