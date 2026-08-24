# Tools sidecar (oh-my-grok)

English. **Code:** [`omg_cli/tools_sidecar.py`](../omg_cli/tools_sidecar.py)
· CLI: `omg tools doctor|serve|lsp|ast|codegraph|research`

This is an **OMG-owned sidecar** for [#73](https://github.com/ImL1s/oh-my-grok/issues/73).
It is **not** Grok-native LSP and **not** a live Antigravity MCP install.

## Honesty

| Surface | What it is |
|---------|------------|
| `omg lsp status\|validate` | Unchanged host-owned `.lsp.json` probe. Semantic ops stay `E_LSP_HOST_OWNED`. |
| `omg mcp-server` | Unchanged focused workflow MCP. `lsp.*` names remain **forbidden**. |
| `omg tools …` | Sidecar. Semantic LSP talks to an explicit transport (`--fake-lsp` or `--lsp-command`). Missing ast-grep is **blocked**, not faked. |

- Detected language servers (for example `rust-analyzer` on PATH) are **not** `ready` until a sidecar session actually starts/initializes them. Doctor never auto-starts arbitrary LSPs.
- `omg tools doctor --strict` emits a **failure** envelope (`ok: false`) when inner checks fail. Inner and outer `ok` stay consistent. Doctor never sets `verified` / `observed` / `healthy` true.
- Server `workspace/configuration` requests during initialize/hover/definition are answered with empty settings (one `{}` per item, or `[]`) so the language server can proceed. They are not dropped.
- `didOpen` text is bounded. Files larger than the sidecar cap are stamped `truncated: true`; hover / definition / rename / code_action (and other document semantic ops) are **refused** rather than analyzing a silent prefix.
- CodeGraph is a **toy local import/symbol scan** with bounded SCIP-inspired JSON `occurrences` (`definition`/`reference`). It is **not** SCIP protobuf, not a real SCIP indexer, and not a branch-accurate shared graph.
- Network research stays opt-in and **blocked** until a provider and credentials exist (none are bundled).
- Windows confinement is `Path.resolve()` + `relative_to`, not POSIX `path_keys`.
- Never writes `passes` / `verified`.

## Commands

```text
omg tools doctor [--strict] [--root PATH]
omg tools serve --stdio [--lsp-command ARGV…] -- [--stdio]
omg tools lsp servers|hover|definition|references|document_symbols|workspace_symbols|diagnostics|prepare_rename|rename|code_action|code_action_resolve
omg tools ast search|replace --pattern '...' --lang python
omg tools codegraph status|query|index --mode off|auto|shared|local
omg tools research status|search
```

### `--lsp-command` and `--`

`--lsp-command` takes the server program (and non-flag argv). Server flags such
as `--stdio` must come **after `--`**, otherwise argparse treats `--stdio` as
the `omg tools serve` flag (or as an unknown option on `omg tools lsp`).
`rust-analyzer` speaks stdio by default and **rejects** `--stdio`
(`unexpected flag`); the sidecar drops that flag for rust-analyzer:

```text
omg tools serve --stdio --lsp-command rust-analyzer
omg tools lsp hover --path src/main.rs --line 0 --character 3 --lsp-command rust-analyzer
omg tools lsp hover --path a.py --lsp-command pylsp -- --stdio
```

Language servers are **never auto-installed**.

- Rename / code-action **apply** and ast-grep **--write** require
  `--capability-mode read-write`. Default is preview / dry-run.
- `code_action` always sends the LSP-required `range` (`--line`/`--character`
  plus optional `--end-line`/`--end-character`).
- After `didOpen`, later disk edits send `textDocument/didChange` (full-text)
  on the next sidecar operation for that URI.
- `workspace/configuration` is answered with empty settings. Truncated
  documents fail closed (`E_LSP_TRUNCATED`) instead of a prefix analysis.
- Network research requires `OMG_TOOLS_NETWORK=1` and a configured provider
  (none is bundled; credentials are never shipped).
- CodeGraph `shared` results are **not** worktree-accurate. `local` is
  branch-accurate only when an index was built from this tree and is not stale.
  `omg tools codegraph index` writes
  `.omg/artifacts/codegraph/{local,shared}-index.json` including bounded
  SCIP-inspired `occurrences` (query matches name/`symbol_id`; not protobuf SCIP).

## AST-grep

Doctor looks for the `ast-grep` binary on PATH, then `~/.cargo/bin` (and
`CARGO_HOME`). `sg` is accepted only after an identity check so shadow-utils
`/usr/bin/sg` is **not** treated as ast-grep.

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
