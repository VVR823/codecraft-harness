# CodeCraft 运行可靠性 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 完成可验证、隔离、有限预算与可解释恢复的代码修复闭环。

**Architecture:** 从当前循环提取模式配置、工作区和用量接口；本地工具和 MCP 工具按 run 持有；SQLite 保存运行配置、调用、动作与检查点。CLI/API 最后收口到相同的运行服务。

**Tech Stack:** Python 3.13、Pydantic 2、SQLite、pytest、OpenAI 兼容 SDK、jsonschema。

**Spec:** `../specs/2026-10-19-engineering-hardening-design.md`。

## Global Constraints

继承 [总计划](2026-10-19-engineering-hardening.md) 的全部约束。工作目录为 backend；命令中的 PY 为该目录下 `.venv/Scripts/python.exe`（Linux 为对应 venv Python）。测试 TEMP/TMP 和 --basetemp 指向新的 D 盘临时目录；不得复用其他正在运行的 pytest 基目录。

不改任务源包；不运行真实 drive_task/run_all；不读取 dotenv 作为测试配置；不执行真实 GitHub 请求。需要新依赖时核对官方来源和支持版本，安装免费库不等于获准调用付费服务。

## Review Focus

- 缺 usage、请求结果未知：R1/R4 保留未知标记并暂停，不能静默记零。
- 不可变配置、baseline 和工作区来源丢失：R3/R4/R7/R8 拒绝而不猜测。
- 修改后断点未落盘：R6 interrupted，不自动重放副作用。
- 多 run 或重连共享工具闭包：R2 用不同 client 标记验证隔离。
- 原验证器错误、零测试与误收集：R5 不按“全绿”子串判定。

## R1：配置、模式与模型适配

**Files:** 新建 `app/runtime/settings.py`、`app/runtime/model_client.py`；修改 `app/config.py`、`app/runtime/llm.py`、`requirements.txt`；测试 `tests/test_runtime_settings.py`、`tests/test_model_client.py`。

**Interfaces:**
- `RunOptions(BaseModel)`：mode=`offline_demo|local|remote`，model、token_budget（默认30000，1..1000000）、context_compress、keep_recent_steps、use_plan/use_skills/use_mcp/use_memory/use_github；mode 默认 offline_demo，remote 默认不可用。
- `Settings(BaseModel)`：data_dir、base_url、api_key、allow_remote=False、model_timeout（1..600）、sandbox_timeout（1..600）；提供 `from_env()`，业务函数不直接读取全局环境。
- 环境变量统一前缀 `CODECRAFT_`：DATA_DIR、MODE、BASE_URL、MODEL、API_KEY、ALLOW_REMOTE、API_TOKEN、TOKEN_BUDGET；旧 ZHIPU_API_KEY 仅作显式remote配置兼容，不在import时自动读取dotenv。显式 --env-file 可用于开发启动，测试不用该参数。
- `ModelReply(text: str, prompt_tokens: int | None, completion_tokens: int | None, total_tokens: int | None)`；`ModelClient.chat(messages: list[dict]) -> ModelReply`；离线决策器仍可注入。

代表性断言（本任务补齐导入和测试依赖）：
```python
def test_default_options_are_offline_and_bounded():
    options = RunOptions()
    assert options.mode == "offline_demo"
    assert options.token_budget == 30_000
    with pytest.raises(ValidationError):
        RunOptions(token_budget=0)
```

- [x] 写失败用例：offline 不调用客户端工厂；local 只接受 loopback、空 key 可用；remote 未启用拒绝；预算0/负数/1000001拒绝；SDK 响应有/无 usage；SDK 重试关闭并由现有重试层管理。
- [x] Run: `PY -m pytest tests/test_runtime_settings.py tests/test_model_client.py -q`。Expected: 首次因缺失模块或接口 FAIL，不能因 socket/权限当预期 RED。
- [x] 实现上述接口；声明 SDK/jsonschema 依赖，mock SDK transport 验证真实构造与响应解析；不通过网络调用模型。保留旧 chat/chat_with_usage 包装以降低现有测试迁移风险。
- [x] Run: 同上，加 `tests/test_llm_resilience.py`。Expected: 全部 PASS；日志不含密钥；未触发任何模型网络请求。
- [x] 在进度表保存实际版本、RED/GREEN 和 diff，无提交。

## R2：工具参数与 MCP 隔离

**Files:** 修改 `app/tools/registry.py`、`app/runtime/protocol.py`、`app/runtime/loop.py`、`app/mcp/mcp_client.py`；测试 `tests/test_tool_schemas.py`、`tests/test_mcp_lifecycle.py`，更新直接关联旧测试。

**Interfaces:**
- `ToolRegistry`：`get(name)`、`describe()`、`register_mcp(server_name, client, tools)`、`close()`；实例由 run 持有。
- `ToolSpec` 增加 `input_schema: dict`；`validate_args(spec, args: dict) -> dict` 在执行前检查类型/必填/额外参数。
- MCP 工具保留完整 inputSchema；默认 HIGH，仅 allowlist 指定的只读 demo 工具为 LOW；client cmd 来自显式配置。

```python
def test_run_registries_do_not_share_handlers(reg_a, reg_b, workspace):
    # reg_a/reg_b由本测试文件分别注册返回a/b的临时FakeClient。
    assert reg_a.get("mcp_demo__ping").handler(workspace, {}).output == "a"
    assert reg_b.get("mcp_demo__ping").handler(workspace, {}).output == "b"
    reg_a.close()
    assert reg_b.get("mcp_demo__ping").handler(workspace, {}).output == "b"
```

- [x] 写失败用例：两份 registry 注册同名工具各使用自己的 client；stop 后重新注册使用新 client；启动失败清理子进程；自定义 cmd 生效；未知工具在执行前拒绝；非法 read/edit/MCP 参数被拒。
- [x] Run: `PY -m pytest tests/test_tool_schemas.py tests/test_mcp_lifecycle.py -q`。Expected: 复现全局旧闭包/缺校验或缺新接口 FAIL。
- [x] 实现实例注册表并注入 loop；协议只校验决策结构，工具名以当前 registry 为准；prompt 从当前表生成，移除静态三处白名单维护。HIGH 继续拒绝审计。
- [x] Run: 上述测试 + `tests/test_mcp.py tests/test_registry.py tests/test_llm_resilience.py`。Expected: PASS；不同 run 无残留；错误可反馈模型。
- [x] 保存结果及权限语义变化（run_tests 执行代码，不描述为无副作用），无提交。

## R3：私有工作区与冻结测试

**Files:** 新建 `app/runtime/workspace.py`；修改 `app/tools/registry.py`、`app/verifier/pytest_runner.py`、`app/tools/sandbox_exec.py`；测试 `tests/test_workspace.py`、`tests/test_test_integrity.py`。

**Interfaces:**
- `WorkspaceSnapshot(run_id, root: Path, baseline_dir: Path, manifest: dict)`。
- `prepare_workspace(source: Path, run_id: str, data_dir: Path, *, github: bool=False) -> WorkspaceSnapshot`。
- `resolve_pack(task_id: str, packs_root: Path) -> Path` 只接受已发现直接子目录；`assert_writable(snapshot, relative_path)`；`verify_baseline(snapshot) -> None`。
- `run_pytest(task_dir, *, baseline_dir=None) -> dict` 返回 ok/passed/failed/exit_code/timed_out/verified/baseline_ok，不修改源包。

```python
def test_two_runs_do_not_share_workspace(source_pack, tmp_path):
    a = prepare_workspace(source_pack, "run-a", tmp_path)
    b = prepare_workspace(source_pack, "run-b", tmp_path)
    assert a.root != b.root
    with pytest.raises(ToolError):
        assert_writable(a, "tests/test_task.py")
```

- [x] 写失败用例：同包两 run 路径不同；源文件不变；../、绝对路径、symlink/junction 逃逸拒绝；冻结测试不能通过写工具改/删；外部改测试检测失败；语法坏编辑不落盘；复制排除 .env/.git/缓存。
- [x] Run: `PY -m pytest tests/test_workspace.py tests/test_test_integrity.py -q`。Expected: 新接口缺失/现有可改测试/语法错误落盘引发 FAIL。
- [x] 实现私有副本与源绑定 manifest；保护设计列出的测试/配置路径；临时执行目录装入冻结测试；工作区 hash 包含相对路径。GitHub 模式要求干净独立来源并创建私有 Git 副本，来源脏则拒绝，不隐式提交；普通模式支持显式目录快照。
- [x] Run: 上述测试 + `tests/test_registry.py tests/test_pytest_runner.py`。Expected: PASS。不可创建链接的平台明确 SKIP，不能冒称通过。
- [x] 保存临时路径和原包前后摘要；不复制真实数据、不清理用户目录。

## R4：存储版本化、调用计量与有限预算

**Files:** 新建 `app/store/migrations.py`、`app/runtime/usage.py`；修改 `app/store/db.py`、`app/tools/approval.py`、`app/runtime/loop.py`；测试 `tests/test_usage_accounting.py`、`tests/test_storage_migrations.py`。

**Interfaces:**
- 新库 schema v2：runs 增加 options_json/workspace_path/baseline_path/error_code/error_message；新增 model_calls（request_id 唯一、usage字段、known状态、调用阶段）和 actions（action_id、step、tool、args、state、result）；既有表保留。
- `inspect_schema(path: Path) -> int`；`migrate_database(path: Path, backup_dir: Path) -> Path` 显式备份+事务迁移；非空旧库启动不自动升级。
- `record_model_call(run_id, request_id, phase, reply, *, outcome) -> None` 同事务更新调用账本与 usage，重复 request_id 不双计。
- `UsageMeter(run_id)`：`total_tokens`、`unknown_calls`、`over_budget(limit)`；不再维护独立 tokens/total_tokens 别名。
- `extend_budget(run_id, new_limit: int | None)`：省略时增加30000，必须高于累计且不超过1000000，并保存审批前后值。

```python
def test_resume_preserves_known_usage(run_id):
    record_model_call(run_id, "c1", "execute", ModelReply("{}", 28000, 1000, 29000), outcome="ok")
    assert UsageMeter(run_id).total_tokens == 29_000
    record_model_call(run_id, "c2", "execute", ModelReply("{}", 90, 10, 100), outcome="ok")
    assert UsageMeter(run_id).total_tokens == 29_100
    record_model_call(run_id, "c2", "execute", ModelReply("{}", 90, 10, 100), outcome="ok")
    assert UsageMeter(run_id).total_tokens == 29_100
```

- [x] 写失败用例：29000恢复后+100=29100；JSON坏输出、planner、failed run 都累计；相同响应不双计；unknown不记成完整零账单；批准后仍有有限新上限；历史库副本数据/版本保留、迁移失败回滚、备份可读。
- [x] Run: `PY -m pytest tests/test_usage_accounting.py tests/test_storage_migrations.py -q`。Expected: 现有覆盖计量/缺接口 FAIL。
- [x] 在临时库实现 schema v2 和账本，循环的每个模型响应先保存用量再解析；未知使用量转 paused/usage_unknown；一次请求发出后的真实用量越界如实记录。checkpoint不作为唯一账单来源。
- [x] Run: 上述测试 + `tests/test_m2.py tests/test_planner.py`。Expected: PASS；旧预算测试按有限扩额新契约更新，并保留拒绝未批准续跑的断言。
- [x] 保存迁移副本来源、备份验证和计量证据；不运行真实库迁移命令。

## R5：完成判定与计划恢复

**Files:** 修改 `app/runtime/loop.py`、`app/runtime/plan.py`、`app/verifier/pytest_runner.py`；测试 `tests/test_completion_contract.py`、`tests/test_planner.py`、`tests/test_pytest_runner.py`。

**Interfaces:** `VerificationResult` 或等价结构包含 verified/baseline_ok/passed/exit_code/timed_out；动作结果保存结构化验证；run summary 增加 verified。`restore_plan(run_id) -> AgentPlan | None` 读取已存合法计划。

```python
def test_done_without_verification_is_not_success(loop_with_only_done):
    result = loop_with_only_done.run()
    assert result["status"] != "done"
    assert result["verified"] is False
```

- [x] 写失败用例：首步done被拒；零收集不能done；测试后又编辑必须重测；已绿未编辑可done；工具失败不产生成功写动作；已有plan无checkpoint恢复仍注入，planner调用0次；PR模式未产生PR不能done。
- [x] Run: `PY -m pytest tests/test_completion_contract.py tests/test_planner.py -q`。Expected: 直接done/计划丢失/文本判定缺陷 FAIL。
- [x] 用结构化验证约束收尾；恢复验证状态从已提交动作结果重建；不能把失败 edit 当成功写入。改造依赖旧宽松done行为的Fake用例，显式提供真实或模拟的结构化验证，禁止增加生产绕过开关。
- [x] Run: 上述测试 + `tests/test_m2.py tests/test_pytest_runner.py tests/test_stall_radar.py`。Expected: PASS；仍保留坏决策纠错和工具失败反馈。
- [x] 保存原错误与新行为差异，不能将done直接升级为任何主观代码质量保证。

## R6：动作记录、崩溃恢复与执行锁

**Files:** 新建 `app/runtime/execution_lock.py`；修改 `app/store/db.py`、`app/runtime/loop.py`；测试 `tests/test_action_recovery.py`、`tests/test_execution_lock.py`。

**Interfaces:** `execution_lock(data_dir: Path, run_id: str)` 跨线程/进程非阻塞上下文管理器，Windows/POSIX 使用标准库文件锁；`begin_action(run_id: str, step: int, tool: str, args: dict) -> str`；`commit_action_checkpoint(action_id: str, result: dict, checkpoint: dict) -> None` 同事务落动作、trace、断点，checkpoint字段沿用现有save_checkpoint接口；`inspect_recovery(run_id: str) -> dict` 返回status、can_auto_resume、reason，状态为safe/interrupted/legacy/drift。

```python
def test_started_action_is_not_silently_replayed(started_action_run):
    # fixture创建临时run、配置、baseline和started动作，不执行真实副作用。
    decision = inspect_recovery(started_action_run)
    assert decision["status"] == "interrupted"
    assert decision["can_auto_resume"] is False
```

- [x] 写失败用例：工具后/落库前故障留下started且恢复interrupted；事务失败无半条trace/新checkpoint；已完成动作不重放；不同进程同run只一方获锁；退出后锁释放；同名文件跨目录移动改变hash；重启不自动执行任务。
- [x] Run: `PY -m pytest tests/test_action_recovery.py tests/test_execution_lock.py -q`。Expected: 旧恢复只报漂移或无原子动作记录 FAIL。
- [x] 引入动作意图和原子提交；started未知副作用不重试；文件锁与数据库条件迁移共同约束执行权，错误持久化。legacy无基线/配置时只读拒绝恢复，不猜路径。
- [x] Run: 上述测试 + R4/R5 关联回归。Expected: PASS；无已完成副作用重复次数，原工作区保留用于核查。
- [x] 保存故障注入点和回执；仅用临时文件/本地Git仓库，不运行真实push/PR。

## R7：CLI/API 共用运行服务与离线闭环

**Files:** 新建 `app/runtime/run_service.py`、`app/runtime/demo.py`；修改 `app/main.py`、`scripts/drive_task.py`、`scripts/run_all.py`、`scripts/drive_resume_probe.py`；测试 `tests/test_run_service.py`、`tests/test_cli.py`、`tests/test_api.py`。

**Interfaces:** `RunService(settings, model_factory=None)` 的 `create(task_id, goal, options, source=None) -> dict`、`execute(run_id) -> dict`、`resume(run_id, *, new_budget=None) -> dict`、`get(run_id) -> dict`；`create_app(settings=None, service=None)` 支持应用级注入；配置和工作区从数据库恢复。

```python
def test_offline_service_reaches_verified_done(service):
    created = service.create("t1_single_fix", "", RunOptions())
    result = service.execute(created["run_id"])
    assert result["status"] == "done"
    assert result["verified"] is True
    assert service.get(created["run_id"])["mode"] == "offline_demo"
```

- [x] 写失败用例：API与CLI同任务同预算结果一致；paused后新run不动旧现场；../../backend拒绝；后台异常终止后仍可查询错误；缺模式配置执行前拒绝；done再次resume冲突；offline禁止一切模型网络；同run恢复竞争只执行一次。
- [x] Run: `PY -m pytest tests/test_run_service.py tests/test_cli.py tests/test_api.py -q`。Expected: 旧共享目录/缺计量/错误丢失等 FAIL。
- [x] 构建服务；固定离线演示仅支持明示的内置T1/T2/T3脚本，不接管任意用户目标；真实任务可用本地兼容模型，默认remote禁用。后台线程执行，最多2个run，超限不悄悄排队；移除入口自动restore/clean和残留run批量标失败。
- [x] CLI 保留旧脚本入口，增加 mode/budget/compress/approve参数；退出码0仅verified done、1失败、2暂停或需核查。API恢复不能通过重传model/路径悄悄换配置。
- [x] Run: 上述测试 + R1–R6新增测试。Expected: PASS；离线演示标签、结构化结果和持久化累计一致。
- [x] 保存API/CLI命令与结果；不启动读取真实.env的生产应用。

## R8：可靠性综合验收与兼容入口

**Files:** 新建 `tests/test_runtime_acceptance.py`；按需要修改旧测试中的过时契约；更新 `README.md` 的运行正确性边界与 `OPTIMIZATION_PROGRESS.md`。

**Interfaces:** 对外保留现有路径与CLI包装；legacy错误码、verified、known_tokens/unknown_calls、mode、error持久化字段写入测试合同。

```python
@pytest.mark.parametrize("task", ["t1_single_fix", "t2_missing_fn", "t3_cross_file"])
def test_offline_acceptance(service, task):
    created = service.create(task, "", RunOptions())
    result = service.execute(created["run_id"])
    assert result["verified"] is True
    assert result["status"] == "done"
```

- [x] 添加综合回归：T1/T2/T3离线修复；有限预算暂停/扩额/续跑；失败写不落盘；checkpoint前后故障；模型适配mock；旧库副本迁移后的legacy查看/拒绝恢复；MCP多run无旧client。
- [x] Run: `PY -m pytest tests/test_runtime_acceptance.py -q`。Expected: 若集成尚有缺口则FAIL，按具体证据修复，不重写断言规避。
- [x] Run: `PY -m pytest tests/ -q --tb=short`。Expected: 全部适用用例PASS，SKIP和告警逐项记录。新测试数量以实际收集为准，不继续写139。
- [x] Run: `PY scripts/verify_mcp_real.py`，仅在已有本地官方server可用时执行；否则标未执行，不购买/下载云资源。Expected: handshake/list/echo/get-sum通过。
- [x] 核对源任务、原数据库摘要和用户评估文档；发布新的上下文快照后进入E1，无自动提交。
