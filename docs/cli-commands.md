# CLI command inventory

English. **Source of truth:** `omg_cli/command_registry.py` (`COMMAND_SPECS`).

This table is regenerated from the registry (#29 Phase 4). Do not hand-edit
between the markers — update `COMMAND_SPECS` and re-run:

```bash
python3 scripts/generate_cli_commands_doc.py
# or check only:
python3 scripts/generate_cli_commands_doc.py --check
```

Related: [cli-contract.md](./cli-contract.md) (exit codes + JSON envelopes).

<!-- OMG:CLI-COMMANDS:START -->
| Command | Family | Summary |
|---------|--------|---------|
| `setup` | install | scaffold .omg + install rules/hooks |
| `doctor` | install | health + drift checks |
| `update` | install | refresh plugin / guidance |
| `uninstall` | install | remove install artifacts |
| `install-hook` | install | install global PreToolUse hook |
| `note` | memory | append project note |
| `state` | run | active run status |
| `cancel` | run | abort active run |
| `resume` | run | print / clear RESUME.md |
| `session` | run | session recovery helpers |
| `recover` | run | bounded recovery |
| `memory` | memory | project memory |
| `tracker` | memory | lifecycle tracker |
| `compact` | memory | compaction helpers |
| `notify` | inspect | notification channels |
| `native-status` | inspect | native host status pack |
| `workflow` | workflow | repository workflows |
| `capabilities` | inspect | capabilities lock surface |
| `parity` | inspect | parity matrix |
| `wiki` | inspect | project wiki |
| `hud` | inspect | one-line HUD |
| `lsp` | inspect | host-owned .lsp.json inspection |
| `provider` | inspect | provider probe (Antigravity capabilities/doctor; #67-A) |
| `interview` | workflow | deep-interview gate |
| `goal` | workflow | ultragoal ledger |
| `accept` | team | acceptance + verified stamp |
| `integrate` | team | worktree integrate |
| `worker` | team | ULW worker ownership |
| `team` | team | tmux team plane (default on; kill-switch OMG_DISABLE_TMUX_TEAM); inside default `same_window` (#96), `--dedicated-window`, outside `detached_session` |
| `review` | modes | structured dual review |
| `qa` | modes | ultraqa freeze/run |
| `autopilot` | modes | strict phase FSM |
| `ulw` | modes | ultrawork fan-out |
| `ralph` | modes | ralph persistence loop |
| `ralplan` | modes | ralplan consensus |
| `ask` | modes | human-only external advisor broker |
| `pipeline` | modes | plan→implement→verify FSM |
| `dual-review` | modes | critic then verifier |
| `mcp-server` | mcp | stdio MCP server |
| `mcp-install` | mcp | install MCP registration |
<!-- OMG:CLI-COMMANDS:END -->

