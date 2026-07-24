# Autopilot 用法（skill + CLI）

English | [简体中文](./autopilot.zh.md) | [繁體中文](./autopilot.zh-TW.md)

English | [简体中文](./autopilot.zh.md) | [繁體中文](./autopilot.zh-TW.md)

**对象：** 使用 Grok Build 的人 + 维护 skill 的人。  
**版本：** 与 [`plugin.json`](../plugin.json) 一致（目前 **0.6.0**）。
**Skill 原文：** [`skills/omg-autopilot/SKILL.md`](../skills/omg-autopilot/SKILL.md)

---

## Autopilot 是什么

| 元件 | 做什么 |
|------|--------|
| **Skill `omg-autopilot`** | Session 内 playbook：厘清 → 计划 → 写码 → 审查 → QA → accept |
| **CLI `omg autopilot *`** | 严格 phase 状态机 + 目的地闸门；run 状态在 `.omg/state/runs/<run_id>/` |
| **Workers** | 只透过 Grok `spawn_subagent`（depth 1）；实作者 `capability_mode=read-write`（无 shell） |

**Grok 上没有：** OMC 式 Stop `decision:block`（无法强制 chat 不结束）。  
**持久化：** 再呼叫 skill / 说“继续”，或外层 `omg ralph "…"`。

---

## 何时用

**适合：**

- 多阶段：需求 → 计划 → 实作 → 审查 → QA → verified  
- 你说 *autopilot*、*full auto*、*build me*、*handle it all*、*帮我做完*  
- 想用一个 coordinator skill，而不是自己串所有 CLI  

**改用别的：**

| 情境 | 改用 |
|------|------|
| 极小修正 | 直接改，或 `omg-ralph` 单一 story |
| 只要计划 | `omg-ralplan` |
| 只要平行 | `omg-ultrawork` / `omg ulw` |
| 中止 | `omg-cancel` / `omg cancel` |
| 只是脑力激荡 | 聊天即可，不要开 autopilot run |

---

## 怎么开始

### A. 在 Grok Build 里（推荐）

1. 专案已跑过 `omg setup`，`omg doctor` hard 检查通过。  
2. 呼叫 skill：  
   - 自然语言：`autopilot 完成 …` / `full auto: …`  
   - 或：`/oh-my-grok:omg-autopilot` + 目标  
3. 让 agent 跑 CLI + workers。若 turn 中断：  
   - 说 **继续 / continue**  
   - 或：`omg autopilot status --run <RUN>` 后再呼叫 skill  

### B. 纯终端机 CLI

```bash
omg doctor
omg autopilot start "完成功能 X"
# 需求已定：
omg autopilot start "完成功能 X" --skip-interview

RUN=…   # start 回傳的 run_id

omg autopilot transition --run "$RUN" --phase ralplan \
  --evidence-json '{"interview_complete":true}' --reason "interview closed"

omg autopilot transition --run "$RUN" --phase implement \
  --evidence-json '{"consensus":true}' --reason "ralplan APPROVE"

omg autopilot transition --run "$RUN" --phase review --reason "impl ready"
# omg review …
omg autopilot transition --run "$RUN" --phase qa --reason "review clean"
# omg qa freeze / run …
omg autopilot transition --run "$RUN" --phase acceptance --reason "ultraqa clean"
omg autopilot complete --run "$RUN"
omg autopilot status --run "$RUN"
```

非法 transition 会 fail closed。

---

## Phase 状态机

```text
interview → ralplan → implement → review → (rework) → qa → acceptance → verified
```

另有 `blocked`、`cancelled`。

| 进入 | 需要的证据 / 章 |
|------|-----------------|
| `ralplan`（从 interview） | `interview_complete: true` |
| `implement` | `consensus: true` |
| `qa` | `stages/structured_review.json` clean |
| `acceptance` | `stages/ultraqa.json` status=`clean` |
| `verified` | **只能** `omg autopilot complete`（不可 `transition … verified`） |

**QA clean ≠ verified。** UltraQA 永不设 `verified`。

---

## Skill playbook（agent 应做的）

| 阶段 | Skill / tools | CLI |
|------|---------------|-----|
| Bootstrap | — | `omg doctor`、`setup`、`autopilot status` |
| interview | `omg-deep-interview` | `omg interview *` → transition `ralplan` |
| ralplan | `omg-ralplan` + critic/verifier **read-only** | transition `implement` |
| implement | `omg-ultrawork` / `omg-ralph` + executor **read-write** | transition `review` |
| review | `omg-dual-review` 或 `omg review` | clean → `qa`；否则 `rework` |
| qa | `omg-ultraqa` | freeze → run → clean → `acceptance` |
| acceptance | — | `omg autopilot complete`（优先） |
| cancel | `omg-cancel` | `omg cancel` |

### Spawn 硬规则

1. 只经 `spawn_subagent`（depth 1）。  
2. 一律设 `capability_mode`。  
3. 被 deny 缺 mode → **立刻重试** 并补上。  
4. 预设 worker 不用 claude/codex/omc team/agy/cursor-agent。  
5. 不手写 `verified`。

### UltraQA freeze（v0.3.2+）

```bash
omg qa freeze --run "$RUN" --scenarios-json \
  '[{"id":"unit","command":"python3 -m pytest -q -m '"'"'not live'"'"'"}]'
omg qa run --run "$RUN"
```

Clean 后 **`prd.json` 可省略** — accept/complete 会从 scenarios materialize（不覆盖既有 operator PRD）。

### Complete / short-circuit（v0.3.2+）

```bash
omg autopilot complete --run "$RUN"
# 若已 omg accept --yes 成功，complete 只同步 phase，不再整輪重跑測試
omg autopilot status --run "$RUN"
# 期望：phase=verified、run_status=verified、autopilot_phase=verified
```

---

## Repository workflow 是另一层

若团队要保存、review、版本化固定 stage graph，请用
`omg workflow install|list|show|plan|run`。Autopilot 可以依 plan 用 Grok 原生
`spawn_subagent` 执行，但不可改写 contract 或捏造 receipt。Workflow 的
`ship` 也不能取代 `omg accept` 或 release state machine。详见
[workflows.zh-TW.md](./workflows.zh-TW.md)。

Grok `/create-workflow` 与 Rhai projection 目前仍是 `optional_unclaimed`；只有
help 文字或本地 `.rhai` 档不能当成已验证 native integration。

## 相关 skills

| Skill | 角色 |
|-------|------|
| `omg-using` | 路由 |
| `omg-deep-interview` | 需求 |
| `omg-ralplan` | 计划共识 |
| `omg-ultrawork` | 平行实作 |
| `omg-ralph` | 单 story 坚持 |
| `omg-dual-review` | 审查 |
| `omg-ultraqa` | QA |
| `omg-ultragoal` | 多 story ledger |
| `omg-cancel` | 中止 |

完整 15 个 skill：[`skills.zh-TW.md`](./skills.zh-TW.md)。

---

## 反模式

- 没有 CLI 章就说“做完”  
- `transition --phase verified`  
- 用假 evidence 跳过 interview/ralplan  
- 实作完 self-approve  
- 无限 skill 自循环（先 status + 让使用者 continue）  
- 把外部 agent CLI 当 worker  
- 宣称 Stop hook 会锁住 session  
- freeze 用 `grep` / `python -c` / `omg` 当 argv0  

---

## 状态目录

```text
.omg/state/runs/<run_id>/
  status.json
  stages/autopilot.json
  stages/structured_review.json
  stages/ultraqa.json
  prd.json                 # 可選；可從 ultraqa materialize
  acceptance.*
```

---

## 安全

主隔离：`capability_mode` + agent disallowed tools。  
Acceptance / QA：`command_policy`（操作者意图闸，不是 OS sandbox）。  
详见：[`security-model.md`](./security-model.md)（英文）。

---

## 快速指令

```bash
omg autopilot start "goal"
omg autopilot start "goal" --skip-interview
omg autopilot transition --run RUN --phase PHASE --evidence-json '{…}' --reason "…"
omg autopilot status --run RUN
omg accept --run RUN --yes
omg autopilot complete --run RUN
omg cancel
```
