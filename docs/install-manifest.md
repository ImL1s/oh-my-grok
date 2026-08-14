# Install manifest (oh-my-grok)

English. **Code:** [`omg_cli/install_manifest.py`](../omg_cli/install_manifest.py)

First cut of [#77](https://github.com/ImL1s/oh-my-grok/issues/77). Extends
`omg setup` / `omg doctor`. This is **not** a separate installer.

## Flags

```text
omg setup --runtime grok|antigravity|both --scope project|user [--force] [--here]
```

Defaults (`grok` + `project`) keep today's project scaffold (AGENTS.md, gitignore,
global rules/hook). User scope writes `~/.omg-user/` and does **not** create a
project `.omg`. Setup from `$HOME` as a project is refused unless `--here`.

## Honesty

- File copy is **not** live Grok/Antigravity verification.
- Doctor JSON `host.auth.ok` and `host.live_evidence` stay false in this slice.
- An invalid/placeholder API key cannot false-green.
- Foreign and user-owned files are preserved unless `--force`.
- Interrupted transactions restore backups from `.omg/install/tx/`.

简体/繁體 indexes point at this English page.
