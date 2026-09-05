# planner A/B 实测报告（M4 数字④，2026-09-05）

> 背景：M4 给 harness 补上 planner（v2.2 §4 承诺 `runtime/plan.py` 但代码里没有，
> JD 关键词"任务规划器"接不上）。设计 = **advisory plan + agentic execution 两段式**：
> 执行前先一次规划调用，计划注入上下文当"参考脚手架"，执行仍让 LLM 每步自选工具
> （保住已打通的 self-repair，不引 LangGraph）。本报告量化验证两件事：
> ① 加了规划**不破坏 self-repair**（plan 档也要全绿）——数字④ 的前提；
> ② 规划在 T1~T3 短任务上的**真实成本**（token/步数/调用）——决定 P5 默认开关。
>
> 原始留档（真机输出）：`backend/data/measure_plan_*.md`（3 份，含规划阶段打印）。

## 方法

- **plan 档**：真机实测各 1 次（glm-4.5-flash，预算护栏 30k 开着，任务包跑前/跑后
  git 还原回 bug 态——A/B 起点一致）。
  - T1：`7afb3d9e`（冒烟）
  - T2：`fcc1e0b2`
  - T3：`9067a708`
- **no-plan 对照**：官方 run_all 回归中位（`docs/run_all_report_free_2026-09-04.md`，
  同模型 glm-4.5-flash、同任务 bug 态、数字①口径，README/docs 已存档）——plan 是
  新增变量，no-plan 基线复用已有 6/6 全绿数据（每任务 2 次中位），不重复烧免费额度。

## 结果

| 任务 | 档 | 全绿 | 步数 | LLM 调用 | 总 token | 规划 token |
|---|---|---|---|---|---|---|
| T1 | no-plan（官方中位） | ✅ | 4 | 6 | 5,369 | — |
| T1 | plan（实测） | ✅ | 5 | 7 | 9,091 | 1,078 |
| T2 | no-plan（官方中位） | ✅ | 6 | 7 | 12,826 | — |
| T2 | plan（实测） | ✅ | 5 | 7 | 11,873 | 1,143 |
| T3 | no-plan（官方中位） | ✅ | 6 | 9 | 14,662 | — |
| T3 | plan（实测） | ✅ | 6 | 9 | 15,617 | 953 |

## 结论（诚实口径，面试可直接引用）

1. **plan 不破坏 self-repair：3/3 全绿** ✅。加了规划阶段，免费 glm-4.5-flash
   照样把 T1~T3 修到全绿——planner 与护栏体系不打架（数字④ 前提成立）。

2. **短任务上 plan 不省 token，是纯开销的下界**：
   - 规划调用本身 ~0.95k~1.1k token/次（固定成本）；
   - 总 token 变化：T1 +3,722 / T2 **−953** / T3 +955 ——单次样本下方向不收敛，
     中位看无稳定收益，不承诺"规划让短任务更快"。
   - 规划产出质量：glm-4.5-flash 3/3 一次输出合法 plan JSON（无需重试）——提示词与
     Pydantic 校验（含 null 容错）适配良好。

3. **T2（需先侦察多文件再补函数）plan 反而省步省 token**（6→5 步，−953 token）：
   单次证据，但方向合理——"多文件侦察型"任务计划能减少试错。价值在**长任务/复杂任务**
   放大，与上下文压缩同构（压缩默认关、只在长任务开）。

## P5 决策：默认关（`use_plan=False`，`--plan` 按需开）

- 短任务（4~6 步）规划是纯开销（+1 次调用 ~1k token），无稳定收益 → 默认关，**数字①
  回归口径（no-plan 6/6）不被默认行为改动**；
- planner 是面向长任务/复杂任务的**能力开关**，与 context_compress 并列，超长任务
  按需开（`run_all.py --plan` / `drive_task.py --plan`）。

## 相关代码（commit cf55aa5）

- `app/runtime/plan.py`（新）：AgentPlan/PlanStep + PLAN_PROMPT + parse_plan（复用
  protocol.extract_json_object，`_coerce` 容错 null 字段省重试）+ plan_to_text
- `app/runtime/loop.py`：`use_plan` 开关 + `_plan_once()`（同一 decider 通道，planning
  tokens = meter 前后差值，产出即 save_plan → 杀在 plan phase 不重规划）
- `app/store/db.py`：`plans` 表（审计/回放）
- `scripts/measure_plan.py`（新）：A/B 实测工具
- `tests/test_planner.py`：7 用例（规划一次/坏 plan 重试与降级/resume 不重规划/
  planning tokens 入预算护栏），单测 36/36
