# Release protocol (maintainers)

English | [简体中文](./RELEASE.zh.md) | [繁體中文](./RELEASE.zh-TW.md)

## Current product line

| Field | Value |
|---|---|
| Version | **0.7.0** |
| Intended tag | `v0.7.0` |
| Public assets | `oh-my-grok-0.7.0.tar.gz`, then `SHA256SUMS` |
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

The manifest binds candidate commit/tree, toolchain, environment allowlist, source date epoch, archive hash/length, exact checksum bytes, and public upload order. Verify before any network writer:

```bash
omg parity release-readback \
  --manifest .omg/artifacts/dual-parity/RUN_ID/OMG-W6/release-bundle-manifest.json
python3 scripts/release_attest.py \
  --asset .omg/artifacts/dual-parity/RUN_ID/OMG-W6/release-bundle/oh-my-grok-0.7.0.tar.gz \
  --checksums .omg/artifacts/dual-parity/RUN_ID/OMG-W6/release-bundle/SHA256SUMS
```

Missing, extra, renamed, symlinked, or byte-drifted files fail closed. Never rebuild after upload begins. Upload only the two prebuilt files, in the manifest order.

## GitHub publication and readback

External writers are serialized by the run-manifest release state machine. Before each call, record the idempotency identity and exact expected bytes; after each call, perform bounded readback. Timeout/ambiguous results remain `unknown`, not success. Do not perform a blind retry.

The approved sequence is:

1. push the frozen candidate to the approved `main` ref and read back its OID;
2. create/read back the exact annotated `v<version>` tag;
3. create the GitHub release from that tag;
4. upload archive, read back hash/length;
5. upload `SHA256SUMS`, read back hash/length;
6. set/read back GitHub latest;
7. verify public latest install in a clean location;
8. persist canonical `release-completion-evidence.json`, including the
   transaction-bound readback chain, then use the dedicated release finalizer
   to move the run manifest from `release_active` to `closed`.

```bash
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
is missing or altered. A release workflow may verify and prepare evidence, but
it must not rebuild or silently publish different bytes. See
`.github/workflows/release.yml`.

## User install text

Convenient latest release:

```bash
curl -fsSL https://raw.githubusercontent.com/ImL1s/oh-my-grok/main/scripts/install.sh | bash
```

Pinned/manual GitHub-only:

```bash
TAG=v0.6.0
curl -fLO "https://github.com/ImL1s/oh-my-grok/releases/download/${TAG}/oh-my-grok-0.7.0.tar.gz"
curl -fLO "https://github.com/ImL1s/oh-my-grok/releases/download/${TAG}/SHA256SUMS"
shasum -a 256 -c SHA256SUMS
curl -fsSLo install.sh "https://raw.githubusercontent.com/ImL1s/oh-my-grok/${TAG}/scripts/install.sh"
bash install.sh --offline --archive ./oh-my-grok-0.7.0.tar.gz \
  --checksums ./SHA256SUMS --source-tag "${TAG}"
```

The installer verifies before extraction, bounds and rejects link/path escape archive members, stages immutably, switches plugin + CLI transactionally, runs strict doctor, writes a receipt, and rolls back failed activation.

## Plugin marketplace and package registries

The GitHub release is the claimed OMG channel. An xAI marketplace PR remains optional and requires that registry's current schema plus an exact tag SHA. PyPI/non-editable wheel publication and npm-style package registries are not claimed release channels for OMG 0.6.0. Do not imply otherwise in release notes.
