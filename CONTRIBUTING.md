# Contributing

Thanks for interest in **oh-my-grok**. This is a Grok Build plugin + local `omg` CLI.

## Dev setup

```bash
# Host
curl -fsSL https://x.ai/cli/install.sh | bash   # or follow https://github.com/xai-org/grok-build

git clone https://github.com/ImL1s/oh-my-grok.git
cd oh-my-grok
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
./scripts/install-plugin.sh
ln -sf "$(pwd)/bin/omg" ~/.local/bin/omg
```

## Tests

```bash
# Hermetic (default gate + local parity with CI)
python -m pytest -q -m "not live"
OMG_E2E=1 OMG_SMOKE_STRICT=0 ./scripts/smoke.sh
python scripts/check_parity_inventory.py
python scripts/check_traceability.py
python scripts/check_writer_ownership.py
python scripts/generate_standalone_hook.py --check
python scripts/generate_capabilities_lock.py --check
ruff check omg_cli/{__init__,main,autopilot,modes,pipeline,ralplan,review,qa,guidance}.py \
  tests/{test_cli_router,test_autopilot,test_modes,test_pipeline,test_ralplan,test_review,test_qa,test_packaging,test_docs_cli_drift,test_release_readback}.py
python -m mypy --follow-imports=skip omg_cli/main.py omg_cli/__init__.py tests/test_release_readback.py
python -m compileall -q omg_cli

# Optional live gates (needs grok auth + quota)
# ./scripts/live_suite.sh --quick
```

Do not commit absolute home paths (`/Users/...`) or private report validators that depend on machine-local files under `tests/`. Hermetic mocks only; optional research report checks go via CLI arg / `OMG_RESEARCH_REPORT_PATH`, not default pytest collection.

Ruff and mypy are static gates when installed by the release environment. They
are intentionally not runtime dependencies.

## Rules of the road

1. Fan-out only via Grok `spawn_subagent` (depth 1). No external agent CLIs as default workers.
2. Only the `omg` CLI may set `passes` / `verified` under `.omg/state/`.
3. Keep isolation claims aligned with `docs/security-model.md` (no “hard sandbox” marketing for fail-open hooks).
4. Prefer small, tested diffs. New accept runners go through `omg_cli/command_policy.py` floors.
5. Workflow definitions are immutable by `(name, workflow_version)`; behavioral
   changes require a new semantic version and migration metadata.
6. Do not infer enabled/healthy/verified from `.mcp.json`, `.lsp.json`, local
   `.rhai` files, or help text. Preserve independent capability tiers.

## Locale / translations

- Canonical product docs are English (`README.md`, `docs/*.md`).
- Localized README copies live under [`docs/readme/`](docs/readme/README.md) (`.zh.md` / `.zh-TW.md` only — never root translations or `.zh-Hant.md`).
- Keep language switchers and the `## Languages` list in sync when adding a locale.
- Prefer updating existing translations over alternate naming schemes.

## Pull requests

- Run `pytest -m "not live"` before opening a PR.
- Describe user-visible behavior and any security surface changes.
- Link new workflow/recovery/release behavior to tests and note any
  `optional_unclaimed` native surface explicitly.
- Do not commit secrets, absolute home paths, or raw machine live logs under `docs/research/live/`.

## Releases

See [`docs/RELEASE.md`](docs/RELEASE.md). Keep `omg_cli.__version__` and
`plugin.json` equal. GitHub assets are prebuilt once, verified with
`omg parity release-readback`, then uploaded in the manifest's exact order.
