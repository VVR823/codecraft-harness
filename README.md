# CodeCraft Harness

[![Offline quality](https://github.com/VVR823/codecraft-harness/actions/workflows/ci.yml/badge.svg)](https://github.com/VVR823/codecraft-harness/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

[在线案例](https://vvr823.github.io/codecraft-harness/) · [架构说明](docs/architecture.md) · [评测结果](docs/evaluation.md)

CodeCraft Harness 是一个面向可信 Python 项目的单 Agent 代码修复工作台。模型在隔离副本中读取源码、修改实现并运行原始测试；系统负责权限控制、预算计量、断点恢复、结果审计和补丁导出。

项目关注的不是“让模型生成一段代码”，而是代码修复任务中更难处理的工程问题：如何保证原测试不可篡改，如何在进程中断后判断工具是否执行，如何限制模型成本，以及如何把一次修复变成可以复查和重放的交付物。

## 核心能力

- 每次运行使用独立的 `workspace / baseline / original` 三份文件，分别承载可修改源码、冻结测试和初始版本。
- `done` 必须满足结构化完成条件：原测试通过、至少执行一项测试，且验证晚于最后一次有效修改。
- 模型调用先记录意图，再结算已知 token；用量未知或达到预算时暂停，不按零成本处理。
- 工具动作、trace、checkpoint 和运行状态事务化保存；副作用不确定时不自动重放。
- 支持显式恢复、有限预算扩额、工作区漂移检测、失败编辑熔断和脱敏补丁导出。
- CLI、FastAPI 和原生 JavaScript 控制台共用同一运行服务。
- 可选接入 OpenAI 兼容的本地模型、MCP 工具、Skills、planner 和长期记忆；默认离线模式不调用模型。

## 运行流程

```text
CLI / Web
   │
RunService ── 文件锁 + SQLite 状态机
   │
HarnessLoop ── 模型决策 / 离线演示决策
   │
ToolRegistry ── 读写源码 / pytest / 可选 MCP
   │
动作记录 + trace + checkpoint + 补丁导出
```

详细的数据边界、事务窗口和停止条件见[架构说明](docs/architecture.md)。

## 快速开始

需要 Python 3.13。以下命令不会调用模型，也不需要 API Key。

```powershell
cd backend
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --require-hashes -r requirements.txt
.\.venv\Scripts\python.exe scripts/drive_task.py t1_single_fix --mode offline_demo --data-dir .\data-demo
```

成功时输出包含 `status=done verified=True`。修改发生在输出所示的私有工作区，内置源任务保持不变。

启动本地控制台：

```powershell
$env:CODECRAFT_DATA_DIR = '.\data-demo'
$env:CODECRAFT_MODE = 'offline_demo'
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1 --no-access-log
```

浏览器打开 `http://127.0.0.1:8000/console/`。Linux 将解释器路径替换为 `.venv/bin/python`。完整配置、恢复、导出和 Docker 命令见[操作指南](docs/operations.md)。

## 工程验证

截至 2026-10-27，当前源码的本地完整门禁结果为：

| 项目 | 结果 |
| --- | --- |
| Python 测试 | 412 passed，1 skipped |
| 覆盖率 | 88.93% |
| 静态检查 | Ruff、113 个文件格式检查、14 个 mypy 模块通过 |
| 前端测试 | Node 测试 8 passed |
| 云端流水线 | Windows、Linux、容器任务通过 |

真实模型结果与工程门禁分开记录：固定小任务曾在同一 7B 配置下各完成一次并完成补丁重放；新版固定集为 2/3；更复杂的真实仓库恢复任务在预算内未生成有效补丁。这里不把小样本结果写成稳定成功率，也不把离线脚本演示写成模型能力。完整口径和证据索引见[评测结果](docs/evaluation.md)。

## 开发与部署

安装开发依赖后，在 `backend` 目录运行统一门禁：

```powershell
.\.venv\Scripts\python.exe -m pip install --require-hashes -r requirements-dev.txt
.\.venv\Scripts\python.exe scripts/check.py
```

离线评测：

```powershell
.\.venv\Scripts\python.exe -m app.bench --mode offline_demo --data-dir .\data-bench --output .\reports\offline
```

Docker 模板默认只映射到 `127.0.0.1:8000`，数据保存在命名卷中：

```sh
docker compose up --build
```

## 项目边界

- 离线模式是确定性工程演示，不代表模型可以理解任意需求。
- 本地模型结果受模型、上下文、源码版本和预算影响，不承诺任意任务成功。
- Python 测试仍以宿主用户权限执行；目录复制、环境白名单和超时清理不是恶意代码安全沙箱。
- 项目定位是可信操作者使用的单机工具，不提供匿名公网代码执行服务，也不是多租户 SaaS。
- GitHub 推送和 PR 工具会产生真实远端副作用，只应在干净、独立且明确授权的仓库副本中使用。

## 文档

| 文档 | 内容 |
| --- | --- |
| [架构说明](docs/architecture.md) | 运行循环、工作区、事务窗口、预算与安全取舍 |
| [操作指南](docs/operations.md) | 本地模型、恢复、导出、配置、API 和 Docker |
| [测试指南](docs/testing.md) | 本地门禁、功能评测和 CI 验证方式 |
| [评测结果](docs/evaluation.md) | 当前工程质量、真实模型结果和证据边界 |
| [低内存模型](docs/low-memory-local-model.md) | 在 CPU 环境连接已有本地 7B 服务 |
| [静态案例](showcase/README.md) | 不连接后端的只读案例页面 |

技术栈：Python、FastAPI、SQLite、Pydantic、pytest、原生 JavaScript、Docker、GitHub Actions、MCP。

MIT License，见 [LICENSE](LICENSE)。
