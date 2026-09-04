# CodeCraft Harness

> 给一个自然语言工程目标，AI Agent 自主完成「规划 → 写码 → 跑测试 → 失败自修 → 全绿」的多轮长任务。
> 轻量沙箱隔离、可中断续跑、可审计回放、成本有护栏。**核心全部自研**（不引 LangChain）。

定位：秋招第二作品，主打**系统设计深度**——证明"能设计 AI 系统本身的工程骨架"。
配套文档：[执行计划 v2.2](docs/执行计划_v2.2.md)（grill 三轮拷问定稿，15 项决策有出处）。

## 硬数字（2026-09-04 实测）

**数字① 回归通过率：6/6 全绿**（**免费** `glm-4.5-flash` × 3 任务 × 2 次，run_all 自愈重试≤2，详见 [报告](docs/run_all_report_free_2026-09-04.md)）

> 同套 harness 用付费 `glm-4-air-250414` 也是 6/6——**证明架构正确，模型只是变量**（早期用更老的免费 `glm-4-flash` 连败，是靠护栏演进 + 换免费新代才拉满）。

**数字② 杀进程恢复率：3/3 = 100%**（T1 在 3 个不同决策点 kill -9，resume 全部续跑至全绿且不重放，详见 [报告](docs/resume_and_compress_report_2026-09-04.md)）

> 度量的是 **resume 机制可靠性**：机制性失败（漂移/预算）立即判 FAIL，LLM 偶发抖动走 ≤2 次自愈重试（与 run_all 同口径）——3/3 里无一次是机制失败。

**数字③ 上下文压缩率：12.3%，压缩且绿**（T2 真机开关对比：单步 prompt 中位 1,770→1,553 token；开压缩后仍 6 步全绿、步数/调用数与关压缩一致——**压缩不损正确性**）

> 压缩只影响 decider 视图，`self.messages` 全量存档（resume/审计不丢信息）。12.3% 是 6 步短任务的下界（近 3 步全文占大半上下文），任务越长压缩率越高。

| 任务 | 难度 | 全绿(免费) | token 中位 | 步数中位 | LLM 调用 | 耗时 |
|---|---|---|---|---|---|---|
| T1 单测修复 | 入门 | 2/2 | 5,369 | 4 | 6 | ~192s |
| T2 补缺失函数 | 简单 | 2/2 | 12,826 | 6 | 7 | ~334s |
| T3 跨文件 bug | 中等 | 2/2 | 14,662 | 6 | 9 | ~352s |

单次 run 实际 token 5k~18k，免费档成本≈0；付费 air 单次约 1~3 分钱。最大单次 18,147 token（预算护栏校准基数 ≈27k）。

**MVP 底线五条进度（2026-09-04 五条全证，②③ 已有独立真机实测）**

| # | 底线 | 状态 | 证据 |
|---|---|---|---|
| 1 | 长任务自修到绿 | ✅ | 真 LLM 全链路 run 数十次，T1~T3 全绿 |
| 2 | 进程杀死可恢复（resume 不重放） | ✅ | **M3 专项实测 3/3**：T1 杀点 2/3/4 全部续跑至全绿（`run_resume_test.py`，退出码分类 + 自愈重试）；预算 demo resume 日志"已完成 3 个动作（不重放）"复证 |
| 3 | 工作区漂移可识别（ws_hash） | ✅ | `tests/test_m2.py`：改文件后 resume 拒绝（"工作区漂移"），未改则正常续跑 |
| 4 | 预算护栏（超限暂停+人工批准） | ✅ | `scripts/demo_budget.py` 真机证据：3940 token 超 3000 预算 → budget_paused → approvals 表 requested+approved → resume 续跑至绿 |
| 5 | T1~T3 全绿 + 预算内 | ✅ | 上方回归表 |

## 架构

```
backend/
├── app/
│   ├── config.py              # 全局配置（沙箱超时/步数上限/预算）
│   ├── main.py                # FastAPI：/api/tasks 提交 → 查询 → resume
│   ├── runtime/
│   │   ├── loop.py            # 自研 agent loop：决策→执行→checkpoint→trace
│   │   ├── protocol.py        # JSON 决策协议（thought/tool/args/done）+ Pydantic 校验
│   │   └── llm.py             # LLM 薄封装（openai 兼容，智谱，可换模型）
│   ├── tools/
│   │   ├── registry.py        # 工具注册表：权限分级 LOW/MED/HIGH + 工具说明书同源
│   │   └── sandbox_exec.py    # 轻量沙箱：复制执行 + utf-8 强制 + 超时进程树强杀
│   ├── verifier/pytest_runner.py  # pytest 结构化解析（红 N 条 / 文件:行 / 断言消息）
│   ├── store/db.py            # SQLite：runs/checkpoints/traces/usage（WAL）
│   └── trace/                 # 事件 trace 记录
├── tasks/                     # T1~T3 手写任务包（module + 测试 + README，git 作还原点）
├── scripts/                   # drive_t1/drive_task/run_all/replay_run/demo_*
├── tests/                     # 19 个单元测试（含真沙箱跑任务包）
└── pytest.ini                 # 回归只收 tests/，排除任务包"考卷"
```

**设计要点**
- **决策协议**：LLM 每步输出一段 JSON（思考+工具+参数），Pydantic 校验失败自动重试并附纠错教学
- **edit_file 精准编辑**：改已有代码只输出 old→new 片段（规避整文件长 JSON 转义），write_file 仅新建；写 .py 即时语法检查
- **工具权限分级**：只读/沙箱 LOW，写工作区 MED，装包等 HIGH（审批流 W3 接）
- **结构化失败反馈**：测试红时喂给 LLM 的是 `test_orders.py:12: assert 30.0 == 27.0`，不是几十行原始日志
- **checkpoint/resume**：每步全量存档上下文，进程被杀从断点续跑、不重放已完成动作（实测 3/3）；resume 前比对工作区 hash 识别外部漂移
- **分层上下文压缩**（M3，可开关）：system+目标全文、近 3 步消息全文、更早历史每步压成一行摘要——只影响决策视图，全量消息仍存档，压缩与可恢复性正交
- **沙箱隔离**：任务包复制执行、源目录只读；`git restore` 一键重置"考卷"

## 快速开始

```bash
# 1. 环境
cd backend
python -m venv .venv && .venv/Scripts/activate     # Windows
pip install -r requirements.txt
cp .env.example .env                                # 填入 ZHIPU_API_KEY

# 2. 单任务真机自修（goal 自动从任务包 README 读）
python scripts/drive_task.py t2_missing_fn --model glm-4.5-flash   # 免费档，6/6
# 付费更强档（可选）：python scripts/drive_task.py t2_missing_fn --model glm-4-air-250414

# 3. 回归表（数字①，含自愈重试）
python scripts/run_all.py --model glm-4.5-flash --repeat 2

# 4. 韧性实测（M3）
python scripts/run_resume_test.py --task t1_single_fix --kill-points 2,3,4   # 数字② 杀进程恢复率
python scripts/measure_compress.py --task t2_missing_fn                      # 数字③ 压缩率（开关对比）

# 5. 单元测试
python -m pytest tests/ -q
```

## 已知边界（踩坑记录，面试可讲）

- **免费模型也能 100%**：早期免费 `glm-4-flash`（最老一代）在多步上下文尾部输出含代码 JSON 时稳定性崩塌（连败 6 次：JSON 裸参数/引号包裹 content/丢字段）。根因不是"模型不会修代码"，而是"把长代码塞进 JSON 字符串"这个动作。把代码移出 JSON（edit_file 只传 old→new 片段）+ 结构化失败反馈 + 强制验证 + 重复动作拦截 + 换免费新代 `glm-4.5-flash` → 拉到 6/6。
- **模型差异仍是真实变量**：同套 harness，付费 `glm-4-air` 一次全绿、免费 `glm-4.5-flash` 靠护栏 + 重试拉满——**架构正确性靠 A/B 验证**，不靠单一模型"能跑"。
- **免费最强档 `glm-4.7-flash`（30B）服务端过载不可用**：高峰期频繁 429/1305（访问量过大），agent 循环每步都调用喂不饱，放弃做默认；`glm-4.5-flash` 免费且稳健。
- 任务包设计影响 AI 行为：包装函数会诱使整文件覆盖误删代码 → 待补函数用单函数文件（已改 T2）。

## 里程碑进度

| 周 | 里程碑 | 状态 |
|---|---|---|
| W1 | M0：骨架/沙箱/loop/协议/trace/checkpoint | ✅ Day1~4 |
| W2 | M1：T1~T3 + pytest 解析 + 端到端闭环 | ✅ Day5~6（数字① 6/6） |
| W3 | M2：漂移识别 + 预算护栏 + 审批流 | ✅（底线 3/4 证据到手，MVP 五条全证） |
| W4~5 | M3：压缩开关（数字③）+ 杀 N 次测恢复率（数字②） | ✅ 2026-09-04（数字② 3/3、数字③ 12.3% 压缩且绿，commit 1e144d2） |
| W5+ | M3 余项：多模型评估 + 长任务压缩率上界复测 | 待 |
| W6~8 | M4：打磨 + run_all 固化 + 简历口径 + 面试预演 | 待 |
