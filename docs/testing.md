# 测试与评测指南

CodeCraft 将确定性的工程测试、离线演示和真实模型评测分开执行。默认质量门禁不连接模型服务，也不读取真实运行数据库。

## 本地质量门禁

在 `backend` 目录安装锁定的开发依赖：

```powershell
.\.venv\Scripts\python.exe -m pip install --require-hashes -r requirements-dev.txt
.\.venv\Scripts\python.exe scripts/check.py
```

`scripts/check.py` 依次执行 Ruff、格式检查、mypy 和 pytest-cov，任一步失败都会返回非零退出码。`--unit-only` 只运行测试与覆盖率。

测试只收集 `backend/tests`，任务包中故意失败的测试由工具在临时副本中执行。测试夹具使用临时数据库和私有工作区；模型边界由固定决策器或 MockTransport 覆盖，不需要本地模型。

当前验证结果及其适用范围见[评测结果](evaluation.md)。历史报告不能替代修改后的重新验证。

## 前端测试

控制台状态逻辑使用 Node 内置测试运行器：

```powershell
node --test ui/tests/console.test.mjs ui/tests/audit.test.mjs ../showcase/tests/ui.test.mjs
```

静态案例页只读取 `showcase/cases.json`，不应请求任务 API。浏览器验收需覆盖桌面和窄屏布局、控制台错误以及失败网络请求。

## 离线功能评测

```powershell
.\.venv\Scripts\python.exe -m app.bench `
  --mode offline_demo `
  --tasks t1_single_fix,t2_missing_fn,t3_cross_file `
  --data-dir .\data-bench `
  --output .\reports\offline

.\.venv\Scripts\python.exe scripts/smoke.py --data-dir .\data-smoke
```

报告包含源码摘要、每次尝试、已知 token、未知调用、耗时和工具次数。默认不重试；只有显式设置 `--retries 1..2` 才会在新副本中增加尝试。失败尝试不能从成本或分母中删除。

离线模式使用固定决策脚本，用于验证运行链路，不代表真实模型修复能力。

## 真实本地模型评测

真实模型只通过显式 `--mode local` 启用。端点必须是 loopback 地址；没有可用端点时返回 `skipped` 和退出码 2，不应自动切换到远端服务。

建议每次评测：

1. 使用新的数据目录和报告目录。
2. 固定模型、源码、预算、上下文和重试次数。
3. 同时检查 `status`、`verified`、原测试、已知 token 和未知调用。
4. 将导出补丁应用到全新副本并重新运行原测试。
5. 保留失败和预算暂停结果，不跨配置合并成绩。

低内存 CPU 环境的配置方法见[本地模型指南](low-memory-local-model.md)。

## Docker 验证

在 Linux 或 WSL 中运行：

```sh
docker compose build
docker compose run --rm backend smoke --data-dir /data/smoke
docker compose run --rm backend drive t1_single_fix --mode offline_demo --data-dir /data
```

验收至少包括：镜像构建、非 root 用户、`/health` 与 `/ready`、离线任务、补丁导出、数据卷重建恢复。容器离线任务仍然不是模型能力证明。

## CI

`.github/workflows/ci.yml` 在 Windows、Ubuntu 和容器环境运行质量门禁。公开状态见 [GitHub Actions](https://github.com/VVR823/codecraft-harness/actions/workflows/ci.yml)。

若本地结果和 CI 不同，先核对 Python、Node、路径语义、符号链接能力和依赖锁；不能通过降低覆盖率门槛或跳过失败步骤来制造通过。
