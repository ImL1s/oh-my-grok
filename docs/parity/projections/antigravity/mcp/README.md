# Antigravity MCP / tools projection

**Status:** static parity projection for
[#73](https://github.com/ImL1s/oh-my-grok/issues/73).

This projection is **not** an installed Antigravity plugin, not proof that
`agy` loaded an MCP server, and not live AG evidence. The repository root now
separately bundles the executable plugin manifest [`mcp_config.json`](../../../../../mcp_config.json);
do not confuse that configuration with this static parity document.

Root plugin registration:

```text
omg tools serve --stdio
```

Tool names are `omg.tools.*`. They are **not** registered on `omg mcp-server`.
`lsp.hover` and other `lsp.*` names stay forbidden on the Grok in-session MCP.

Grok `omg lsp` remains a host-owned `.lsp.json` probe (`E_LSP_HOST_OWNED` for
semantic operations).
