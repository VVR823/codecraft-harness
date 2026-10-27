# 评测结果与证据边界

本文汇总 CodeCraft 当前可复现的工程验证和模型评测，避免把不同源码版本、不同模型或不同任务的结果混在一起。

## 当前工程门禁

当前公开源码对应的本地门禁完成于 2026-10-27：

| 检查 | 结果 |
| --- | --- |
| pytest | 412 passed，1 skipped |
| 覆盖率 | 88.93% |
| Ruff | 通过 |
| 格式检查 | 113 个文件通过 |
| mypy | 14 个模块通过 |
| Node 测试 | 8 passed |

跳过项是 Windows 符号链接能力差异；对应 Linux 路径已有单独验证。最新 GitHub Actions 同时运行 Windows、Ubuntu 和容器任务，工作流状态以仓库首页徽章和 [Actions 页面](https://github.com/VVR823/codecraft-harness/actions/workflows/ci.yml)为准。

这些结果证明当前实现通过既定工程检查，不证明模型能够稳定修复任意仓库。

## 离线演示

`offline_demo` 使用固定、公开的决策脚本运行 T1/T2/T3。它用于验证以下工程链路：

- 源码复制和原测试冻结；
- 工具调用、trace 与 checkpoint 持久化；
- 修改后的测试验证；
- 结果查询和补丁导出。

离线演示不调用模型，token 为 0。它不能作为真实模型修复成功率。

## 本地模型结果

### 固定小任务

2026-10-20 的历史固定集在同一 qwen2.5 7B 低内存配置下完成 T1、T2、T3 各一次，三个补丁均在全新任务副本中重放并通过原测试。总计 22,521 个已知 token、0 次未知用量。机器可读记录见 [`local-7b-fixed-suite-2026-10-20.json`](evidence/local-7b-fixed-suite-2026-10-20.json)。

这只是 3 个内置小任务各一次的结果，不是稳定成功率估计。

### 新版固定集

2026-10-25 的新版固定集完成 2/3；T3 达到预算后暂停。远端 GitHub 工具使用固定控制器在专用夹具仓库完成了分支推送和 PR 验证，但这不等于模型可以自主完成 PR 交付。机器可读记录见 [`real-acceptance-2026-10-25.json`](evidence/real-acceptance-2026-10-25.json)。

### 复杂真实仓库任务

2026-10-27 使用本地 7B 代码模型测试较复杂的恢复缺陷。运行能够读取失败测试和目标实现，但在有限预算内反复搜索，最终没有生成有效补丁，状态为 `budget_paused`、`verified=false`。该失败结果保留，不与小任务结果合并，也不标记为成功。机器可读记录见 [`complex-repository-2026-10-27.json`](evidence/complex-repository-2026-10-27.json)。

## 容器与公开交付

- Docker 镜像以非 root 用户运行，支持健康检查、离线任务、补丁导出和数据卷持久化。
- 容器重建后可以从持久卷读取原运行；旧数据库不会在启动时静默迁移。
- GitHub Pages 仅展示静态案例，不连接任务 API，不接收源码或密钥。
- 公网仓库不包含真实数据库、模型文件、环境变量文件或原始提示词。

容器证据见 [`container-acceptance-2026-10-21.json`](evidence/container-acceptance-2026-10-21.json) 和 [`container-resume-2026-10-21.json`](evidence/container-resume-2026-10-21.json)。

## 如何复现

在 `backend` 目录安装开发依赖后运行：

```powershell
.\.venv\Scripts\python.exe scripts/check.py
.\.venv\Scripts\python.exe -m app.bench --mode offline_demo --data-dir .\data-bench --output .\reports\offline
```

真实模型评测必须显式使用 `--mode local`，并提供仅绑定 loopback 的 OpenAI 兼容端点。每次评测使用新的数据和报告目录；失败尝试、未知用量和预算暂停都应保留。

## 结果解释规则

- 不把离线脚本结果写成模型能力。
- 不把单次或小样本结果写成稳定成功率。
- 不合并不同模型、源码版本或预算的成绩。
- `health=ok` 只证明模型服务已加载，不证明修复成功。
- 只有 `done`、`verified=true`、原测试通过且补丁可复查时，才将一次运行记为完成。
- 工程测试通过与模型任务完成是两个独立结论。
