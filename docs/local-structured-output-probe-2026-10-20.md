# 本地结构化输出：诊断与接入

最新状态：用户确认后，默认关闭的显式开关已接入CLI/评测/API；离线门禁268PASS/1SKIP。首次开启后的完整T1仍未通过，记录在后文，不能把前面的只读协议诊断当作修复成功。

2026-10-20，用户在第四次T1超时后再次要求“继续”。本次仅做一次受控协议诊断，**不是新的T1修复尝试**。

## 实际结果

| 项目 | 记录 |
| --- | --- |
| 服务/模型 | 本机Ollama，已有qwen2.5:3b |
| API | http://127.0.0.1:11434/v1/chat/completions |
| 请求约束 | response_format=json_schema，strict=true；单请求60秒、max_tokens=256、SDK重试0 |
| Schema | 必须包含thought、tool、args、done；tool固定read_file；args必须有path；done=false，禁止额外字段 |
| 意图 | 生成读取main.py的决策，不实际执行工具或读该文件 |
| 结果 | 已返回；Schema校验通过，args.path意图校验通过 |
| 用量 | prompt58 + completion55 = total113，未知用量false |
| 耗时/退出码 | 10.243秒 / 0 |
| 响应保存 | 不保存原始响应正文，仅保留校验布尔值和计量信息 |

该结果证明本机端点/模型接受并完成了**这组只读Schema**的受控诊断；尚未证明多工具参数、完成分支、规划或任意Schema兼容，更没有证明能修复T1。前面4次修复失败记录全部保留，不能把本次诊断计入修复成功分子。

## 证据和边界

目录`.cache/optimization/schema-probe-20261020-144847/`：`probe.py`、调用前的`intent.json`、`result.json`、stdout/stderr、exit-code.txt、source-identity.json。脚本拒绝重复使用已有intent的目录，只发一次调用，无自动重试、工具执行、.env读取、远端回退或模型下载。

产品代码未改，app SHA256仍3894316e243bfc30b3a9defc000807a9b06f87c7953bf3c98ed68ebf1e2ffc5b，HEAD仍5b05f04ef8c105764a5f6e295ee1f67b2af4b054；原库主文件hash仍8f8b25a8797f16bfbc3862ebb4f95ea09d00e966e791a11352297d73a58d6078。本次未写真实库、未提交或发布，未重跑无变化的完整门禁；255PASS/1SKIP、88.34%仍是此前提示修复的证据。

参考Ollama官方[结构化输出](https://docs.ollama.com/capabilities/structured-outputs)及[兼容接口](https://docs.ollama.com/api/openai-compatibility)。文档能力与本机诊断分别核查，当前没有把本机一次通过泛化到其他服务/版本。

## 建议的有限接入设计（尚未实施）

增加默认关闭的显式开关，并保存进run配置；启用时仅给local模式的执行决策请求增加thought/tool/args/done的JSON Schema，保留现有解析器、工具参数验证和权限边界。规划与其他模式沿用当前行为，服务不支持参数则明确暂停并保留调用记录，不静默降级重发。

先用内存MockTransport覆盖Schema请求、阶段/模式隔离、配置保存及无隐式重试，再运行完整门禁；通过后只新建一次相同3B/T1、20000 token/300秒的验收。不恢复旧未知run、不扩额，不把诊断成功当作修复成功。Schema只约束结构，不注入修复答案，也不承诺能解决此前超时。

## 后续：开关已接入并完成一次正式验收

用户随后明确“继续”上述有限方案，现已完成：

- RunOptions新增structured_output，默认false；新run保存该字段，旧配置缺字段按false加载，无数据库迁移。只允许local开启。
- CLI与评测增加`--structured-output`；API创建任务接受structured_output。CLI恢复不接受覆盖此字段。控制台尚无此开关。
- 执行阶段由服务生成AgentStep决策封套Schema，要求thought/tool/args/done且禁止外层额外字段；tool和args仍由原解析器/注册表校验。规划、默认关闭和其他模式保持原请求格式。
- ModelClient通过response_format=json_schema发送约束，SDK仍max_retries=0；不支持则按现有安全错误分类暂停，不移除参数后重发。离线DemoClient的统一接口显式拒绝Schema参数。

配置6项先RED→GREEN；随后传输/阶段隔离/拒绝降级4项先失败再修复，原默认格式测试同步通过。首次完整门禁发现DemoClient类型契约缺新参数，补了直接拒绝Schema的失败用例后修复，没有忽略mypy错误。最终定向17PASS（含原4项适配测试），完整scripts/check.py退出0：**268PASS/1SKIP、63.30秒、覆盖率88.47%**；Ruff、95文件格式检查、14模块mypy通过。

日志目录`.cache/optimization/structured-output-20261020-171204/`：config-red/green.log、transport-red/green.log、demo-red.log、final-targeted.log、final-check.log、final-check-exit.txt、source-identity.json。较早full-check.log是类型检查失败，最终结果以final-check.log为准。按gstack清单做本次范围作者自查，未新增独立审查。

### 正式CLI验收结果

通过门禁后使用正式`python -m app.bench`，明确开启`--structured-output`，仅一次3B/T1，20000 token、单请求300秒、repeat1/retries0；清除当前子进程的CODECRAFT旧配置后显式指定loopback端点和禁止远端，没有读取.env。

| 项目 | 实际结果 |
| --- | --- |
| run_id | c4933ae0db06 |
| 模型/源码 | qwen2.5:3b；app SHA256 001dac9320b18046e6e97c69bf8a08ed6a1c0afbf47648578a181898170093be |
| 耗时/退出码 | 16.797秒 / 1 |
| 状态 | budget_paused、verified=false；本次循环预算分支error_code为空 |
| 调用/计量 | 11次received，22207已知token、0未知；已保存上下文中协议格式纠正0条 |
| 动作 | 8工具动作；初测1PASS/1FAIL，读取utils.py，3次编辑utils.py均失败，其他为读取 |
| 失败原因 | old使用`return text.strip(' ')`，原文是双引号版本，精确片段匹配拒绝写入 |
| 产物/保护 | 补丁0字节，测试基线/原任务源码未变，工作区hash匹配最后断点 |

第11次请求前已知累计19762，低于20000，完整响应增加2445后达到22207，系统停止后续请求；没有扩额。这仍是请求间累计阈值，不是逐token硬截断。

新目录`.cache/optimization/local-3b-schema-20261020-171204/`保留report/、stdout/stderr、exit-code.txt、verify_acceptance.py、verification.json、trace-facts.json、export/。实际持久选项structured_output=true，全部模型调用已结算；未恢复任何旧run。原库主文件hash仍8f8b25a8797f16bfbc3862ebb4f95ea09d00e966e791a11352297d73a58d6078，HEAD仍5b05f04ef8c105764a5f6e295ee1f67b2af4b054；代码未提交/发布，无付费、远端、下载或真实库写入。

本次说明：结构化请求确实进入完整执行流程，本次没有字段格式纠正或超时，但未修复代码。不能凭单次耗时或0纠正推广为稳定性/节省率保证。5次T1尝试均保留为未通过，独立只读诊断不进入修复成功分子。

### 下一步建议（未实施）

只增强edit_file未匹配时的反馈：给出对应原文定位或候选原文，便于模型逐字复制；**不做模糊替换，不放宽唯一且精确匹配，不注入答案**。先补引号差异/不修改文件等回归，再以相同预算新建一次3B/T1复验。当前没有实施该建议或发起下一次模型尝试。
