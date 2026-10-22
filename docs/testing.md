# CodeCraft验证方式与证据

所有核心回归使用临时数据库、私有工作区、固定决策器或模型MockTransport，质量门禁不调用模型。后续单独授权的本地模型尝试见[真实验收](local-acceptance-2026-10-20.md)；历史公开源码/静态页与云CI见[公开交付](cloud-publication-2026-10-21.md)。本轮未调用远端模型；修复随本次源码更新同步。离线演示与真实模型评测必须分开解读。

2026-10-22全面自检：11处问题已复现并修复，Windows完整门禁332PASS/1SKIP、81.02秒、87.69%；Linux333PASS/0SKIP、48.10秒、87.80%；两者均无告警，Ruff、102文件格式和14模块mypy通过，原覆盖率门槛未变。JS8PASS，Windows现有官方MCP参考server握手/list/中文echo/get-sum通过。真实Edge认证、三项离线任务、下载补丁原始副本2/4/5PASS及人工预算恢复通过。详情与限制见[自检报告](self-audit-2026-10-22.md)。以上为本地验收结果；本次同步触发的云CI以工作流结果为准，未做真实模型试跑。

2026-10-21历史Linux门禁：在源码白名单副本和独立Linux卷执行scripts/check.py，287PASS/0SKIP、45.91秒、覆盖率88.71%，Ruff/97文件格式/14模块mypy通过。真实Compose构建、非root、health/ready、离线任务/导出、保卷换容器以及Windows HTTP访问均通过。日志`.cache/optimization/container-linux-20261021/`；[完整范围与失败处置](container-validation-2026-10-21.md)。这是本地Linux容器实测，不是云CI或真实模型评测。

2026-10-20历史协议门禁：281PASS/1SKIP、73.53秒、覆盖率88.56%，Ruff/97文件格式与14模块mypy通过。4项新SDK内存传输回归验证新动作历史与完成示例均符合决策封套，覆盖普通/Schema模式和预算暂停后恢复；历史格式RED4FAIL、完成示例RED4FAIL，最终关联25PASS。日志在`.cache/optimization/decision-history-20261020/`，最终以final-check.log及退出0为准；此前check.log对应尚未修正完成示例的阶段。

上一轮相同内容写入修复为277PASS/1SKIP、88.52%；其回归覆盖不触碰文件、全绿后保持验证、服务重启/断点恢复，以及实际修改仍要求重测，先6FAIL/2PASS，再关联29PASS。日志在`.cache/optimization/noop-verification-20261020/`。初次测试夹具误用offline自带目录的失败保留，修正为无网络执行的local配置后才取得有效RED。

此前原计划交付核对为269PASS/1SKIP、88.51%、JavaScript6PASS。只读案例页4个案例桌面和375px实测通过，无页面错误或任务API请求。日志与截图在`.cache/optimization/plan-closeout-20261020/`；本轮未修改或重跑前端。原计划逐项结论见[验收矩阵](acceptance.md)。

后续独立复核最新真实T1：只读账本与基线核对通过；新副本原测试1PASS/1FAIL，直接应用导出补丁后2PASS，无手工修改。未调用模型或改动原run。只读页新增该成功记录后，5案例Edge桌面/375px检查通过，无页面错误或任务API请求；最新脚本/结果/截图在`.cache/optimization/acceptance-audit-20261020-193633/`。后端源码未变，281项门禁未重复执行。

## 本地门禁

在backend目录安装带哈希的开发依赖，运行统一入口：

```powershell
.\.venv\Scripts\python.exe -m pip install --require-hashes -r requirements-dev.txt
.\.venv\Scripts\python.exe scripts/check.py
```

入口依次执行ruff lint、format check、mypy和pytest-cov，遇到失败退出。`--unit-only`只执行测试与覆盖率。pytest仅收集tests目录，任务包里的故意失败测试由工具在临时副本运行。测试自动把数据库切换到临时库；不能用旧历史报告代替当前结果。

依赖唯一来源是pyproject.toml+uv.lock；requirements两个文件由uv export生成，不手工编辑。变更依赖后运行`uv lock`、`uv sync --locked --group dev`，然后导出：

```sh
uv export --locked --no-dev --no-emit-project --format requirements-txt --output-file requirements.txt
uv export --locked --group dev --no-emit-project --format requirements-txt --output-file requirements-dev.txt
```

首次覆盖率为1970/2293=85.91365023986044%，门槛按该实测值设置，不降低以换取通过。mypy检查本轮14个模块，含运行配置、模型边界、工作区、执行锁、运行服务、存储、评测、导出、API、安全与日志；并未宣称全部历史模块已达到strict类型检查。

## 功能评测

```powershell
.\.venv\Scripts\python.exe -m app.bench --mode offline_demo --tasks t1_single_fix,t2_missing_fn,t3_cross_file --data-dir .\data-bench --output .\reports\offline
.\.venv\Scripts\python.exe scripts/smoke.py --data-dir .\data-smoke
```

报告含schema_version、Git版本、源码摘要、完整attempts、首次通过与显式重试后通过、已知用量、未知调用、总耗时和工具次数。默认不重试；只有操作者显式设置`--retries 1..2`才从新副本增加尝试。失败尝试不会从成本和分母中删除。

`scripts/run_all.py`是同一评测入口。`measure_plan.py`/`measure_compress.py`分别默认开启对应选项，每次只生成明确的一档报告；比较时分别运行基线与开关档，不再隐式发起两批请求。`measure_compress_scaling.py --output <json>`仅计算合成文本的本地估算，标记local_estimate，不代表API token或“压缩后仍修复成功”。

真实模型评测仅显式local模式；没有已配置端点返回skipped/退出2。首次本地qwen2.5:7b/T1尝试在请求阶段暂停，1次用量未知、0次工具调用，不能据此判断模型修复能力或token节省率，详见[验收记录](local-acceptance-2026-10-20.md)。

异常诊断回归使用内存MockTransport和Fake客户端覆盖超时、连接失败、HTTP 500、响应处理异常、其他异常和用量缺失；验证固定分类持久化、敏感正文不入结果/数据库、单次调用以及同步/后台恢复拒绝时不丢失诊断。它们不连接Ollama或任何远端模型，不能替代真实模型验收。

2026-10-20追加诊断修复后，完整门禁252PASS/1SKIP、覆盖率88.33%，Ruff/格式检查及14模块mypy通过；日志在`.cache/optimization/diagnostic-20261020-110922/full-check.log`。这是追加6项回归后的新结果，与最初246PASS/1SKIP的工程交付记录分别保留。

提示边界另有`test_repair_boundary_guidance.py`：验证首次决策包含原测试/配置只读边界，以及edit_file/write_file改测试被拒绝后，反馈能引导读取并修改实现文件，通过原始冻结测试。用例中的Fake只验证提示传递和保护，不计入真实模型修复成功证据。

该修复后完整门禁255PASS/1SKIP、116.91秒、覆盖率88.34%，Ruff/94文件格式及14模块mypy通过，日志在`.cache/optimization/prompt-boundary-20261020-113210/full-check.log`。随后真实3B/T1独立复验仍未通过：5次已完成调用8935 token，另1次超时用量未知，无成功写入；不能用离线门禁代替此失败结果。

结构化输出开关的`test_structured_output.py`新增13项：默认关闭与API字段、local模式约束、CLI/评测传参和恢复拒绝覆盖、实际SDK MockTransport的Schema请求、默认/远端格式不变、离线显式拒绝、规划隔离、配置保存恢复、HTTP400不降级重发。最终完整门禁268PASS/1SKIP、63.30秒、88.47%，Ruff/95文件格式及14模块mypy通过，日志`.cache/optimization/structured-output-20261020-171204/final-check.log`。随后一次真实3B/T1仍预算暂停、无成功编辑；离线门禁与真实效果继续分开。

## 已覆盖的故障

| 场景 | 回归位置 |
| --- | --- |
| 恢复累计用量、未知usage、有限扩额 | test_usage_accounting、test_run_service |
| 无验证done、零测试、写后未验证、存plan无断点 | test_completion_contract、test_planner |
| 改测试/路径越界/语法失败/环境密钥与输出上限 | test_workspace、test_test_integrity、test_sandbox_boundaries |
| 工具后提交前崩溃、SQL事务回滚、跨进程互斥、远端超时不重放 | test_action_recovery、test_execution_lock、test_runtime_acceptance、test_uncertain_tools |
| 独立副本、API/CLI一致、持久错误、legacy | test_run_service、test_cli、test_service_api |
| 鉴权、速率、容量、body/分页界限、日志脱敏 | test_api_security、test_logging |
| 完整尝试和失败成本、导出允许列表/锁/漂移 | test_benchmark_contract、test_export_run |
| 健康/就绪区分、迁移备份、离线冒烟 | test_deployment_contract |

JS行为测试在仓库根执行：

```sh
node --test backend/ui/tests/console.test.mjs backend/ui/tests/audit.test.mjs showcase/tests/ui.test.mjs
```

真实Edge浏览器验收在本机临时后端完成：离线提交→done/verified→下载、桌面与375px布局；HTML注入文本与预算25的表单使用明确模拟的API状态验证，实际预算/恢复语义由服务测试验证。独立静态案例页只加载本目录JSON，未请求后端。浏览器日志与截图在本地`.cache/optimization/E3/`，不纳入Git。内置CUA因权限路径启动失败，使用已有Playwright和Edge，无安装新浏览器。

## 官方MCP子集回归

本机已有参考server时执行：

```sh
python scripts/verify_mcp_real.py
```

此前Windows使用已安装的`@modelcontextprotocol/server-everything@2026.8.31`，握手、13工具发现、echo与get-sum=42通过。CI安装同一版本，核心pytest不依赖Node或该server。2026-10-21本地Docker/Linux与云CI均有独立证据，云CI包含Windows/Linux官方Node/MCP。2026-10-22当前修复后Windows已复验同一现有参考server，未安装新依赖；当前Linux仅复验Python门禁与自研MCP回归，未重复官方Node/MCP。

## 本轮证据与未执行项

阶段日志按`.cache/optimization/R1`至`E5`保留；最新次数和最终审查结论见[交付记录](delivery-2026-10-19.md)及本地进度账本。清洁Windows venv仅装运行依赖时27项SDK/Mock/运行服务回归通过。Windows符号链接SKIP历史保留；2026-10-21已在Linux实跑该用例，完整287PASS、无SKIP。

最新本地3B/T1单次真实验收已done/verified=true，原2测试通过、预算内完成；详见[真实记录](local-acceptance-2026-10-20.md)。这不代表其他任务或模型均通过。原计划逐项状态见[验收矩阵](acceptance.md)。未执行：任何远端模型、真实GitHub PR交付工具验收，以及本轮修复后的真实模型复测。Docker完整构建/卷重建、Linux与公网源码/静态页已有2026-10-21历史证据；本轮容器复验范围以自检报告为准。进程级复制执行不隔离恶意Python；上线或接收不可信代码需要另外设计操作系统级隔离。

运行与恢复操作见[operations.md](operations.md)，机制与取舍见[architecture.md](architecture.md)。
