# M5 三开关真机留档（2026-09-06）

> 目的：给 README「真机三开关全开 T1 全绿」一个**可指的证据文件**（对齐"数字能指到
> 脚本与日志"的自家标准）。对应 commit：见 README W9 行。

## Run 事实

- **run_id**: `b5c4a1e61208`（data/harness.db runs/traces 表可查）
- **命令**: `python scripts/drive_task.py t1_single_fix --skills --mcp --memory`
- **任务**: T1 单测修复（`trim_whitespace` 只去空格 bug，1 测试红）
- **模型**: config 默认（glm-4.5-flash 免费）
- **结果**: `done`，6 步，全绿 `2 passed`，耗时 ~102s
- **MCP**: server `demo` 已连，注册 1 个工具 `sqlite_query`（进注册表 mcp_demo__sqlite_query）
- **Skills**: pytest-green 命中注入（goal 含"让测试全绿"）
- **记忆**: run 结束沉淀 1 条 `success_path`（t1_single_fix）——内容含真实改动
  `utils.py: strip(" ") → strip()`（trace 的 args_summary 未截断前）

## 步骤回放（traces 表）

| step | 动作 | 说明 |
|---|---|---|
| 1 | run_tests | 先跑测试拿结构化失败（不猜） |
| 2-3 | read_file ×2 | 读 test_utils.py + utils.py 看现场 |
| 4 | edit_file | 精准替换 bug 行（old→new 片段） |
| 5 | run_tests | 验证 → 2 passed 全绿 |
| 6 | done | 收工（护栏强制：全绿才允许 done） |

## 口径（诚实说明）

- 这是一次**能力冒烟**（三模块开关同时开不破坏 self-repair），不是数字① 那样的多样本
  回归——数字① 的 6/6 是裸 harness + 四护栏跑的，M5 三模块默认关，不掺基线口径。
- 单次 run 存在单样本运气，不做推广结论；三模块各自的机制单测（11+10+8）与隔离后的
  85/85 才是机制正确性的主证据。
