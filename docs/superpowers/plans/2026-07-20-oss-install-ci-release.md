# OSS Install · CI · Release Hygiene Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make public oh-my-grok installable and releasable for strangers: dual-track install docs, `omg --version`, hermetic CI including smoke, and a minimal release protocol — without PyPI or marketplace PR yet.

**Architecture:** Keep **plugin.json version as single source of truth**. CLI reports that version via `--version`. Docs describe **full install** (clone + install-plugin + omg symlink) vs **plugin-only** (half surface). CI stays zero-secret: pytest + smoke; never live_suite. Release docs define tag `vX.Y.Z` + GitHub Release checklist only.

**Tech Stack:** Python 3.11+, argparse, bash install/smoke scripts, GitHub Actions, markdown docs.

**Out of scope (YAGNI):** PyPI/pipx package, xai-org/plugin-marketplace PR, history rewrite, live GHA jobs, rewriting global hooks to GROK_PLUGIN_ROOT (document only).

**Repo root:** `/Users/iml1s/Documents/mine/oh-my-grok` · public `https://github.com/ImL1s/oh-my-grok` · current version **0.2.5**

---

## File map

| File | Responsibility |
|------|----------------|
| `plugin.json` | Version SoT (already 0.2.5) — do not invent a second number |
| `omg_cli/__init__.py` | Export `__version__` loaded from plugin.json |
| `omg_cli/main.py` | `--version` on root parser |
| `tests/test_cli_router.py` | Tests for version |
| `README.md` | Dual-track Quick start, upgrade/uninstall, CI badge |
| `CONTRIBUTING.md` | Point at release protocol |
| `.github/workflows/ci.yml` | Smoke + cache + permissions |
| `scripts/install-plugin.sh` | Optional auto-symlink `omg` when `~/.local/bin` writable |
| `CHANGELOG.md` | Keep-a-Changelog for 0.2.5 |
| `docs/RELEASE.md` | Maintainer release checklist |

---

### Task 1: Version SoT + `omg --version`

**Files:**
- Modify: `omg_cli/__init__.py`
- Modify: `omg_cli/main.py` (`build_parser`, optionally `main`)
- Modify: `tests/test_cli_router.py`
- Test: `tests/test_cli_router.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_cli_router.py`:

```python
def test_version_flag_matches_plugin_json():
    plugin = json.loads((REPO_ROOT / "plugin.json").read_text(encoding="utf-8"))
    expected = plugin["version"]
    r = _run_omg("--version")
    assert r.returncode == 0, r.stderr
    out = (r.stdout + r.stderr).strip()
    assert expected in out
    assert "omg" in out.lower() or expected in out
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /Users/iml1s/Documents/mine/oh-my-grok
.venv/bin/python -m pytest tests/test_cli_router.py::test_version_flag_matches_plugin_json -v
```

Expected: FAIL (unknown `--version` or no output match)

- [ ] **Step 3: Implement version loader + argparse**

`omg_cli/__init__.py`:

```python
"""oh-my-grok CLI package."""
from __future__ import annotations

import json
from pathlib import Path

def _load_version() -> str:
    plugin = Path(__file__).resolve().parents[1] / "plugin.json"
    try:
        data = json.loads(plugin.read_text(encoding="utf-8"))
        ver = str(data.get("version", "")).strip()
        return ver or "0.0.0"
    except (OSError, json.JSONDecodeError, TypeError):
        return "0.0.0"

__version__ = _load_version()
```

In `build_parser()` on the root `ArgumentParser` (after creating `parser`):

```python
from omg_cli import __version__

parser = argparse.ArgumentParser(
    prog="omg",
    description="oh-my-grok CLI — setup, doctor, state, and mode launchers",
    parents=[common],
)
parser.add_argument(
    "--version",
    action="version",
    version=f"omg {__version__}",
)
```

- [ ] **Step 4: Run tests**

```bash
.venv/bin/python -m pytest tests/test_cli_router.py -q --tb=short
.venv/bin/python -m pytest -q -m "not live" --tb=line
```

Expected: all pass; `bin/omg --version` prints `omg 0.2.5`

- [ ] **Step 5: Commit**

```bash
git add omg_cli/__init__.py omg_cli/main.py tests/test_cli_router.py
git commit -m "feat(cli): omg --version from plugin.json source of truth"
```

---

### Task 2: README dual-track install + upgrade/uninstall

**Files:**
- Modify: `README.md` (Quick start section ~L50–86)

- [ ] **Step 1: Replace Quick start with dual-track content**

Replace the Quick start section (from `## Quick start` through the relocate note) with:

```markdown
## Quick start

**Requirements:** [Grok Build CLI](https://github.com/xai-org/grok-build) (`grok` on `PATH`) · Python **3.11+**

OMG has **two surfaces**: Grok **plugin** (skills/agents/hooks) + **`omg` CLI** (state, accept, verified). You need both for the full product.

### Full install (recommended)

Use a **stable path** so the global soft-gate does not break when you tidy folders:

```bash
# 0) Host
curl -fsSL https://x.ai/cli/install.sh | bash

# 1) Clone to a stable home
git clone https://github.com/ImL1s/oh-my-grok.git ~/.local/share/oh-my-grok
cd ~/.local/share/oh-my-grok
./scripts/install-plugin.sh
# optional pin: git checkout v0.2.5

# 2) omg on PATH (not on PyPI yet)
ln -sf "$(pwd)/bin/omg" ~/.local/bin/omg   # ensure ~/.local/bin is on PATH
omg --version

# 3) Wire a project
cd /path/to/your-project
omg setup
omg doctor
```

`install-plugin.sh` runs `grok plugin install . --trust` **and** writes  
`~/.grok/hooks/omg-pretool-deny.json` with an **absolute path** into this checkout  
(plugin-bundled PreToolUse alone has been insufficient in live sessions).

### Plugin-only (half surface — not enough alone)

```bash
grok plugin install ImL1s/oh-my-grok --trust
# better pin: grok plugin install ImL1s/oh-my-grok@v0.2.5 --trust
```

This installs skills/agents from GitHub. It does **not** put `omg` on PATH and does **not** guarantee the global soft-gate. Prefer **Full install** unless you know you only need in-session skills.

### Upgrade / relocate / uninstall

| Action | Commands |
|--------|----------|
| Upgrade | `cd ~/.local/share/oh-my-grok && git pull && ./scripts/install-plugin.sh` |
| Relocate clone | Re-run `./scripts/install-plugin.sh` + refresh `ln -sf …/bin/omg ~/.local/bin/omg` |
| Uninstall plugin | `grok plugin uninstall oh-my-grok` (or name from `grok plugin list`) |
| Remove soft-gate | `rm -f ~/.grok/hooks/omg-pretool-deny.json` |
| Remove CLI link | `rm -f ~/.local/bin/omg` |

`omg setup` only scaffolds **project** files (`.omg/`, AGENTS fragment). It does **not** install the plugin.

Smoke after install:

```bash
omg doctor
omg ulw "noop" --dry-run
```
```

Also add a CI badge next to existing badges if not present:

```markdown
<a href="https://github.com/ImL1s/oh-my-grok/actions/workflows/ci.yml"><img src="https://github.com/ImL1s/oh-my-grok/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
```

- [ ] **Step 2: Sanity check**

```bash
rg -n 'stable home|Plugin-only|Uninstall|omg setup' README.md
# no dead grok-cli link
rg -n 'xai-org/grok-cli' README.md && exit 1 || true
```

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: dual-track install, stable home, upgrade/uninstall"
```

---

### Task 3: CI — smoke + cache + permissions

**Files:**
- Modify: `.github/workflows/ci.yml`

- [ ] **Step 1: Replace workflow with hardened hermetic CI**

```yaml
name: ci

on:
  push:
    branches: [main]
  pull_request:

permissions:
  contents: read

concurrency:
  group: ci-${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true

jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: ["3.11", "3.12"]
    steps:
      - uses: actions/checkout@v4
      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.python-version }}
          cache: pip
          cache-dependency-path: requirements-dev.txt
      - name: Install deps
        run: |
          python -m pip install --upgrade pip
          pip install -r requirements-dev.txt
      - name: Hermetic unit/integration
        run: python -m pytest -q -m "not live" --tb=short
      - name: Smoke + hermetic e2e
        run: ./scripts/smoke.sh
        env:
          OMG_E2E: "1"
          OMG_SMOKE_STRICT: "0"
```

Notes for implementer:
- Do **not** add live_suite.
- Do **not** add secrets.
- `smoke.sh` tolerates missing `grok` for plugin validate (WARN) and doctor soft fails when STRICT=0.

- [ ] **Step 2: Local parity check**

```bash
OMG_E2E=1 OMG_SMOKE_STRICT=0 ./scripts/smoke.sh
.venv/bin/python -m pytest -q -m "not live" --tb=line
```

Expected: smoke OK + ALL_REAL_E2E_OK; 402+ tests pass

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/ci.yml
git commit -m "ci: add smoke e2e, pip cache, permissions, concurrency"
```

---

### Task 4: CHANGELOG + RELEASE protocol

**Files:**
- Create: `CHANGELOG.md`
- Create: `docs/RELEASE.md`
- Modify: `CONTRIBUTING.md` (add short “Releases” pointer at end)

- [ ] **Step 1: Write CHANGELOG.md**

```markdown
# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Product version source of truth: [`plugin.json`](./plugin.json).

## [0.2.5] - 2026-07-20

### Added
- Core-purpose parity CLI surfaces (goal ledger, interview, review, UltraQA, autopilot destination gates).
- Open-source packaging: MIT LICENSE, SECURITY, CONTRIBUTING, hermetic GitHub Actions CI.
- Public verification summary under `docs/research/verification-2026-07-20.md`.
- `omg --version` (reads `plugin.json`).

### Changed
- README dual-track install (full vs plugin-only); recommend stable home `~/.local/share/oh-my-grok`.
- Live machine evidence no longer shipped; regenerate via `docs/research/live/README.md`.
- Git history scrubbed of home paths and live suite JSON (filter-repo).

### Security
- Isolation honesty documented in `docs/security-model.md` (capability_mode primary; PreToolUse fail-open soft-gate).
- Global PreToolUse soft-gate install path remains absolute-checkout (re-run `install-plugin.sh` after relocate).

## [Unreleased]

### Planned
- Optional PyPI/`pipx` CLI track (deferred).
- Optional PR to xAI plugin-marketplace (sha-pinned).
```

- [ ] **Step 2: Write docs/RELEASE.md**

```markdown
# Release protocol (maintainers)

## Version source of truth

1. Bump **`plugin.json` `"version"`** first (e.g. `0.2.6`).
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
gh release create "v${VERSION}" --title "v${VERSION}" --notes-file <(sed -n "/## \[${VERSION}\]/,/## \[/p" CHANGELOG.md | sed '$d')
```

Or use `grok plugin tag` if it matches your workflow and then push the tag.

## Dual-track install text for release notes

- **Full:** clone (or `git checkout vX.Y.Z`) → `./scripts/install-plugin.sh` → symlink `bin/omg`
- **Plugin-only:** `grok plugin install ImL1s/oh-my-grok@vX.Y.Z --trust` (CLI + global soft-gate still needed for full product)

## Not in this protocol yet

- PyPI publish
- Automated marketplace sha bump PR
```

- [ ] **Step 3: Append to CONTRIBUTING.md**

```markdown
## Releases

See [`docs/RELEASE.md`](docs/RELEASE.md). Version SoT is `plugin.json`. Users should prefer git tags (`vX.Y.Z`) over floating `main` when possible.
```

- [ ] **Step 4: Commit**

```bash
git add CHANGELOG.md docs/RELEASE.md CONTRIBUTING.md
git commit -m "docs: CHANGELOG and maintainer release protocol"
```

---

### Task 5: install-plugin optional PATH symlink

**Files:**
- Modify: `scripts/install-plugin.sh` (Next steps section / end)

- [ ] **Step 1: After writing hooks, auto-symlink when possible**

Before final `echo "install-plugin OK"`, add:

```bash
echo "== omg CLI symlink (best-effort) =="
LOCAL_BIN="${HOME}/.local/bin"
OMG_BIN="${ROOT}/bin/omg"
if [[ -x "$OMG_BIN" ]]; then
  mkdir -p "$LOCAL_BIN" 2>/dev/null || true
  if [[ -d "$LOCAL_BIN" && -w "$LOCAL_BIN" ]]; then
    ln -sfn "$OMG_BIN" "${LOCAL_BIN}/omg"
    echo "linked ${LOCAL_BIN}/omg -> ${OMG_BIN}"
    if ! command -v omg >/dev/null 2>&1; then
      echo "NOTE: add ${LOCAL_BIN} to PATH if 'omg' is not found" >&2
    fi
  else
    echo "WARN: cannot write ${LOCAL_BIN}; symlink manually:" >&2
    echo "  ln -sf \"${OMG_BIN}\" \"\${HOME}/.local/bin/omg\"" >&2
  fi
fi
```

Keep existing manual next-steps text as fallback (can shorten step 1 now that auto runs).

- [ ] **Step 2: Shellcheck-ish dry read**

```bash
bash -n scripts/install-plugin.sh
```

Expected: exit 0

- [ ] **Step 3: Commit**

```bash
git add scripts/install-plugin.sh
git commit -m "feat(install): best-effort symlink omg into ~/.local/bin"
```

---

### Task 6: Final verification (controller)

- [ ] **Step 1: Full hermetic suite**

```bash
.venv/bin/python -m pytest -q -m "not live" --tb=line
OMG_E2E=1 OMG_SMOKE_STRICT=0 ./scripts/smoke.sh
./bin/omg --version
```

Expected: all green; version `omg 0.2.5`

- [ ] **Step 2: Push + confirm CI**

```bash
git push origin HEAD
gh run list --limit 3
```

- [ ] **Step 3: Optional annotated tag for current SoT** (only if maintainer wants ship now)

```bash
# Only after push is green:
git tag -a v0.2.5 -m "oh-my-grok v0.2.5" 2>/dev/null || true
# If tag already exists elsewhere, skip or move carefully
git push origin v0.2.5
gh release create v0.2.5 --title "v0.2.5" --generate-notes
```

If tagging is undesirable mid-PR, leave as manual step in RELEASE.md only.

---

## Self-review (writing-plans)

| Spec item | Task |
|-----------|------|
| Dual-track install docs | Task 2 |
| Stable home + uninstall | Task 2 |
| omg --version / SoT | Task 1 |
| CI smoke + hardening | Task 3 |
| CHANGELOG + release protocol | Task 4 |
| install UX symlink | Task 5 |
| Verify before complete | Task 6 |
| No PyPI / no live GHA / no marketplace PR | Out of scope ✓ |

No TBD placeholders. Version string always **from plugin.json**.
