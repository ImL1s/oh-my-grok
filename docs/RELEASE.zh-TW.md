# 發佈協定（維護者）

English | [简体中文](./RELEASE.zh.md) | [繁體中文](./RELEASE.zh-TW.md)

## 目前產品線

| 欄位 | 值 |
|---|---|
| Version | **0.9.0** |
| Intended tag | `v0.9.0` |
| Public assets | `oh-my-grok-0.9.0.tar.gz`，再來是 `SHA256SUMS` |
| Install | GitHub release transaction；不依賴 PyPI |

發佈成功**不是**因為測試通過或 tag 存在就算數。產品成功條件是：不可變的 release transaction 狀態為 `complete`，且 run manifest 在精確的 branch、commit、bundle、GitHub asset 與 latest-release readback 之後 finalized 為 `closed`。

## 版本與產生的產物

`omg_cli.__version__` 必須等於 `plugin.json.version`。Python 常數可安全給 wheel／建置 metadata 使用；`plugin.json` 是 plugin manifest。凍結產品位元組前：

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

當輸入刻意變更時，各跑一次 generator、審查位元組、再跑一次，並在 `--check` 前證明 hash 不變。

## 候選閘門

在**精確的候選 commit** 上執行，並把輸出記錄進 W6 aggregate：

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

Live Grok 閘門只在「依賴目前 host 行為」的宣稱時才需要。設定檔或 help probe **不能**把能力升格為 observed／healthy／verified。

`omg team` **預設開啟**（關閉：`OMG_DISABLE_TMUX_TEAM=1`；舊 `OMG_EXPERIMENTAL_TMUX_TEAM=0` 也會關）。Hermetic 傳輸證明：`scripts/live_team_smoke.py --fixture-executor` → `FIXTURE_TEAM_SMOKE_OK`。Grok-live 升格證明：`scripts/live_team_smoke.py --live` → `LIVE_TEAM_SMOKE_OK`（quota；2026-07-30 本地已綠；**不要**只靠 fixture 宣稱 Grok-live 對等）。不是完整 OMX catalog parity（v1：36 named / 25 implemented；見 `docs/team-operation-catalog-v1.md`）。

## 凍結的 run manifest

`omg parity run` 委託 `omg_cli.contracts.run_manifest` 裡的精確契約引擎；它**不是**第二套實作。

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

所有 W0–W6 handoff 與 aggregate 的 input／final 簽章都必須對凍結候選驗證。不要對移動中的 worktree 簽名，也不要重產另一個 wave 的產物。

## 建一次，上傳精確位元組

整合／發佈負責人在以下路徑建立一份決定性的預建 bundle：

```text
.omg/artifacts/dual-parity/<run-id>/OMG-W6/
  release-bundle-manifest.json
  release-bundle/
    oh-my-grok-<version>.tar.gz
    SHA256SUMS
```

manifest 綁定候選 commit／tree、toolchain、環境 allowlist、source date epoch、archive hash／長度、精確 checksum 位元組，以及公開上傳順序。用正規命令產生（不要手寫）：

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

缺檔、多檔、改名、symlink 或位元組漂移一律 fail-closed。上傳開始後**絕不**重建。只依 manifest 順序上傳那兩個預建檔。

## GitHub 發佈與 readback

外部寫入由 run-manifest 的 release state machine 序列化。每次呼叫前記錄 idempotency 身分與精確期望位元組；呼叫後做有界 readback。Timeout／ambiguous 結果維持 `unknown`，不是成功。不要盲目重試。

核准順序：

1. 把凍結候選 push 到核准的 `main` ref 並 readback OID；
2. 建立／readback 精確的 annotated `v<version>` tag（`git cat-file -t` 必須是 `tag`）；
3. 從該 tag 建立 GitHub release，notes 使用對應版本的 CHANGELOG 章節；
4. 上傳 archive，readback hash／長度（身分安全；絕不 `--clobber`）；
5. 上傳 `SHA256SUMS`，readback hash／長度；
6. 設定／readback GitHub latest；
7. 在乾淨位置驗證公開 latest 安裝（證據不含憑證或私人路徑）；
8. 用 `omg parity release-evidence`（唯一 constructor）持久化 canonical `release-completion-evidence.json`，再用專用 release finalizer 把 run manifest 從 `release_active` 移到 `closed`。

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

通用的 manifest transition 路由**不能**關閉 release。finalizer 會把證據綁到精確的 `release_active` manifest hash、凍結 bundle hash、release nonce、候選 commit，以及必要的 per-channel／asset readback；若不可變的 0400 證據缺失或被改，closed manifest 驗證會失敗。tag 觸發的 workflow 會生成並驗證 bundle manifest、annotated tag、CHANGELOG notes、asset／latest readback，以及公開 latest 安裝探針。只有在提供 `release_active` run 與完整 facts 時才會呼叫 `finalize-release`（`.omg/` 被 gitignore，CI checkout 不會捏造 dual-parity run）。facts 的 `run_id` 必須是 dual-parity run，而不是 GitHub Actions run id；`omg parity run finalize-release` 直接跟子命令（`--` 不要寫在 `finalize-release` 前面）。workflow 可以驗證並準備證據，但不得重建或悄悄發布不同位元組。GitHub 資產身份以遠端下載雜湊（`remote_assets`）為準，絕不把本地 bundle 位元組當作成功 readback。branch／tag protection 是外部 settings gate：workflow 把 `gh api` readback 寫入 `release-publication-facts` artifact，API 不可用時絕不宣稱 `configured`。見 [`.github/workflows/release.yml`](../../.github/workflows/release.yml)。

## 使用者安裝文字

便利的 latest release：

```bash
curl -fsSL https://raw.githubusercontent.com/ImL1s/oh-my-grok/main/scripts/install.sh | bash
```

釘版／手動、僅 GitHub：

```bash
TAG=v0.9.0
curl -fLO "https://github.com/ImL1s/oh-my-grok/releases/download/${TAG}/oh-my-grok-0.9.0.tar.gz"
curl -fLO "https://github.com/ImL1s/oh-my-grok/releases/download/${TAG}/SHA256SUMS"
shasum -a 256 -c SHA256SUMS
curl -fsSLo install.sh "https://raw.githubusercontent.com/ImL1s/oh-my-grok/${TAG}/scripts/install.sh"
bash install.sh --offline --archive ./oh-my-grok-0.9.0.tar.gz \
  --checksums ./SHA256SUMS --source-tag "${TAG}"
```

安裝程式在解壓前驗證、限制並拒絕 link／path 逃逸的 archive 成員、不可變暫存、交易式切換 plugin + CLI、跑 strict doctor、寫 receipt，並在啟用失敗時回滾。

## Plugin marketplace 與套件登錄

GitHub release 是 OMG 宣稱的發佈通道。xAI marketplace PR 仍屬可選，且需要該登錄當前 schema 加上精確 tag SHA。PyPI／非 editable wheel 與 npm 式套件登錄**不是** OMG 0.7.5 宣稱的發佈通道。發佈說明不要暗示否則。
