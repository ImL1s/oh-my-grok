# Host probe fixtures (#105 PR2)

Hermetic JSON inputs for `omg_cli.host_probe.load_fixture` / `probe_host_from_fixture`.
No real `grok` binary and no network.

| Fixture | Intent |
|---------|--------|
| `legacy-0.2.107.json` | Stop-gate floor; resume/close absent → LEGACY/BLOCKED |
| `legacy-no-resume.json` | Mid-legacy without resume |
| `0.2.121.json` | Modern: all four caps true |
| `malformed.json` | Non-object advertisement/inspect → fail closed |
| `version-lies.json` | Claims 0.2.121 but behavior/inspect deny resume |
| `advertisement-beats-version.json` | 0.2.120 advertises resume → resume true |
