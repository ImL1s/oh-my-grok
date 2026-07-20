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

## Not in this protocol yet

- PyPI publish
- Automated marketplace sha bump PR
