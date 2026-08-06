# Antigravity (`agy`) probe fixtures (#67-A)

Pinned capture notes for hermetic unit tests. **Do not** require a live `agy`
or network in CI.

| File | Source | Notes |
|------|--------|-------|
| `version.txt` | `agy --version` (host capture) | Semver string only; currently `1.1.10` |
| `help.txt` | `agy --help` (host capture) | Flag/subcommand surface for capability envelope |
| `fake_agy.py` | stub | Executable via PATH / `OMG_AGY_BIN`; honors `FAKE_AGY_VERSION` |

Upstream inventory pin (docs cross-ref only; compat is version-string based):

`docs/parity/upstream-snapshots/Antigravity.json` → `pin_revision`
`bfab12dac5bd090015a89cf82e65093d13b567d9`.

Refresh procedure: re-run `agy --version` / `agy --help` on a vetted host, update
these files, adjust `TESTED_MIN`/`TESTED_MAX` in `omg_cli/providers/antigravity.py`
if the tested range changes.
