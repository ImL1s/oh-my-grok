# Install manifest (oh-my-grok)

English. **Code:** [`omg_cli/install_manifest.py`](../omg_cli/install_manifest.py)

First cut of [#77](https://github.com/ImL1s/oh-my-grok/issues/77). Extends
`omg setup` / `omg doctor`. This is **not** a separate installer.

## Flags

```text
omg setup --runtime grok|antigravity|both --scope project|user [--force] [--here]
omg setup import --from PATH [--dry-run] [--json]
omg setup migrate --from PATH [--dry-run] [--json]
```

Defaults (`grok` + `project`) keep today's project scaffold (AGENTS.md, gitignore,
global rules/hook). Those legacy steps run **inside** the install transaction
(not as a separate `run_setup` call). `--runtime antigravity` still applies
generic `.omg` gitignore init. User scope writes `~/.omg-user/` and does **not**
create a project `.omg`. Setup from `$HOME` as a project is refused unless `--here`.

`omg setup import` copy-safe ingests a file or directory of user artifacts
(rules, skills, agents, hooks, MCP JSON) into `.omg/install/imported/`
(`~/.omg-user/install/imported/` for `--scope user`) as `ownership=imported`
with provenance (source posix path, sha256, byte_size, imported_at). It never
follows symlinks and refuses credential-shaped bytes (`api_key`, `sk-`, bearer
tokens, private-key PEM — same needles as medley inspect / redaction).
`--dry-run` prints planned rows and writes nothing.

`omg setup migrate` classifies a legacy GROK_HOME or project tree in place
(managed / imported / user-owned / foreign) and records rows without overwriting
user-owned files. `--dry-run` is honored. Apply fails closed on foreign or
malformed classification (no `--force` override).

`omg uninstall --yes` consults the install manifest when present: only
receipt-owned or manifest-owned **unchanged** regular files are unlinked.
On-disk sha256 drift is preserved. Project `.omg/state` is never deleted.

## Honesty

- File copy is **not** live Grok/Antigravity verification.
- Doctor JSON `host.auth.ok` and `host.live_evidence` stay false in this slice.
- An invalid/placeholder API key cannot false-green.
- Foreign and user-owned files are preserved unless `--force` (preserved
  rows are not managed drift).
- A directory occupying a managed path is `foreign`. `--force` refuses to
  write bytes onto a directory (it does not `write_bytes` over a dir).
- Interrupted transactions restore backups from `.omg/install/tx/`
  (including the manifest itself if the commit marker fails). Restore
  targets must stay under the install root (or the machine grok home for
  optional global rules/hook); `backup_dir` must be the expected `tx/<id>`
  directory.
- Manifest and artifact writes never follow a symlink; a claimed path that
  becomes a symlink is drift. A symlinked `.omg` (or other parent) is refused.
- Overwrites larger than the backup cap fail closed.
- Mergeable `AGENTS.md` / `.gitignore` record the on-disk hash after setup so
  doctor is not immediately stale. Rollback restores those backups.
- User-scope `user.manifest.marker` is a **state marker**, not a runtime plugin
  artifact. Inspect `enabled` / `loadable` are true only when a runtime
  plugin/rules artifact is enabled and exact.
- `desired_artifacts()` ids must match frozen `EXPECTED_IDS_BY_RUNTIME_SCOPE`
  (non-empty schema-valid rows are not enough).
- `omg doctor` probes both the project manifest and `~/.omg-user`.

简体/繁體 indexes point at this English page.
