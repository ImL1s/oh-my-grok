# Release protocol (maintainers)

## Version source of truth

1. Bump **`plugin.json` `"version"`** first (e.g. `0.3.0`).
2. Confirm `omg --version` prints the same string.
3. Update README `Version: **X.Y.Z**` and `docs/security-model.md` header if present.
4. Add a CHANGELOG section for the release.

## Pre-tag gates (local)

```bash
python -m pytest -q -m "not live"
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
git push origin "v${VERSION}"
gh release create "v${VERSION}" --title "v${VERSION}" --generate-notes
```

Or use `grok plugin tag` if it matches your workflow and then push the tag.

## Dual-track install text for release notes

- **Full:** clone (or `git checkout vX.Y.Z`) → `./scripts/install-plugin.sh` → symlink `bin/omg`
- **Plugin-only:** `grok plugin install ImL1s/oh-my-grok@vX.Y.Z --trust` (CLI + global soft-gate still needed for full product)

## Not in this protocol yet

- PyPI publish
- Automated marketplace sha bump PR
