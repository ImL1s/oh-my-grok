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

# Optional live gates (needs grok auth + quota)
# ./scripts/live_suite.sh --quick
```

## Rules of the road

1. Fan-out only via Grok `spawn_subagent` (depth 1). No external agent CLIs as default workers.
2. Only the `omg` CLI may set `passes` / `verified` under `.omg/state/`.
3. Keep isolation claims aligned with `docs/security-model.md` (no “hard sandbox” marketing for fail-open hooks).
4. Prefer small, tested diffs. New accept runners go through `omg_cli/command_policy.py` floors.

## Pull requests

- Run `pytest -m "not live"` before opening a PR.
- Describe user-visible behavior and any security surface changes.
- Do not commit secrets, absolute home paths, or raw machine live logs under `docs/research/live/`.

## Releases

See [`docs/RELEASE.md`](docs/RELEASE.md). Version SoT is `plugin.json`. Users should prefer git tags (`vX.Y.Z`) over floating `main` when possible.
