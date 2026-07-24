# 发布协定（维护者）

English | [简体中文](./RELEASE.zh.md) | [繁體中文](./RELEASE.zh-TW.md)

## 目前产品线

| 字段 | 值 |
|---|---|
| Version | **0.6.0** |
| Intended tag | `v0.6.0` |
| Public assets | `oh-my-grok-0.6.0.tar.gz`，再来是 `SHA256SUMS` |
| Install | GitHub release transaction；不依赖 PyPI |

发布成功**不是**因为测试通过或 tag 存在就算数。产品成功条件是：不可变的 release transaction 状态为 `complete`，且 run manifest 在精确的 branch、commit、bundle、GitHub asset 与 latest-release readback 之后 finalized 为 `closed`。

## 版本与产生的产物

`omg_cli.__version__` 必须等于 `plugin.json.version`。Python 常数可安全给 wheel／建置 metadata 使用；`plugin.json` 是 plugin manifest。冻结产品字节前：

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

当输入刻意变更时，各跑一次 generator、审查字节、再跑一次，并在 `--check` 前证明 hash 不变。

## 候选闸门

在**精确的候选 commit** 上执行，并把输出记录进 W6 aggregate：

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

Live Grok 闸门只在“依赖目前 host 行为”的宣称时才需要。设定档或 help probe **不能**把能力升格为 observed／healthy／verified。

## 冻结的 run manifest

`omg parity run` 委托 `omg_cli.contracts.run_manifest` 里的精确契约引擎；它**不是**第二套实作。

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

所有 W0–W6 handoff 与 aggregate 的 input／final 签章都必须对冻结候选验证。不要对移动中的 worktree 签名，也不要重产另一个 wave 的产物。

## 建一次，上传精确字节

整合／发布负责人在以下路径建立一份决定性的预建 bundle：

```text
.omg/artifacts/dual-parity/<run-id>/OMG-W6/
  release-bundle-manifest.json
  release-bundle/
    oh-my-grok-<version>.tar.gz
    SHA256SUMS
```

manifest 绑定候选 commit／tree、toolchain、环境 allowlist、source date epoch、archive hash／长度、精确 checksum 字节，以及公开上传顺序。任何网络写入前先验证：

```bash
omg parity release-readback \
  --manifest .omg/artifacts/dual-parity/RUN_ID/OMG-W6/release-bundle-manifest.json
python3 scripts/release_attest.py \
  --asset .omg/artifacts/dual-parity/RUN_ID/OMG-W6/release-bundle/oh-my-grok-0.6.0.tar.gz \
  --checksums .omg/artifacts/dual-parity/RUN_ID/OMG-W6/release-bundle/SHA256SUMS
```

缺档、多档、改名、symlink 或字节漂移一律 fail-closed。上传开始后**绝不**重建。只依 manifest 顺序上传那两个预建档。

## GitHub 发布与 readback

外部写入由 run-manifest 的 release state machine 序列化。每次呼叫前记录 idempotency 身份与精确期望字节；呼叫后做有界 readback。Timeout／ambiguous 结果维持 `unknown`，不是成功。不要盲目重试。

核准顺序：

1. 把冻结候选 push 到核准的 `main` ref 并 readback OID；
2. 建立／readback 精确的 annotated `v<version>` tag；
3. 从该 tag 建立 GitHub release；
4. 上传 archive，readback hash／长度；
5. 上传 `SHA256SUMS`，readback hash／长度；
6. 设定／readback GitHub latest；
7. 在干净位置验证公开 latest 安装；
8. 持久化 canonical `release-completion-evidence.json`（含 transaction-bound readback chain），再用专用 release finalizer 把 run manifest 从 `release_active` 移到 `closed`。

```bash
omg parity run finalize-release \
  --path .omg/state/runs/RUN_ID/run-manifest.json \
  --expected-revision REVISION \
  --expected-previous-manifest-hash SHA256 \
  --expected-lease-generation GENERATION \
  --evidence .omg/artifacts/dual-parity/RUN_ID/OMG-W6/release-evidence-input.json
```

通用的 manifest transition 路由**不能**关闭 release。finalizer 会把证据绑到精确的 `release_active` manifest hash、冻结 bundle hash、release nonce、候选 commit，以及必要的 per-channel／asset readback；若不可变的 0400 证据缺失或被改，closed manifest 验证会失败。release workflow 可以验证并准备证据，但不得重建或默默发布不同字节。见 [`.github/workflows/release.yml`](../../.github/workflows/release.yml)。

## 使用者安装文字

便利的 latest release：

```bash
curl -fsSL https://raw.githubusercontent.com/ImL1s/oh-my-grok/main/scripts/install.sh | bash
```

钉版／手动、仅 GitHub：

```bash
TAG=v0.6.0
curl -fLO "https://github.com/ImL1s/oh-my-grok/releases/download/${TAG}/oh-my-grok-0.6.0.tar.gz"
curl -fLO "https://github.com/ImL1s/oh-my-grok/releases/download/${TAG}/SHA256SUMS"
shasum -a 256 -c SHA256SUMS
curl -fsSLo install.sh "https://raw.githubusercontent.com/ImL1s/oh-my-grok/${TAG}/scripts/install.sh"
bash install.sh --offline --archive ./oh-my-grok-0.6.0.tar.gz \
  --checksums ./SHA256SUMS --source-tag "${TAG}"
```

安装程式在解压前验证、限制并拒绝 link／path 逃逸的 archive 成员、不可变暂存、交易式切换 plugin + CLI、跑 strict doctor、写 receipt，并在启用失败时回滚。

## Plugin marketplace 与套件登录

GitHub release 是 OMG 宣称的发布通道。xAI marketplace PR 仍属可选，且需要该登录当前 schema 加上精确 tag SHA。PyPI／非 editable wheel 与 npm 式套件登录**不是** OMG 0.6.0 宣称的发布通道。发布说明不要暗示否则。
