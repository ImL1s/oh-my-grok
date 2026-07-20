# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Product version source of truth: [`plugin.json`](./plugin.json).

## [Unreleased]

### Planned
- Optional PyPI/`pipx` CLI track (deferred).
- Optional PR to xAI plugin-marketplace (sha-pinned).

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
