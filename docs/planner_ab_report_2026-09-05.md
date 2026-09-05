# planner A/B 实测报告（M4 数字④，2026-09-05）

> 背景：M4 给 harness 补上 planner（v2.2 §4 承诺 `runtime/plan.py` 但代码里没有，
> JD 关键词"任务规划器"接不上）。设计 = **advisory plan + agentic execution 两段式**：
> 执行前先一次规划调用，计划注入上下文当"参考脚手架"，执行仍让 LLM 每步自选工具
> （保住已打通的 self-repair，不引 LangGraph）。本报告量化验证两件事：
> ① 加了规划**不破坏 self-repair**（plan 档也要全绿）——数字④ 的前提；
> ② 规划在 T1~T3 短任务上的**真实成本**（token/步数/调用）——决定 P5 默认开关。
>
> 原始留档（真机输出）：`backend/data/measure_plan_*.md`（8 份，含规划阶段打印与全部
> 失败样本 run_id）。
>
> ⚠️ 2026-09-05 补测升级：本报告初版 no-plan 对照引用官方中位、plan 为单次样本；
> 后经**背靠背补测**（docs/planner_backtoback_report_2026-09-05.md）同批重跑，
> T2 "省 953 token"被证伪为单次运气，结论按补测修正——**以背靠背报告为准**。

## 方法

- **plan 档**：真机实测各 2 次（glm-4.5-flash，预算护栏 30k 开着，任务包跑前/跑后
  git 还原回 bug 态——A/B 起点一致）。健康期样本：
  - T1：`7afb3d9e`（冒烟）、`70ecf590`（背靠背）
  - T2：`fcc1e0b2`、`eb626777`（背靠背恢复期）
  - T3：`9067a708`、`0c939442`（背靠背恢复期）
- **no-plan 对照**：先引用官方 run_all 回归中位（`docs/run_all_report_free_2026-09-04.md`），
  后经背靠背补测同批实测确认（T1 5,239 / T2 11,787 / T3 13,394，3/3 全绿与官方中位吻合）。

## 结果（健康期完整对照，背靠背口径）

| 任务 | 档 | 全绿 | 步数 | LLM 调用 | 总 token | 规划 token |
|---|---|---|---|---|---|---|
| T1 | no-plan（背靠背实测） | ✅ | 4 | 5 | 5,239 | — |
| T1 | plan（2 次均绿） | ✅ | 5 | 7~8 | 9,091 / 9,904 | ~1.1k |
| T2 | no-plan（背靠背实测） | ✅ | 6 | 7 | 11,787 | — |
| T2 | plan（2 次均绿） | ✅ | 5 | 7~8 | 11,873 / 13,739 | ~1.0k |
| T3 | no-plan（背靠背实测） | ✅ | 6 | 8 | 13,394 | — |
| T3 | plan（2 次均绿） | ✅ | 6 | 9 | 15,617 / 17,030 | ~1.2k |

## 结论（诚实口径，面试可直接引用）

1. **plan 不破坏 self-repair：健康期 6/6 全绿** ✅（T1~T3 各 2 次，含补测复现）。
   加了规划阶段，免费 glm-4.5-flash 照样把 T1~T3 修到全绿——planner 与护栏体系不打架
   （数字④ 前提成立）。补测中出现的失败样本全部归因免费账户 429 限流高压（服务端
   质量退化），非 plan 逻辑问题——详见背靠背报告。

2. **短任务上 plan 不省 token，是纯开销**（背靠背修正）：
   - 规划调用本身 ~1.0k~1.4k token/次（固定成本）；
   - 总 token 变化（健康期）：T1 +4,665 / T2 +1,952 / T3 +3,636 ——**初版"T2 −953"是
     单次运气**，背靠背证伪。无稳定 token 收益，不承诺"规划让短任务更快"。
   - 规划产出质量：glm-4.5-flash 6/6 一次输出合法 plan JSON（无需重试）——提示词与
     Pydantic 校验（含 null 容错）适配良好。

3. **T2（需先侦察多文件再补函数）plan 稳定省 1 步**（no-plan 6 步 → plan 5 步，2/2 复现）：
   "多文件侦察型"任务计划能减少试错步数——**省步不省 token**。价值在**长任务/复杂任务**
   放大，与上下文压缩同构（压缩默认关、只在长任务开）。

4. 🔴 **plan 的隐藏成本（背靠背新发现）**：plan 档多一次规划调用 + 更长上下文 = 更高 token
   消耗 = **更容易撞免费账户限流**。同批 6 局连跑时 no-plan 3/3 全绿、plan 第 2 局起
   空转/失败——免费档跑 plan 要多承担限流风险（详见背靠背报告 §二-3）。

## P5 决策：默认关（`use_plan=False`，`--plan` 按需开）

- 短任务（4~6 步）规划是纯开销（+1 次调用 ~1k token，总 token +2k~+4.7k），无稳定收益，
  还放大免费模型限流风险 → 默认关，**数字① 回归口径（no-plan 6/6）不被默认行为改动**；
- planner 是面向长任务/复杂任务的**能力开关**，与 context_compress 并列，超长任务
  或付费模型按需开（`run_all.py --plan` / `drive_task.py --plan`）。

## 相关代码（commit cf55aa5）

- `app/runtime/plan.py`（新）：AgentPlan/PlanStep + PLAN_PROMPT + parse_plan（复用
  protocol.extract_json_object，`_coerce` 容错 null 字段省重试）+ plan_to_text
- `app/runtime/loop.py`：`use_plan` 开关 + `_plan_once()`（同一 decider 通道，planning
  tokens = meter 前后差值，产出即 save_plan → 杀在 plan phase 不重规划）
- `app/store/db.py`：`plans` 表（审计/回放）
- `scripts/measure_plan.py`（新）：A/B 实测工具
- `tests/test_planner.py`：7 用例（规划一次/坏 plan 重试与降级/resume 不重规划/
  planning tokens 入预算护栏），单测 36/36
