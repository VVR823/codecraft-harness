# CodeCraft 工程交付 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把可靠运行时交付成其他人能安装、体验、使用和核查结果的零新增费用应用。

**Architecture:** 依赖与质量检查本地可运行，CLI/API共享运行服务；原生控制台消费持久化结果；静态只读展示消费脱敏案例文件，不连接运行后端。Docker仅为部署方式。

**Tech Stack:** R1–R8 产物、pyproject与锁文件、ruff/mypy/pytest-cov、原生JS、标准库导出脚本、Docker。

**Spec:** `../specs/2026-10-19-engineering-hardening-design.md`。

## Global Constraints

继承 [总计划](2026-10-19-engineering-hardening.md) 的所有约束。依赖版本从官方文档/包索引核实后锁定，不凭记忆选版本。不启用付费服务或远端模型。所有验证有本地入口，不能靠推送触发云CI来替代本轮验证。不发布站点。

## Review Focus

- 清洁安装遗漏延迟导入的SDK：E1导入与mock请求测试覆盖。
- Authorization/模型错误混入日志或导出：E2/E4显式注入标记密钥检查所有输出。
- 远端响应注入HTML、轮询错run：E3用恶意字符串和切换run场景验证。
- 重试后报告漏掉失败成本、离线冒充真实质量：E4检查原始尝试与evidence_kind。
- 容器重建只有DB没有工作区：E5持久卷包含完整runs目录，验证恢复现场。

## E1：可复现安装与质量门禁

**Files:** 新建 `backend/pyproject.toml`、锁文件、`backend/requirements-dev.txt`、`backend/scripts/check.py`；修改 `backend/requirements.txt`、`.github/workflows/ci.yml`、`.dockerignore`；新增安装合同测试 `backend/tests/test_dependency_contract.py`。

**Interfaces:** `python scripts/check.py [--unit-only]` 为本地统一质量入口，按lint/format/type/pytest-cov顺序执行并传播失败退出码；requirements为锁文件的兼容安装入口，依赖来源仅一份。

```python
def test_declared_model_dependencies_are_importable():
    import openai
    import jsonschema
    assert callable(openai.OpenAI)
    assert callable(jsonschema.validate)
```

- [x] 写依赖合同测试：SDK/jsonschema均声明且可导入；运行依赖不依赖开发环境；锁定输入与产物一致；mock模型响应可通过客户端解析。
- [x] Run: `PY -m pytest tests/test_dependency_contract.py -q`。Expected: 当前缺元数据/锁文件时FAIL，不能因真实模型无key而FAIL。
- [x] 建立运行/开发依赖分组，使用验证过的免费锁定工具生成Windows/Linux Python3.13可解析依赖，不freeze无关环境；固定MCP官方server测试版本。
- [x] 配置ruff与mypy，先对本轮模块与公共边界执行有效类型检查，不使用全局ignore_errors；清理真实问题，必要格式调整与逻辑修改分开记录。
- [x] Run: 在D盘新临时venv按锁安装，执行SDK导入和mock链路；随后运行check.py。Expected: 安装及检查退出0。记录首次覆盖率并设置不低于该基线的CI门槛，不预写未经实测百分比。
- [x] CI复用本地命令，Windows/Linux矩阵配置与本轮实际运行结果区分；不push触发云端任务，不提交。

## E2：API鉴权、限流、输入边界与日志

**Files:** 新建 `backend/app/api_security.py`、`backend/app/logging_config.py`；修改 `backend/app/main.py`、R1配置、LLM/loop工具相关print、`docker-compose.yml`；测试 `backend/tests/test_api_security.py`、`backend/tests/test_logging.py`。

**Interfaces:** API_TOKEN可选；`configure_logging(format: str)`；`RateLimiter(limit=5, window=60, max_clients=10000, idle_ttl=120)`；`create_app`接受注入时钟。API进程默认127.0.0.1，生产多worker不在支持范围。

```python
def test_api_requires_configured_bearer(secured_client):
    assert secured_client.get("/api/runs").status_code == 401
    response = secured_client.get("/api/runs", headers={"Authorization": "Bearer test-only-token"})
    assert response.status_code == 200
```

- [x] 写失败用例：API_TOKEN配置后/api未认证401，正确Bearer通过；鉴权失败不能启动run；POST第6次429/Retry-After；时钟推进恢复；10000容量拒绝新客户端；忽略伪造转发头。
- [x] 写输入与日志测试：goal>20000、model>128、body>64KiB、分页越界拒绝；最多2个活动run；request_id/run_id/error_code持久化；假密钥不进入API、日志和异常结果。
- [x] Run: `PY -m pytest tests/test_api_security.py tests/test_logging.py -q`。Expected: 原API无鉴权/无界限流等FAIL。
- [x] 实现边界，使用常量时间token比较，页面token不写入URL/持久存储；限流内存有界；不放开CORS；脱敏SDK异常。Git证书验证恢复默认；测试子进程采用环境白名单并保持必要PATH/系统运行变量。
- [x] Run: 上述测试 + `tests/test_api.py tests/test_run_service.py tests/test_github_tools.py tests/test_pytest_runner.py`。Expected: PASS；子进程看不到注入的模型/API/GitHub密钥。
- [x] 更新安全边界和单worker说明，保留完整日志，无提交。

## E3：控制台可用性与静态只读案例

**Files:** 修改 `backend/ui/index.html`，按需要提取 `backend/ui/app.js`；新建 `showcase/index.html`、`showcase/app.js`、`showcase/cases.json`、`showcase/README.md`；测试 `backend/tests/test_ui_contract.py`、`backend/ui/tests/console.test.mjs`、`showcase/tests/ui.test.mjs`。

**Interfaces:** 控制台使用R7 API的mode/verified/error_code/error_message/known_tokens/unknown_calls/approvals字段和分页trace；只读页加载本目录cases.json，不连接API、不接收源码或密钥、不执行代码。

```python
def test_console_and_showcase_do_not_embed_credentials(client, showcase_text):
    assert client.get("/console/").status_code == 200
    assert "ZHIPU_API_KEY=" not in showcase_text
    assert "Authorization: Bearer" not in showcase_text
```

- [x] 添加失败用例：HTML标记按文本显示；切换run后旧请求结果不能覆盖新run；预算扩额数值传入正确；paused/interrupted/legacy/done按钮语义正确；token不进入存储和URL。
- [x] Run: `PY -m pytest tests/test_ui_contract.py -q`；根目录执行 `node --test backend/ui/tests/console.test.mjs showcase/tests/ui.test.mjs`。Expected: 旧innerHTML/错误丢失/缺模式标识时FAIL；DOM交互用真实浏览器验证，不以字符串存在替代。
- [x] 改控制台：明确离线演示/本地模型模式，提供真实可用的任务提交、有限扩额、恢复与结果入口；持久错误和分页trace可见；刷新历史与详情不产生重复任务。
- [x] 只读页展示问题、工具时间线、修改差异、测试、恢复与预算案例；案例来源和版本显式标记historical_real或offline_demo。默认使用合成可公开样例，私有历史只有经过脱敏核对才可加入。
- [x] Run: API/UI合同与JS测试，启动临时数据后端并用browse或受控浏览器检验提交→状态→暂停/恢复→结果；只读页断开后端仍可访问。Expected: 相应流程PASS，无真实模型请求。
- [x] 保存截图和步骤证据；静态产物只准备不发布。浏览器不可用时明确未验收，不伪造通过。

## E4：统一评测与结果导出

**Files:** 新建 `backend/app/bench.py`、`backend/app/export_run.py`；修改 `backend/scripts/run_all.py`、`measure_*.py` 的包装和报告；测试 `backend/tests/test_benchmark_contract.py`、`backend/tests/test_export_run.py`。

**Interfaces:** `python -m app.bench --mode offline_demo --tasks t1_single_fix,t2_missing_fn,t3_cross_file --output <dir>`；`python -m app.export_run <run_id> --output <dir> --data-dir <temp>`；真实模型评测只允许显式local模式，本轮无远端运行。

```python
def test_offline_report_preserves_evidence_type(offline_report):
    assert offline_report["evidence_kind"] == "offline_demo"
    assert offline_report["attempts"]
    assert offline_report["unknown_calls"] == 0
    assert offline_report["verified_successes"] <= len(offline_report["attempts"])
```

- [x] 写失败用例：输出schema_version/git_revision/evidence_kind/options/attempts；失败重试token/耗时进入合计；verified才计成功；unknown保留；离线报告不能标real；导出不泄露密钥和原始prompt。
- [x] Run: `PY -m pytest tests/test_benchmark_contract.py tests/test_export_run.py -q`。Expected: 旧报告仅保留最后一次尝试/缺结构化证据等FAIL。
- [x] 实现JSON+Markdown报告，分别输出首次通过与重试后通过、完整尝试数、已知用量/未知调用、耗时、工具次数；local未配置时明确跳过真实评测。导出只含相对路径补丁、脱敏结果和摘要trace，不打包.env、.git或数据库。
- [x] Run: 上述测试 + T1/T2/T3离线suite到新临时数据目录。Expected: 固定suite结果符合剧本，完整报告可解析，无远端调用；故障案例按预期失败而不删除分母。
- [x] 保存新报告路径、生成命令和代码身份。九月历史报告保持原样，不把新离线成绩替换成真实成绩。

## E5：Docker、文档和最终交付验证

**Files:** 修改 `Dockerfile`、`docker-compose.yml`、`docker-entrypoint.sh`、`README.md`；新建 `docs/architecture.md`、`docs/operations.md`、`docs/testing.md`；补充 `backend/tests/test_deployment_contract.py`、本地冒烟脚本和context快照。

**Interfaces:** 镜像默认非root、127.0.0.1端口映射、离线模式；data卷包含DB、baseline、workspace；health与ready明确区分。显式迁移入口调用R4备份迁移，不在服务启动时偷偷升级历史库。

```python
def test_health_and_readiness_are_separate(offline_client, missing_config_client):
    assert offline_client.get("/health").status_code == 200
    assert offline_client.get("/ready").status_code == 200
    assert missing_config_client.get("/health").status_code == 200
    assert missing_config_client.get("/ready").status_code == 503
```

- [x] 写失败合同：镜像排除.env/data/.git/.mcp-verify/node_modules；SDK导入与离线闭环可用；持久卷包含恢复所有必需目录；入口默认不发远端模型请求。
- [x] Run: `PY -m pytest tests/test_deployment_contract.py -q`。Expected: 旧镜像依赖.git/漏SDK/仅DB持久化等FAIL。
- [x] 更新镜像和compose，移除普通任务对.git依赖；清洁安装和首次无密钥体验写成精确步骤。补充本地模型配置、自带目录任务、导出、迁移恢复和故障边界；纠正MCP帧历史与文件落点。
- [x] Run: 全套check.py、离线suite、官方MCP本地验证、UI步骤与所有已安装依赖导入。Expected: 适用检查PASS，告警/SKIP单列。
- [x] 2026-10-21已实跑build→health/ready→离线run→保留卷重建→恢复查询；另补暂停任务重建后保持暂停、显式API resume→done/verified、原动作不重放，以及镜像SDK smoke。此前无Docker为历史环境状态，不再作为当前缺项。证据见[容器实测](../../container-validation-2026-10-21.md)、[暂停恢复报告](../../evidence/container-resume-2026-10-21.json)。未使用收费产品/服务，未触发云CI。
- [x] 最终核对：原库、用户评估文档和源任务未被改写；git diff --check通过；代码审查处理重要问题并留证。更新进度、完成/未完成矩阵、偏离设计和上下文快照，无自动提交/发布。

## 完成声明规则

只有实际运行的检查才能声明通过。静态站点准备好不等于已公开上线；本地Docker验证不等于容器沙箱；离线suite不等于真实模型成功率；书面说明不等于本人已掌握。未执行项与限制进入最终交付记录。
