# Release protocol (maintainers)

English | [简体中文](./RELEASE.zh.md) | [繁體中文](./RELEASE.zh-TW.md)

## Current product line

| Field | Value |
|---|---|
| Version | **0.9.0** |
| Intended tag | `v0.9.0` |
| Public assets | `oh-my-grok-0.9.0.tar.gz`, then `SHA256SUMS` |
| Install | GitHub release transaction; no PyPI dependency |

The release is not published merely because tests pass or a tag exists. Product
success is immutable release transaction state `complete` plus a run manifest
finalized to `closed` after exact branch, commit, bundle, GitHub asset, and
latest-release readback.

## Version and generated artifacts

`omg_cli.__version__` must equal `plugin.json.version`. The Python constant is import-safe for wheel/build metadata; `plugin.json` is the plugin manifest. Before freezing product bytes:

```bash
python3 - <<'PY'
import json
from omg_cli import __version__
assert __version__ == json.load(open("plugin.json"))["version"]
print(__version__)
PY
python3 scripts/generate_standalone_hook.py --check
python3 scripts/generate_capabilities_lock.py --check
```

When inputs intentionally changed, run each generator once, review the bytes, run it again, and prove the hash is unchanged before `--check`.

## Deterministic package (#26)

Build the public archive from the shipping inventory (never hand-curate a second
file list):

```bash
python3 scripts/package_release.py --out dist/release-bundle --mtime 0
# dual-build must be byte-identical:
python3 scripts/package_release.py --out dist/release-bundle-b --mtime 0
cmp dist/release-bundle/*.tar.gz dist/release-bundle-b/*.tar.gz
```

CI workflow `release` jobs:

1. **package** (`contents: read`) — write `oh-my-grok-<ver>.tar.gz` + `SHA256SUMS`
2. **verify** (`contents: read`) — download the same artifact; tests/static/smoke; re-verify checksums
3. **publish** (`contents: write`, tags only) — upload exact bytes; public hash readback; no rebuild

## Candidate gates

Run on the exact candidate commit and record outputs in the W6 aggregate:

```bash
python3 scripts/check_parity_inventory.py
python3 scripts/check_traceability.py
python3 scripts/check_writer_ownership.py
python3 -m pytest -q -m "not live" --tb=short
ruff check omg_cli/{__init__,main,autopilot,modes,pipeline,ralplan,review,qa,guidance}.py \
  tests/{test_cli_router,test_autopilot,test_modes,test_pipeline,test_ralplan,test_review,test_qa,test_packaging,test_docs_cli_drift,test_release_readback}.py
python3 -m mypy --follow-imports=skip omg_cli/main.py omg_cli/__init__.py tests/test_release_readback.py
python3 -m compileall -q omg_cli
OMG_E2E=1 OMG_SMOKE_STRICT=0 ./scripts/smoke.sh
```

Live Grok gates are required only for claims that depend on current host behavior. A config file or help probe cannot promote a capability to observed/healthy/verified.

`omg team` is **default on** (kill switch `OMG_DISABLE_TMUX_TEAM=1`; legacy `OMG_EXPERIMENTAL_TMUX_TEAM=0` disables). Hermetic transport proof: `scripts/live_team_smoke.py --fixture-executor` → `FIXTURE_TEAM_SMOKE_OK`. Grok-live promotion proof remains `scripts/live_team_smoke.py --live` → `LIVE_TEAM_SMOKE_OK` (quota; do not claim Grok-live parity from fixture alone).

## Frozen run manifest

`omg parity run` delegates the exact contract engine in `omg_cli.contracts.run_manifest`; it is not a second implementation.

```bash
omg parity run init --root . --repository-id OMG --run-id RUN_ID \
  --frozen-base-commit COMMIT --frozen-base-tree TREE \
  --approved-branch main --approved-remote origin \
  --approved-remote-old-oid OLD_OID \
  --ownership-manifest-hash SHA256 \
  --artifact-hash requirements=SHA256 \
  --artifact-hash prd=SHA256 \
  --artifact-hash test_spec=SHA256 \
  --artifact-hash plan=SHA256 \
  --release-channel github

omg parity run verify --path .omg/state/runs/RUN_ID/run-manifest.json --root .
```

All W0-W6 handoffs and aggregate input/final signatures must verify against the frozen candidate. Do not sign around a moving worktree or regenerate another wave's artifact.

## Build once, upload exact bytes

The integration/release owner creates one deterministic prebuilt bundle at:

```text
.omg/artifacts/dual-parity/<run-id>/OMG-W6/
  release-bundle-manifest.json
  release-bundle/
    oh-my-grok-<version>.tar.gz
    SHA256SUMS
```

The manifest binds candidate commit/tree, toolchain, environment allowlist, source date epoch, archive hash/length, exact checksum bytes, and public upload order. Produce it with the canonical command (do not hand-author):

```bash
omg parity release-bundle \
  --run-id RUN_ID \
  --archive dist/release-bundle/oh-my-grok-0.9.0.tar.gz \
  --checksums dist/release-bundle/SHA256SUMS \
  --candidate-commit COMMIT \
  --candidate-tree TREE \
  --live-receipt \
  --write
omg parity release-readback \
  --manifest .omg/artifacts/dual-parity/RUN_ID/OMG-W6/release-bundle-manifest.json
python3 scripts/release_attest.py \
  --asset .omg/artifacts/dual-parity/RUN_ID/OMG-W6/release-bundle/oh-my-grok-0.9.0.tar.gz \
  --checksums .omg/artifacts/dual-parity/RUN_ID/OMG-W6/release-bundle/SHA256SUMS
```

Missing, extra, renamed, symlinked, or byte-drifted files fail closed. Never rebuild after upload begins. Upload only the two prebuilt files, in the manifest order.

## GitHub publication and readback

External writers are serialized by the run-manifest release state machine. Before each call, record the idempotency identity and exact expected bytes; after each call, perform bounded readback. Timeout/ambiguous results remain `unknown`, not success. Do not perform a blind retry.

The approved sequence is:

1. push the frozen candidate to the approved `main` ref and read back its OID;
2. create/read back the exact annotated `v<version>` tag (`git cat-file -t` must be `tag`);
3. create the GitHub release from that tag using the matching versioned CHANGELOG section as notes;
4. upload archive, read back hash/length (identity-safe; never `--clobber`);
5. upload `SHA256SUMS`, read back hash/length;
6. set/read back GitHub latest;
7. verify public latest install in a clean location (no credentials or private paths in evidence);
8. persist canonical `release-completion-evidence.json` via `omg parity release-evidence` (the only constructor), then use the dedicated release finalizer to move the run manifest from `release_active` to `closed`.

```bash
python3 scripts/release_github_facts.py notes --version 0.8.0 --output /tmp/notes.md
python3 scripts/release_github_facts.py tag-identity --tag v0.8.0 --expected-commit COMMIT
omg parity release-evidence \
  --facts /tmp/omg-release-facts.json \
  --output .omg/artifacts/dual-parity/RUN_ID/OMG-W6/release-evidence-input.json
omg parity run finalize-release \
  --path .omg/state/runs/RUN_ID/run-manifest.json \
  --expected-revision REVISION \
  --expected-previous-manifest-hash SHA256 \
  --expected-lease-generation GENERATION \
  --evidence .omg/artifacts/dual-parity/RUN_ID/OMG-W6/release-evidence-input.json
```

The generic manifest transition route cannot close a release. The finalizer
binds the evidence to the exact `release_active` manifest hash, frozen bundle
hash, release nonce, candidate commit, and required per-channel/asset
readbacks; closed manifests fail verification if that immutable 0400 evidence
is missing or altered. The tag-triggered workflow generates and validates the
bundle manifest, annotated-tag identity, changelog notes, asset/latest
readback, and a public-latest install probe. It invokes `finalize-release`
only when a `release_active` run and complete facts file are supplied
(`.omg/` is gitignored, so a GitHub Actions checkout does not invent a dual-parity
run). The facts file's `run_id` must be that dual-parity run, not the GitHub
Actions run id; `omg parity run finalize-release` takes the subcommand directly
(no `--` before `finalize-release`). A release workflow may verify and prepare
evidence, but it must not rebuild or silently publish different bytes.
Ambiguous writes remain unknown/non-success. GitHub asset identity is
the hashed remote download (`remote_assets`); local bundle bytes are never
substituted as a successful readback. Branch/tag protection is an
external settings gate: the workflow records `gh api` readback into the
`release-publication-facts` artifact and never claims `configured` when the
API is unavailable. See `.github/workflows/release.yml`.

## User install text

Convenient latest release:

```bash
curl -fsSL https://raw.githubusercontent.com/ImL1s/oh-my-grok/main/scripts/install.sh | bash
```

Pinned/manual GitHub-only:

```bash
TAG=v0.9.0
curl -fLO "https://github.com/ImL1s/oh-my-grok/releases/download/${TAG}/oh-my-grok-0.9.0.tar.gz"
curl -fLO "https://github.com/ImL1s/oh-my-grok/releases/download/${TAG}/SHA256SUMS"
shasum -a 256 -c SHA256SUMS
curl -fsSLo install.sh "https://raw.githubusercontent.com/ImL1s/oh-my-grok/${TAG}/scripts/install.sh"
bash install.sh --offline --archive ./oh-my-grok-0.9.0.tar.gz \
  --checksums ./SHA256SUMS --source-tag "${TAG}"
```

The installer verifies before extraction, bounds and rejects link/path escape archive members, stages immutably, switches plugin + CLI transactionally, runs an install-time doctor gate, writes a receipt, and rolls back failed activation.

Install-gate policy: **integrity stays fail-closed** (digest/pointers/owned globals/checksum/receipt). The install probe runs `doctor --strict` then, on failure, a non-strict doctor pass — coexistence-only WARNs (foreign orch / Claude compat) yield `completed_with_warning`; integrity FAILs roll back. Interactive `omg doctor --strict` is unchanged and still exits non-zero on coexistence. On gate failure the installer prints the non-strict doctor transcript (bounded, redacted; hashes only in the receipt).

Success banner says **integrity-verified** (not full strict purity). Upgrade a managed install without re-pasting curl:

```bash
omg update
```

`omg update` has three managed paths: (1) **release receipt** → checksum-verified stage `scripts/install.sh`; (2) **clean development** checkout matching the receipt → `git pull --ff-only` + `install-plugin.sh`; (3) **dirty / absent / drifted / unprovable** development → preserve the source checkout and fall back to the verified stage `install.sh` release transaction. Explicit contributor `root=` checkouts still refuse dirty trees without mutation. Pre-fix managed CLIs without this fallback must re-run the curl installer once.

## Plugin marketplace and package registries

The GitHub release is the claimed OMG channel. An xAI marketplace PR remains optional and requires that registry's current schema plus an exact tag SHA. PyPI/non-editable wheel publication and npm-style package registries are not claimed release channels for OMG 0.7.5. Do not imply otherwise in release notes.
