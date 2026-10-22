# CodeCraft Harness

[公开案例页](https://vvr823.github.io/codecraft-harness/)（GitHub Pages，只读）

一个可自部署的单 Agent 代码修复工作台：模型逐步使用工具，执行原始测试，保存动作、用量和断点，再交付可核查的修改。保留自研循环，使用 Python、FastAPI、SQLite 和原生 JS。

**零费用体验从离线演示开始。** 离线T1/T2/T3使用固定公开脚本，无密钥、无模型请求。真实任务可连接自己已有的本地模型服务，不依赖免费API额度。2026-10-20历史版本在同7B低内存配置下固定三任务各一次通过，补丁已独立复验；T2复用前次同配置结果，详见[固定集报告](docs/evidence/local-7b-fixed-suite-2026-10-20.json)。小集合单次结果不等于任意任务稳定成功；本次修复后未重跑真实模型。应用在自己电脑运行，另有独立只读案例页，不提供匿名公网代码执行。

最新本地自检（2026-10-22）：修复11处可复现问题；Windows 332PASS/1SKIP、覆盖率87.69%，Linux 333PASS/0SKIP、87.80%，均无告警；JS 8PASS。真实浏览器离线任务、刷新恢复、导出补丁回放和桌面/手机检查通过；当前源码容器非root冒烟通过，复用已有镜像依赖，未联网重建基础层。详见[自检报告](docs/self-audit-2026-10-22.md)。本次源码同步包含这些修复，对应云CI结果见[工作流](https://github.com/VVR823/codecraft-harness/actions/workflows/ci.yml)；2026-10-21云CI、完整Docker构建/保卷重建及真实模型固定集按历史版本分别保留。

历史同版通用3B固定集完成1/3；首次代码3B对照完成0/2。失败证据不覆盖，代码模型T1尚未测试，不能将不同版本的单项结果拼成当前完整集合通过。

## 三步看到第一次修复

在已获取仓库的根目录打开 PowerShell，准备 Python 3.13：

1. 安装锁定运行依赖（下载的是免费开源库）：

   ```powershell
   cd backend
   py -3.13 -m venv .venv
   .\.venv\Scripts\python.exe -m pip install --require-hashes -r requirements.txt
   ```

2. 运行不调用模型的演示，使用新的数据目录：

   ```powershell
   .\.venv\Scripts\python.exe scripts/drive_task.py t1_single_fix --mode offline_demo --data-dir .\data-demo
   ```

   预期 `status=done verified=True`。修改保存在输出的 `workspace` 路径，源任务保持不变。若提示历史库需迁移，换新目录体验，或按操作文档显式备份迁移。

3. 打开工作台：

   ```powershell
   $env:CODECRAFT_DATA_DIR = '.\data-demo'
   $env:CODECRAFT_MODE = 'offline_demo'
   .\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1 --no-access-log
   ```

   浏览器访问 `http://127.0.0.1:8000/console/`，提交任务、查看trace、下载结果。历史记录刷新不会重复创建任务。Linux使用 `python3.13 -m venv .venv` 和 `.venv/bin/python`；本地Linux容器已通过Python质量门禁，云端 CI 由 GitHub Actions 持续验证，实际状态见[工作流](https://github.com/VVR823/codecraft-harness/actions/workflows/ci.yml)。

已有 uv 的开发者可在backend执行 `uv sync --locked --group dev`。当前主要依赖已经锁定，版本与哈希以 `uv.lock` 为准。

## 这次优化补了什么

- CLI/API共用运行服务，每run独立源码副本，冻结原测试与配置。
- `done`要求结构化测试通过，并晚于最后一次成功修改；零动作、零测试不能直接完成。
- 模型调用先记意图，成功与失败成本保留；用量未知暂停，预算批准是有限扩额。
- 动作结果、trace、checkpoint同事务保存；中断窗口不自动重放未知副作用。
- 文件锁与数据库状态约束同run执行权；重启不自动续跑，旧run缺基线时只读。
- 可选Bearer鉴权、有界限流、输入限制、结构日志、依赖锁与本地质量门禁。
- 原生控制台、只读案例、JSON/Markdown评测和脱敏相对补丁导出。

普通修复不需要Git仓库。可选Skills、planner、MCP、长期记忆默认关闭；HIGH工具仍拒绝，不实现通用高危工具放行。

## 文档与验证

| 入口 | 内容 |
| --- | --- |
| [运行操作](docs/operations.md) | 本地模型、自带源码、人工恢复、配置/API、迁移、导出与Docker模板 |
| [低内存本地模型](docs/low-memory-local-model.md) | 复用已有7B权重，启动CPU服务、检查加载、显式验收及退出 |
| [架构说明](docs/architecture.md) | 执行循环、工作区、事务窗口、预算及取舍 |
| [全面自检](docs/self-audit-2026-10-22.md) | 本轮11项缺陷、实际浏览器与双平台验证 |
| [测试说明](docs/testing.md) | 本地门禁、评测命令、故障回归与证据边界 |
| [云端验证与发布](docs/cloud-publication-2026-10-21.md) | 公网案例页、源码和云 CI 入口 |
| [本轮交付记录](docs/delivery-2026-10-19.md) | 实际检查、未执行项、审查结果 |
| [原计划逐项验收](docs/acceptance.md) | 22项原始验收条件、证据与环境限制 |
| [静态案例](showcase/README.md) | 独立只读网页，不连接任务API |
| [发布来源](docs/evidence/publication-source-2026-10-22.json) | 本次源码摘要和变更清单 |

开发门禁：在backend运行 `.\.venv\Scripts\python.exe scripts/check.py`，包括ruff、格式、mypy和pytest-cov。默认离线suite：`python -m app.bench --mode offline_demo --data-dir .\data-bench --output .\reports\offline`。报告保留每次尝试，离线结果不得改标为真实模型成功率。

## 使用边界

测试会执行Python代码，只用于可信源码。复制目录、冻结测试、环境白名单和进程树超时清理能减少误操作，无法隔离恶意代码或阻断其网络访问。默认单worker、本机监听、最多2个并行run；不作为多租户或匿名SaaS部署。

真实模型修复产物与run完成状态分别记录，不承诺任意模型或任务都能成功。已实测Docker构建/持久卷重建和本地Linux Python门禁；此次未调用远端模型；公开源码和静态案例页已经发布。云 CI 实际状态见[工作流](https://github.com/VVR823/codecraft-harness/actions/workflows/ci.yml)；GitHub PR 交付工具的真实远端验收仍未执行。容器离线演示不算真实模型成绩，具体范围见逐项验收。九月报告是历史证据，原数据不覆盖；优化前README（保留在本地历史归档中）仅供历史追溯。

MIT License，见 [LICENSE](LICENSE)。

公开仓库从筛选快照更新，保留既有远端历史；未上传本机真实数据、密钥或内部恢复记录。历史模型评测按原应用源码摘要分别核对。
