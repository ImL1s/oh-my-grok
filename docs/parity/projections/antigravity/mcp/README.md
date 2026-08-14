# Antigravity MCP / tools projection

**Status:** static parity projection for
[#73](https://github.com/ImL1s/oh-my-grok/issues/73).

This is **not** an installed Antigravity plugin, not proof that `agy` loaded
an MCP server, and not live AG evidence.

Suggested registration (when the user opts in later):

```text
omg tools serve --stdio
```

Tool names are `omg.tools.*`. They are **not** registered on `omg mcp-server`.
`lsp.hover` and other `lsp.*` names stay forbidden on the Grok in-session MCP.

Grok `omg lsp` remains a host-owned `.lsp.json` probe (`E_LSP_HOST_OWNED` for
semantic operations).
