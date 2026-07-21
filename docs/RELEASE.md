# Release protocol (maintainers)

## Latest published

| Field | Value |
|-------|--------|
| Version | **0.3.2** |
| Tag | [`v0.3.2`](https://github.com/ImL1s/oh-my-grok/releases/tag/v0.3.2) |
| Notes | QA freeze allowlist UX, pytest marker coalesce, auto PRD from UltraQA, autopilot complete short-circuit, `autopilot_phase` sync |

Source of truth: [`plugin.json`](../plugin.json) · history: [`CHANGELOG.md`](../CHANGELOG.md)  
User guides: [`docs/skills.md`](./skills.md) (all skills) · [`docs/autopilot.md`](./autopilot.md) · skill: [`skills/omg-autopilot/SKILL.md`](../skills/omg-autopilot/SKILL.md)

## Version source of truth

1. Bump **`plugin.json` `"version"`** first (e.g. `0.3.2`).
2. Confirm `omg --version` prints the same string.
3. Update README `Version: **X.Y.Z**` and `docs/security-model.md` header if present.
4. Add a CHANGELOG section for the release.
5. Refresh this file’s **Latest published** table after tag + GitHub Release.

## Pre-tag gates (local)

```bash
python3 -m pytest -q -m "not live"
OMG_E2E=1 ./scripts/smoke.sh
grok plugin validate .
# optional isolation claims:
# python3 scripts/canary_pretool.py --live
# ./scripts/live_suite.sh --quick
```

Do **not** require live_suite for docs-only patches.

## Tag + GitHub Release

```bash
VERSION=$(python3 -c "import json; print(json.load(open('plugin.json'))['version'])")
git tag -a "v${VERSION}" -m "oh-my-grok v${VERSION}"
git push origin main
git push origin "v${VERSION}"
gh release create "v${VERSION}" --title "v${VERSION}" --generate-notes
# or paste CHANGELOG section as --notes
```

Or use `grok plugin tag` if it matches your workflow and then push the tag.

## Dual-track install text for release notes

- **Full:** clone (or `git checkout vX.Y.Z`) → `./scripts/install-plugin.sh` → symlink `bin/omg`
- **Plugin-only:** `grok plugin install ImL1s/oh-my-grok@vX.Y.Z --trust` (CLI + global soft-gate still needed for full product)

## Post-release docs hygiene

After a product release, a small **docs-only** follow-up is fine (no version bump):

- README default flow / tips matching new CLI behavior
- `docs/security-model.md` residual honesty for new gates
- This file’s **Latest published** row

```bash
git add README.md docs/ CHANGELOG.md   # only what changed
git commit -m "docs: post-release notes for vX.Y.Z"
git push origin main
```

## Packaging tracks

### Editable pipx / pip (available; **editable-only**)

`pyproject.toml` at repo root exposes console script `omg = omg_cli.main:main`
with dynamic version from `omg_cli.__version__` (reads `plugin.json`).

**Supported recipes only:**

```bash
pipx install --editable /path/to/oh-my-grok
# or, from a checkout:
pip install -e .
```

**Not supported:** non-editable `pip install .` / wheel / sdist install into
site-packages. Several modules resolve `plugin_root()` as
`Path(__file__).resolve().parents[1]` to find checkout-root **siblings**
(`plugin.json`, `templates/`, `skills/`, `agents/`, `hooks/`). A non-editable
install copies only `omg_cli/` → those siblings are missing under
site-packages → `omg --version` prints `0.0.0` and plugin_root features report
"missing" (graceful, not a crash). PEP 660 editable install keeps `__file__` in
the source tree so `plugin_root()` still resolves.

`./scripts/install-plugin.sh` + `ln -sf …/bin/omg` remains the **primary**
install path. If both the symlink and a pipx editable entry exist, you can get
two `omg` binaries on `PATH` — check with `which -a omg`.

**Not in this protocol yet:** publishing a non-editable wheel to PyPI.

### xAI plugin-marketplace (prepare only — do **not** submit)

A sha-pinned listing for [xai-org/plugin-marketplace](https://github.com/xai-org/plugin-marketplace)
needs, before any PR:

| Prerequisite | Status |
|--------------|--------|
| `plugin.json` fields | Present |
| `grok plugin validate .` | Gate in this protocol (pre-tag) |
| Pinned sha | `git rev-parse <tag>` after release tag |
| Registry entry schema | **UNVERIFIED** — pull exact schema from that repo's CONTRIBUTING before drafting |

**Isolation honesty for any listing** (do not overclaim security):

- Primary isolation is Grok's per-spawn `capability_mode` (read-only /
  read-write; never `execute`/`all` for default workers).
- PreToolUse soft-gate is **fail-open** — it is not a sandbox.
- No OMC-style Stop hard-pin (Grok Stop is passive).

Nothing is submitted by this release protocol; marketplace remains optional
prep-only until a human opens a PR with a verified schema.

### Still not in this protocol

- Automated marketplace sha bump PR
- Non-editable PyPI publish
