# CodeCraft Harness

> 给一个自然语言工程目标，AI Agent 自主完成「规划 → 写码 → 跑测试 → 失败自修 → 全绿」的多轮长任务。
> 轻量沙箱隔离、可中断续跑、可审计回放、成本有护栏。**核心全部自研**（不引 LangChain）。

定位：秋招第二作品，主打**系统设计深度**——证明"能设计 AI 系统本身的工程骨架"。
配套文档：[执行计划 v2.2](docs/执行计划_v2.2.md)（grill 三轮拷问定稿，15 项决策有出处）。

## 硬数字（2026-09-04 实测）

**数字① 回归通过率：6/6 全绿**（`glm-4-air-250414` × 3 任务 × 2 次，详见 [报告](docs/run_all_report_2026-09-04.md)）

| 任务 | 难度 | 全绿 | token 中位 | 步数中位 | LLM 调用 | 耗时 |
|---|---|---|---|---|---|---|
| T1 单测修复 | 入门 | 2/2 | 6,638 | 5 | 7 | ~11s |
| T2 补缺失函数 | 简单 | 2/2 | 14,602 | 6 | 10 | ~15s |
| T3 跨文件 bug | 中等 | 2/2 | 7,924 | 4 | 6 | ~10s |

单次 run 实际 token 5k~18k，成本约 1~3 分钱。最大单次 18,147 token（预算护栏校准基数 ≈27k）。

**MVP 底线五条进度**

| # | 底线 | 状态 | 证据 |
|---|---|---|---|
| 1 | 长任务自修到绿 | ✅ | 真 LLM 全链路 run 数十次，T1~T3 全绿 |
| 2 | 进程杀死可恢复（resume 不重放） | ✅ | kill -9 模拟（`os._exit(137)`）→ 续跑至绿 |
| 3 | 工作区漂移可识别（ws_hash） | ⏳ W3 | checkpoint 已落 ws_hash 字段，比对逻辑待激活 |
| 4 | 预算护栏（超限暂停+人工批准） | ⏳ W3 | usage 表已记账，审批流待接 |
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
- **checkpoint/resume**：每步全量存档上下文，进程被杀从断点续跑、不重放已完成动作
- **沙箱隔离**：任务包复制执行、源目录只读；`git restore` 一键重置"考卷"

## 快速开始

```bash
# 1. 环境
cd backend
python -m venv .venv && .venv/Scripts/activate     # Windows
pip install -r requirements.txt
cp .env.example .env                                # 填入 ZHIPU_API_KEY

# 2. 单任务真机自修（goal 自动从任务包 README 读）
python scripts/drive_task.py t2_missing_fn --model glm-4-air-250414

# 3. 回归表（数字①）
python scripts/run_all.py --model glm-4-air-250414 --repeat 2

# 4. 单元测试
python -m pytest tests/ -q
```

## 已知边界（踩坑记录，面试可讲）

- **模型差异是真实变量**：同一任务、同一套 harness，免费 `glm-4-flash` 连败 6 次（JSON 裸参数/引号包裹 content），付费 `glm-4-air` 一次全绿。**架构正确性靠 A/B 验证**，不靠单一模型"能跑"。
- 免费模型在多步上下文尾部输出含代码 JSON 时稳定性崩塌 → 协议演进（edit_file/纠错教学/重试 3 次）缓解，但换更强模型是根本解。
- 任务包设计影响 AI 行为：包装函数会诱使整文件覆盖误删代码 → 待补函数用单函数文件。

## 里程碑进度

| 周 | 里程碑 | 状态 |
|---|---|---|
| W1 | M0：骨架/沙箱/loop/协议/trace/checkpoint | ✅ Day1~4 |
| W2 | M1：T1~T3 + pytest 解析 + 端到端闭环 | ✅ Day5~6（数字① 6/6） |
| W3 | M2：漂移识别 + 预算护栏 + 审批流 | 进行中 |
| W4~5 | M3：压缩开关（数字③）+ 杀 N 次测恢复率（数字②）+ 多模型评估 | 待 |
| W6~8 | M4：打磨 + run_all 固化 + 简历口径 + 面试预演 | 待 |
