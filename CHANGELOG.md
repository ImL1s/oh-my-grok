# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Product version source of truth: [`plugin.json`](./plugin.json).

## [Unreleased]

### Planned
- Optional PyPI/`pipx` CLI track (deferred).
- Optional PR to xAI plugin-marketplace (sha-pinned).

## [0.2.6] - 2026-07-20

### Added
- **`omg --madmax`**: OMC-style break-glass host launcher — full-open Grok (`--always-approve` + `--permission-mode bypassPermissions`) in a **new tmux session** each launch (timestamp + nonce).
- Guardrails: subcommand before `--madmax` → exit 2; `--safe` / non-bypass `--permission-mode` → exit 2; root `--yolo` is not a madmax alias (stripped with note).
- Login-shell pane command + `tmux new-session -e` env forward (no secrets in pane start-command text); best-effort DA1 drain.
- Docs: dual-track install, security-model Host launcher section, `docs/RELEASE.md`, CI smoke/e2e.

### Changed
- Hermetic CI runs `scripts/smoke.sh` in addition to pytest.
- Session naming / attach policy: never reattach old madmax sessions (continuity via `grok --continue` / `--resume`).

### Security
- Documented madmax as operator break-glass (not a sandbox); detached sessions remain until `tmux kill-session`.
- Env forward via tmux `-e` (not shell `export` in pane argv).

## [0.2.5] - 2026-07-20

### Added
- Core-purpose parity CLI surfaces (goal ledger, interview, review, UltraQA, autopilot destination gates).
- Open-source packaging: MIT LICENSE, SECURITY, CONTRIBUTING, hermetic GitHub Actions CI.
- Public verification summary under `docs/research/verification-2026-07-20.md`.
- `omg --version` (reads `plugin.json`).
- Dual-track install docs (full vs plugin-only); maintainer release protocol.

### Changed
- README recommends stable home `~/.local/share/oh-my-grok`.
- Live machine evidence no longer shipped; regenerate via `docs/research/live/README.md`.
- Git history scrubbed of home paths and live suite JSON (filter-repo).
- CI runs hermetic smoke/e2e in addition to pytest.

### Security
- Isolation honesty documented in `docs/security-model.md` (capability_mode primary; PreToolUse fail-open soft-gate).
- Global PreToolUse soft-gate install path remains absolute-checkout (re-run `install-plugin.sh` after relocate).
