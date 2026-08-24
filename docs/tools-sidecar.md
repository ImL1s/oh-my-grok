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
- Hover / definition retry on LSP JSON-RPC `-32801` (`content modified`) while the language server indexes, the same way they retry JSON `null`.
- `didOpen` text is bounded. Files larger than the sidecar cap are stamped `truncated: true`; hover / definition / rename / code_action (and other document semantic ops) are **refused** rather than analyzing a silent prefix.
- CodeGraph is a **toy local import/symbol scan** that also writes a hermetic SCIP protobuf `Index` beside JSON-lite (`{local,shared}-index.scip`). Query prefers protobuf occurrences (`not_scip: false` when the `.scip` decodes). JSON-only remains `not_scip`. Homebrew MIP `scip` (scipopt) is **not** Sourcegraph SCIP. Shared indexes are still not branch-accurate.
- Network research is opt-in (`OMG_TOOLS_NETWORK=1`). When enabled, the default provider is **Wikipedia OpenSearch** (`GET en.wikipedia.org/w/api.php?action=opensearch`). Credentials are **never bundled**. `OMG_TOOLS_RESEARCH_PROVIDER=none` or `off` stays blocked (`E_NETWORK_NO_PROVIDER`). HTTP / timeout / non-JSON / unexpected shape fail closed as `E_NETWORK_PROVIDER` (not fake hits). This is **not** a live Antigravity MCP install.
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
- Network research requires `OMG_TOOLS_NETWORK=1`. Default provider is
  Wikipedia OpenSearch (`OMG_TOOLS_RESEARCH_PROVIDER=wikipedia`, or unset).
  `none`/`off` remain unconfigured. Credentials are never shipped. This is
  not live Antigravity MCP.
- CodeGraph `shared` results are **not** worktree-accurate. `local` is
  branch-accurate only when an index was built from this tree and is not stale.
  `omg tools codegraph index` writes
  `.omg/artifacts/codegraph/{local,shared}-index.json` (JSON-lite,
  `not_scip: true`) and a sibling `{local,shared}-index.scip` protobuf
  Index. Query prefers protobuf occurrences.

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
