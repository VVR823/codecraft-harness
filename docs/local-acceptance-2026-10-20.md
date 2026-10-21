# 真实本地模型验收记录

最新状态：同源码、同7B低内存配置的T1/T2/T3固定集完成3/3，各项均低于20000预算、0未知；T2复用既有同配置结果，T1/T3本轮各一次补验，全部导出补丁独立重放通过。22521已知token，观察耗时合计264.173秒。不是单一连续批次，也不是统计稳定成功率。后端未改，原286PASS/1SKIP门禁证据保留。

后续协议诊断：已有3B的单次JSON Schema只读决策校验通过，10.243秒、113 token、未执行工具；它不计作T1修复成功，产品接入尚未实施，详见[独立诊断记录](local-structured-output-probe-2026-10-20.md)。

## 首次尝试

2026-10-20，Windows，用户明确选择“本地验收”。结论：**此次未通过**。单次T1尝试在首个模型请求阶段暂停，未产生工具动作或源码修改，不能据此判断模型的修复能力。

## 配置与实际结果

| 项目 | 记录 |
| --- | --- |
| 模型/服务 | 已有qwen2.5:7b，Q4_K_M，Ollama 0.34.0 |
| 端点 | http://127.0.0.1:11434/v1；local模式，无远端回退 |
| 任务 | t1_single_fix，目标为修复trim_whitespace并通过原有2项测试 |
| 有限用量/请求超时 | 20000 token / 300秒；达到累计阈值后停止下一次请求，并非逐token硬截断 |
| 尝试次数 | repeat=1，retries=0；未扩额、恢复或换模型再跑 |
| run_id | 96d7b174e967 |
| 耗时/退出码 | 9.822秒 / 1 |
| 状态 | paused，verified=false，error_code=usage_unknown |
| 请求/用量 | 1次请求，账本outcome=started；prompt/completion/total均null |
| 已知token/未知调用 | 0 / 1；0只是已知量合计，不能解释为实际消耗0 |
| 工具动作/补丁 | 0 / 空补丁，原测试与私有工作区保持不变 |
| 代码身份 | HEAD 5b05f04ef8c105764a5f6e295ee1f67b2af4b054；未提交源码SHA256 9fbe07feb6e66922db991fd41fd8eaa1e264e0081084f04c94bd69dce48da08e |

原命令从backend运行，显式设置CODECRAFT_BASE_URL、CODECRAFT_MODE=local、CODECRAFT_MODEL=qwen2.5:7b、CODECRAFT_MODEL_TIMEOUT=300、CODECRAFT_ALLOW_REMOTE=false，未读取.env：

```powershell
.\.venv\Scripts\python.exe -u -m app.bench --mode local --tasks t1_single_fix --model qwen2.5:7b --budget 20000 --repeat 1 --retries 0 --data-dir ..\.cache\optimization\local-20261020-105444\data --output ..\.cache\optimization\local-20261020-105444\report
```

此命令为历史复现记录，**不自动重新执行**；未来若明确批准新尝试，应另取新的数据/报告目录。当前run有未知请求，保留现场，不绕过未知用量门禁恢复。

## 已查明与尚未查明

- 请求后的只读查询/api/version、/api/ps、/v1/models均返回200；/api/ps中没有加载中的模型。本机服务可响应查询，不等于推理调用成功。
- 机器总内存约15.68GiB，检查时空闲约3.59GiB；显卡为RTX3050Ti Laptop（约4GiB显存）及Intel集显。内存余量是待核查条件，**未证明它导致了本次失败**。
- RunService将模型传输/响应异常统一处理为用量未知，原始异常没有保存在本次账本。已有server.log为空，未取得这次请求的服务端错误；目前无法区分资源不足、传输失败或响应兼容问题，不能宣称根因已定位。
- 暂停保留请求意图并拒绝自动续跑，避免把结果未知当作未发生。该行为确实发生，但不是模型修复成功的证据。

后续若继续，应先设计不泄露正文/凭证的异常分类诊断，再决定是否在新的run中尝试已有3B模型或再次运行7B；本次没有修改异常处理、服务配置或占用其他程序的内存，也没有自动重试。

## 证据与数据保护

本机目录`.cache/optimization/local-20261020-105444/`保留：

- `report/report.json`、`report/report.md`：完整失败尝试和分母。
- `benchmark.stdout.log`、`benchmark.stderr.log`、`exit-code.txt`：命令输出与退出状态。
- `data/harness.db`及`data/runs/96d7b174e967/`：独立账本与工作区；没有使用原库。
- `export/result.json`、`export/changes.patch`：结构化结果与空补丁。
- `verification.json`：1次请求、测试基线完整、工作区与original一致、源码身份和原库主文件hash核对。

原数据库主文件SHA256仍为8f8b25a8797f16bfbc3862ebb4f95ea09d00e966e791a11352297d73a58d6078；未查询、迁移或写入原库。没有业务代码修改、提交、发布、付费调用或模型下载。未重新执行工程全套测试；此前246PASS/1SKIP为独立的历史工程验证。

## 后续：安全异常诊断已修复

用户随后明确选择“按方案修复”，在同日追加完成诊断改造，与上面的首次尝试分开记录：

- 请求异常现在只保存固定类别及有效HTTP数字状态码，不保存异常正文、提示词、源码、凭证或服务端request-id；说明保存在既有error_message，无数据库迁移。
- error_code仍为usage_unknown，账本中的未知用量仍未知；同步/后台恢复继续拒绝请求，并保留原诊断，不自动重试。
- 6项新增回归先观察失败再修复通过；最终scripts/check.py退出0，252PASS/1SKIP、124.57秒、覆盖率88.33%，Ruff检查/93文件格式检查及14模块mypy通过。按gstack清单做了本次修改的自查，没有新增独立审查。
- 代码HEAD未变；修复后app源码SHA256为3270e22ca7a53a90481f9ed1f35f97530fd84cfc877399c68cdaca28750ed709。未提交或发布。

日志在`.cache/optimization/diagnostic-20261020-110922/`：red.log、green.log、full-check.log、full-check-exit.txt、verification.json。额外核对原run仍为1次未知请求、测试基线完整、工作区等于原始副本、原库主文件hash不变。此次没有新发真实模型请求；新分类无法恢复上次已经丢失的异常，**原失败根因仍未确定，真实模型验收仍未通过**。分类含义见[操作文档](operations.md)。

## 第二次独立尝试：已定位7B加载失败

用户再次要求“继续”后，使用修复后的源码，新建了一次相同模型/任务的验收；没有恢复或重放原run。以上“根因未确定”是此前时点的结论，后续证据如下：

| 项目 | 第二次实际记录 |
| --- | --- |
| 模型/任务/预算 | 已有qwen2.5:7b / T1 / 20000 token，单次请求300秒、repeat1/retries0 |
| run_id | 0990d8c51397 |
| 耗时/退出码 | 7.628秒 / 1 |
| 状态/诊断 | paused、verified=false、usage_unknown；error_message含model_http_error HTTP 500 |
| 请求/工具/补丁 | 1次用量未知请求 / 0工具 / 空补丁 |
| 本次源码SHA256 | 3270e22ca7a53a90481f9ed1f35f97530fd84cfc877399c68cdaca28750ed709 |

本机已有Ollama server.log出现了对应时间11:17:18的失败记录：模型加载时申请2661705728字节（约2.48GiB）的缓冲区失败，随后无法分配CUDA_Host缓冲区、模型加载进程退出，接口返回HTTP 500。该证据表明本次失败发生在本机模型加载资源分配阶段，尚未进入代码修复工具执行；**不能据此判断模型不会修复代码**。

诊断脚本仅保留固定类别、数字状态码和布尔线索，没有保存模型响应/异常正文。最初的短语探针server_reports_insufficient_memory=false，只表示没有匹配到其列出的短语，不能排除其他分配失败；后续既有服务日志明确证明CUDA_Host分配失败。没有把此线索错误写成“内存正常”，也未把固定分类扩成原始正文日志。

新证据在`.cache/optimization/local-20261020-111551/`：

- `run_acceptance.py`：一次性诊断脚本，显式Settings(loopback/local)、现有模型、有限预算，无.env读取/远端回退/自动重试；不是产品代码。
- `report/`、`benchmark.stdout.log`、`benchmark.stderr.log`、`exit-code.txt`：第二次完整尝试及退出状态。
- `safe-transport-probe.json`：安全类别及HTTP 500；`server-log-facts.json`：从既有本地日志提取的分配字节数、CUDA_Host失败布尔值及HTTP 500时间，没有复制整份服务日志。
- `export/`：结构化结果及空补丁；`verification.json`：两个run均1次请求/0动作、测试基线完整、工作区等于original、原库主文件hash不变。

累计保留了2次7B/T1尝试，均未通过，2次用量未知，不能把已知token合计0解释成实际消耗0。两次使用不同代码摘要，分别留档，不合并成模型能力成功率或token节省率。

当前没有修改应用代码、Ollama配置、驱动或其他进程，没有下载模型或重跑完整测试。已向用户提出下一步可改用现有qwen2.5:3b、另建有限预算验收；**尚待用户选择，3B尚未实跑，不保证一定可用**。不自动切换、扩额或恢复任何未知run。

## 第三次独立尝试：3B可运行，修复未达标

用户随后回复“继续”，确认使用已有qwen2.5:3b新建一次T1。此前关于“3B未实跑”的描述是第二次结束时的状态；此次实际结果如下：

| 项目 | 第三次实际记录 |
| --- | --- |
| run_id | f0033b80fd2f |
| 模型/任务 | qwen2.5:3b / t1_single_fix |
| 运行配置 | local；预算20000 token；单请求300秒；repeat1/retries0；其余可选功能关闭 |
| 模型加载 | 已成功加载；只读/api/ps显示显存2159374499字节，context_length=4096 |
| 耗时/退出码 | 139.757秒 / 1 |
| 结果 | budget_paused、verified=false、budget_exhausted |
| 调用账本 | 11次模型响应均已结算，已知21657 token，未知调用0 |
| 工具执行 | 6次：初测、搜索、读取、搜索、两次编辑；没有成功修改 |
| 原测试 | 初测1 passed/1 failed；测试基线完整，没有修复后的通过结果 |
| 编辑目标/保护 | 第6、7步均试图编辑test_utils.py，被原测试保护拒绝 |
| 导出 | 空补丁，原始任务源码不变，当前工作区hash匹配最后断点 |

达到预算的过程不是无限扩额：第11次请求前已知累计19254，低于20000；该次完整响应增加2403后累计21657，系统在下一次请求前暂停。现有预算按请求之间的累计阈值检查，不是服务端逐token硬截断。没有扩额、恢复或创建第四次尝试；JSON协议修正属于该run内部决策流程，所有11次请求都保留在账本，不能只按6次工具调用计算成本。

本次说明：现有硬件能够运行3B并产生真实决策，但这个模型/提示配置在一次T1尝试中没有完成修复。模型两次试图修改测试，说明需要改进其对任务边界的遵循；原测试保护按设计生效。不能把“模型能加载”“请求都可计量”或“安全拦截有效”写成“修复成功”，也不能凭单次结果推出所有3B模型均无能力。

证据目录`.cache/optimization/local-3b-20261020-112119/`包含一次性运行/核对脚本、report/、日志、exit-code.txt、export/、verification.json及edit-targets.json。核对脚本只读结果并导出，不运行模型、不恢复任务。未改变产品源码，app摘要仍3270e22ca7a53a90481f9ed1f35f97530fd84cfc877399c68cdaca28750ed709，原库主文件hash保持不变。

累计保留两次7B加载失败及一次3B预算暂停，共3次未通过尝试；模型和代码版本不同，分别记录，不包装成统计意义上的模型成功率。当前仍无本轮真实模型修复成功证据，也没有新增付费、模型下载、远端调用、提交、部署或完整工程门禁重跑。

建议下一步仅调整既有提示流：开始时明确实现文件可改、测试与配置只读；拒绝修改测试后引导返回实现文件。权限不放宽、不注入标准答案，先补回归，再用相同预算新建一次3B/T1复验。这是建议中的有限修复，**尚未实施**，原三个run和失败记录均保留。

## 提示边界修复与第四次独立尝试

用户随后回复“继续”，确认上述有限修复及单次同预算复验。现已实施：初始系统提示明确实现文件可改、原测试/配置只读；edit_file/write_file的原保护拒绝后增加定位并读取实现文件的引导。没有修改protected()、工具权限、预算或done验证，也没有把T1答案注入提示。

新增3项回归先因缺少提示失败；首次修复后测试遇到Windows沙箱C盘Temp权限问题，不计通过，改用独立D盘TEMP/TMP后3PASS/3.95秒。完整scripts/check.py退出0：255PASS/1SKIP、116.91秒、覆盖率88.34%，Ruff/94文件格式与14模块mypy通过。修复日志在`.cache/optimization/prompt-boundary-20261020-113210/`。按gstack清单做了本次范围的作者自查，没有新增独立审查。

| 项目 | 第四次实际记录 |
| --- | --- |
| run_id | e54bdc4a54a3 |
| 模型/任务 | 仍为已有qwen2.5:3b / T1 |
| 配置 | local；预算20000；单请求300秒；repeat1/retries0，其他可选功能关闭 |
| 耗时/退出码 | 333.740秒 / 1 |
| 状态 | paused、verified=false、usage_unknown |
| 诊断 | model_timeout，第6次请求超时；已完成5次请求累计8935 token，另1次用量未知 |
| 工具 | 初测→read_file utils.py→再次测试，共3次，无成功写入 |
| 测试/补丁 | 两次均1PASS/1FAIL；基线完整；补丁0字节，源码未修复 |
| 源码身份 | HEAD不变；app SHA256为3894316e243bfc30b3a9defc000807a9b06f87c7953bf3c98ed68ebf1e2ffc5b |

本次确实读取了实现文件，没有再尝试改测试，但没有成功编辑，不能据此宣称提示修改已使修复可靠。已保存轨迹中至少有一次AgentStep字段校验失败：模型返回的编辑参数对象缺少thought，解析器拒绝后要求重出；另有提前done被未通过的测试拦截。请求超时的具体服务端原因尚未确认，**不能断言该字段错误导致了超时**。

新目录`.cache/optimization/local-3b-guided-20261020-113505/`保留运行/核对脚本、report/、日志、exit-code.txt、safe-transport-probe.json、trace-facts.json、export/和verification.json。账本中超时请求仍started/用量未知，不补填0、不恢复、不自动重试或扩额。客户端进程已退出；不能据此保证服务端未执行超时请求。

核对冻结测试、原任务源码、最后断点工作区hash及原数据库主文件hash均保持。所有4次真实尝试（两次7B加载失败、3B预算暂停、3B超时）分别保留，当前0次修复完成；配置/代码不同，不作为统计意义上的模型能力评分。

下一步建议先验证本机的结构化决策输出约束，针对已出现的“只返回工具参数而缺决策字段”问题，而非增加预算或继续堆提示。Ollama[结构化输出文档](https://docs.ollama.com/capabilities/structured-outputs)及[兼容接口文档](https://docs.ollama.com/api/openai-compatibility)说明可通过response_format传JSON Schema；本机对此参数尚未实跑，不保证支持所有Schema。此建议尚未实施，也没有发起第五次尝试。

## 第五次T1：显式结构化输出已接入，编辑仍失败

后续用户确认了只读兼容诊断后的有限接入设计，现已增加默认关闭的structured_output选项，并通过完整门禁268PASS/1SKIP、88.47%。正式bench CLI开启后，run c4933ae0db06在16.797秒内获得11次响应、22207已知token、0未知，最终budget_paused/verified=false，空补丁。

本次已存轨迹中没有协议字段纠正，正确读取了utils.py，但三次edit_file的old片段都把原文双引号改成了单引号，精确匹配拒绝写入。没有放宽保护或手工改题来制造成功。初测仍1PASS/1FAIL，无修复后的全绿证据。完整配置、日志与源码身份见[结构化输出接入记录](local-structured-output-probe-2026-10-20.md)。

当前5次真实T1均未通过，另有1次只读Schema诊断通过；它们分别留档。原四个run未恢复、未扩额，原数据库主文件hash与源任务保持，代码未提交或发布。下一步建议只改进未匹配编辑时的原文定位反馈，不做模糊写入；尚未实施或继续模型验收。

## 后续收口判断：复用已有写入工具，不继续堆反馈

用户质疑验收流程过重，随后确认先收口、重新判断最短路径。本轮未修改产品代码、未调用模型、未重跑完整门禁。只读第五次run独立库，确认三次edit_file的new均为`return text.strip()`，old均因引号不同失败；不能据此推断模型在其他任务中的能力。

现有write_file可以完整覆盖业务实现文件，仍有路径、测试保护与Python语法检查；但loop.py系统提示末尾绝对禁止覆盖已有文件。该禁止规则限制了一条现有替代路径，不是已证明的唯一失败根因。

在独立临时副本中重放已保存的编辑意图：原old被拒且不写入；人工仅纠正old的原文引用后，edit_file与write_file得到相同文件，原2项测试通过。源任务hash未变，真实模型调用0。证据在`.cache/optimization/reassessment-20261020-173354/{probe.py,result.json,pytest.log}`。**这是人工辅助的离线诊断，不是第六次T1或模型修复成功。**

撤回上一段“增强未匹配反馈”的推进建议。最小候选方案改为仅调整现有提示：完整读取的小实现文件在精确编辑失败时可用write_file完整写回；原权限、唯一精确匹配和写后原测试验证保持。该提示尚未修改，模型能否自主选择并正确写回尚未验证；如后续单次同预算复验仍失败，应收口报告本机组合的能力边界，不再逐个症状追加功能。

## 第六次T1：产物通过，未正常收尾；本地验收收口

用户“继续”批准后，仅调整loop.py系统提示，允许完整读取的小实现文件在精确匹配失败后完整写回；不改工具、权限、匹配算法或完成判定。新增闭环回归先因旧提示失败；首次GREEN发现测试误传Path给verify_baseline，改为WorkspaceSnapshot后，最终关联20PASS/4.01秒，Ruff及两文件格式通过。日志`.cache/optimization/full-write-20261020-174000/`以final-green.log为准，全门禁未重跑。

正式bench单次已有3B/T1、Schema开启、预算20000、单请求300秒、repeat1/retries0：run `fa5b727df3ef`，17.482秒，11次调用、22286已知token、0未知，最终budget_paused/verified=false，既有预算分支error_code为空。第3步准确编辑为return text.strip()，第4步原2测试全绿；第5/8/11步又write_file，第7/10步原测试再次全绿，没有正常结束。没有恢复、扩额或重跑。

最终文件另经执行副本独立核对：原2测试通过、baseline_ok=true、工作区hash未变；不修改run账本或将状态改成done。补丁271字节，源任务及冻结测试完整。app SHA256为`efcc16ad984b3460995eba19ad35d8c33c90f509e793e728ff1b07c7679a6caf`，HEAD未变；原库仅主文件hash核对保持，未查询/写入/迁移。无远端、付费、下载、提交或发布。

证据在`.cache/optimization/local-3b-full-write-20261020-174000/`，含report/、trace-facts.json、verification.json、export/changes.patch及final-artifact-verification.json。completion-facts.json空列表只表示该次提取未取得决策字段，不能推断模型从未输出done或具体收尾原因。本轮不继续调查或修补。

**真实模型已产生通过原测试的修复产物；完整自主完成验收仍未通过。** 前五次失败分别保留，不能倒写为成功，也不能从一个T1推出稳定修复率。按事先停止条件，本地验收到此收口，不再针对本机组合追加补丁或第七次尝试。

## 第七次T1：修正后单次正式验收通过

用户继续原计划补缺，并明确要求简洁高效。本轮只执行事先说明的一次端到端复验，不追加功能或试验：正式bench、已有qwen2.5:3b、local/Schema开启、20000 token、单请求300秒、repeat1/retries0，独立新数据目录，无旧run恢复或扩额。

run `2e7d7bf6a83b`正常done，verified=true、退出0，10.191秒，6次模型调用、11181已知token、0未知，4次工具动作。第2步初测1PASS/1FAIL、第3步读取实现、第4步编辑、第5步原2测试通过，随后正常结束。冻结基线、内置源任务及最后断点hash均核对通过；导出补丁318字节。没有另跑模型或手工改题。

证据目录`.cache/optimization/local-3b-protocol-20261020-193203/`含report/、stdout/stderr、exit-code.txt、verification.json和export/。app SHA256仍`e261626efcc6af88b0874cb21732313ca0dfca302bb67844d86761ea7ded0d08`；原库仅主文件hash核对保持，未查询/迁移/写入。未修改产品源码，未重跑已通过的281项完整门禁，无远端、付费、下载、提交或发布。

该结果证明当前本机组合完成了这一次T1闭环，不能据单样本宣称稳定修复、推广到T2/T3或认定某一项修正的单独因果作用。前六次失败/未完成记录不删除。Docker/Linux与系统链接权限的未验证项仍保留。

## 独立复核：账本、原测试与导出补丁均核实

用户要求确认验收“实在完成，而不是应付”后，未调用模型，另建独立核验目录。只读查询run数据库：完整性ok、状态done、1条done轨迹、6次调用均received且合计11181 token、4个动作均completed，最后原测试2PASS且晚于编辑；断点hash匹配。冻结测试逐文件与原内置任务相同，原始源码副本也与内置源一致。

再用全新副本做对照：未打补丁时仍1PASS/1FAIL；导出的changes.patch经git apply --check后直接应用，无手工改动；同一份冻结原测试变为2PASS，代码与真实run产物一致。原run工作区hash保持，当前app源码身份仍与验收报告一致。核验脚本最初误用动作状态名committed，查明实现实际为completed后纠正，未改变任何产品或验收数据。

证据`.cache/optimization/acceptance-audit-20261020-193633/{audit.py,result.json}`。成功案例现已加入只读页，仍保留未完成案例；5案例在Edge桌面/375px核对通过，无页面错误或任务API请求，脚本、截图、showcase-verification.json同目录保存。

## 补充T2/T3：同版正式验收均未通过

用户继续原计划补缺后，T1不重复，仅补T2/T3各一次。已有qwen2.5:3b、local/Schema、每项20000 token、单请求300秒、repeat1/retries0，新数据目录；未改代码、扩额、恢复或自动重试。

- T2 run 158f9e99a30e：16.109秒、21785已知token、0未知，budget_paused/verified=false，6工具动作、空补丁。初测0PASS/4FAIL；模型edit_file将原文双引号写成单引号，精确匹配拒绝，随后读取无成功写入。
- T3 run d190e44934e4：8.054秒、20891已知token、0未知，budget_paused/verified=false，5工具动作、空补丁。初测2PASS/3FAIL；3次编辑都给出不存在的if语句片段，保护拒绝写入。

两次都已结算、动作completed，测试基线、源任务和断点hash核对通过，导出保留空补丁；预算为请求间阈值，最后完整响应后停止下一次请求。证据`.cache/optimization/local-3b-t2-t3-20261020-194311/{report,verification.json,export}`。

输入报告的source_sha256与options完全相同后，按正式summarize/write_report汇总T1及本次两项，存于同目录current-version-suite/：完成1/3、53857已知token、0未知、34.354秒、15工具动作。旧代码版本失败不纳入同版汇总，但原记录全部保留。本轮无产品修改或完整门禁重跑，无远端费用、下载、真实库写入/迁移或发布。结论是当前3B组合的多任务修复能力仍有限，不以工程回归代替模型能力，不继续针对失败追加补丁来凑通过。

## 用户授权的代码模型对照：下载完成，T2/T3仍未通过

用户明确“确认”后，通过已有本机Ollama下载qwen2.5-coder:3b，1929912626字节，digest f72c60cabf6237b07f6e632b2c48d533cef25eda2efbd34bed21c5e9c01e6225；服务报告下载及SHA256校验成功。只下载此模型，不购买服务或调用远端推理。

正式bench仅T2/T3各一次，代码、预算20000、超时300秒、Schema与其他选项均与通用3B对照一致，只改变model；repeat1/retries0，不读.env、不恢复旧run。

- T2 run c72b087cbc64：budget_paused/verified=false，22232已知token、0未知、15.427秒、8工具、空补丁。编辑候选缩进错误，语法检查先拒绝、未写入；原0PASS/4FAIL。
- T3 run d8cfe265dc5a：budget_paused/verified=false，21001已知token、0未知、7.785秒、8工具、空补丁。old出现2次，唯一匹配保护拒绝；原2PASS/3FAIL。

合计43233已知token、0未知、23.212秒，完成0/2。verify.py核对数据库、动作、冻结基线、源任务和断点hash通过，并保留导出空补丁；comparison.json确认source相同、options仅model不同。无自动重试、扩额、代码/权限/测试变更。T1未在代码模型上测试，不合并成同一模型的三任务结果。

证据`.cache/optimization/coder-3b-20261020-213632/{download-result.json,report,verification.json,comparison.json,export}`。原数据未写入/迁移，产品源码未改，281项门禁不重复跑。该次对照没有证实换模型即可解决问题；不把下载或保护生效算作任务修复成功。

## 立即排错与修复（22:18）

用户要求立即发现并解决错误，本轮只修改loop、工具说明和相应回归，未改源题/原测试/预算/权限。

原始参数已核实：T2把完整def放入替换raise单句的new，产生缩进错误；T3的old同时命中文档和代码。精确唯一匹配与候选语法检查正确，未放宽。最初统一小文件全文写回指引未解决模型问题；随后缩短重复提示、过滤未启用交付/HIGH工具，运行提示3144→1606字符。动态MCP只读工具与开启GitHub模式的交付工具仍保留。

真正复现的程序缺陷：_prev_action只保存成功动作，read→编辑/测试失败→read被误认为连续读取；错误反馈还要求read，形成恢复死循环。2项新回归在旧逻辑均失败，改为失败后清空旧记录后通过；连续重复成功动作保护仍在，关联41PASS。另3项回归覆盖小文件全写回与简洁工具菜单，总新增5项。最终离线门禁286PASS/1SKIP、76.92秒、覆盖率88.59%，Ruff/97文件format/14模块mypy通过。最初沙箱检查停滞，核对本轮4个进程后仅停止自己的测试进程，原日志保留；随后用正常本机权限执行，临时库不触碰真库。

|批次/源码摘要|任务|run|结果|已知token/未知|耗时|
|---|---|---|---|---|---|
|仅指引/bdfa94ee|T2|9a258a2e47ae|budget_paused/未验证|20167/0|11.731s|
|仅指引/bdfa94ee|T3|90940191c981|budget_paused/未验证|20645/0|8.284s|
|精简/2b1f0f40|T2|81bfc1c5ec67|budget_paused/未验证|21428/0|9.434s|
|精简/2b1f0f40|T3|50bb3290f009|done/verified，5PASS|7158/0|4.355s|
|恢复修复/bd7bd7ce|T2|45f934e46508|budget_paused/未验证|20409/0|15.562s|
|相同最终源码/已有7B CPU|T2|744813325dc1|paused/usage_unknown，0工具|0/1|3.274s|

每批repeat1/retries0，均显式local、20000预算、300秒，无扩额或恢复；后续批次是排错后的明确验证，不删除前批失败。代码3B/T3导出补丁257字节，在全新内置源副本先2PASS/3FAIL，git apply原补丁后5PASS，未手工补码，原测试未改。该成功属于精简版本，不能与旧T1或最终版本拼为同版固定集通过。

最终T2恢复重读已可执行，但3B产生了错误的函数重定义、局部import作用域和调用结构，测试仍1PASS/3FAIL。其586字节错误补丁保留，不计完成。7B未下载新权重：复用已安装qwen2.5:7b建立独立tag codecraft-qwen25-7b-cpu-20261020，Modelfile仅num_gpu=0/num_ctx=4096，未覆盖原模型。Ollama日志22:14:13明确CPU_REPACK缓冲3121348608字节分配失败，未产生有效模型输出，不计编码能力失败或通过，也不把未知用量当零。

证据根.cache/optimization/small-file-repair-20261020：red/green、compact-red/green、recovery-red/green、各批report/verification/export、recovery-check.log与t3-replay.json。最终app SHA256 bd7bd7cedf1e75e317366d04910ad1764f2f8f7cacb9713cc85e6217e07aec8c。原真实库主文件hash不变，未查询/写入/迁移；源tasks无修改，无提交/推送/部署/清理/定时任务。程序恢复缺陷已修，T3有真实成功证据，T2仍未完成；不继续盲目堆提示或重复受内存限制的7B加载。

## 释放内存后关闭CPU重排，T2真实通过（22:41）

最新内存处置与真实复验：用户授权关闭非GPT应用后，可用内存实测5.68GiB。复用已有qwen2.5:7b权重，以独立本地llama-server关闭CPU重排、mmap加载、4096上下文、128/64批次、1并发、6线程；健康检查200，成功加载后可用1.42GiB。T2 run fa828cc35d24正常done/verified，原4测试通过，7798 token/0未知、120.649秒，低于原20000预算，repeat1/retries0。导出补丁在全新内置源副本直接应用，由4FAIL变为4PASS，无手工改码、改测试或额外模型请求。所有旧失败保留；这是当前源码在7B低内存配置的单项成功，不是同版全任务通过或稳定成功率证明。

服务复用D:/ollama/models/blobs/sha256-2bada8a7450677000f678be90653b85d364de7db25eb5ea54136ada5f3933730（4683073952字节）；启动已安装D:/develop/ollama/lib/ollama/llama-server.exe，独立127.0.0.1:11435，模型别名qwen2.5-7b-cpu-norepack。参数为--no-repack --load-mode mmap --gpu-layers 0 --ctx-size 4096 --batch-size 128 --ubatch-size 64 --parallel 1 --threads 6。没有下载、修改原Ollama/GPT配置或系统分页文件。释放应用内存和调整引擎共同构成本次条件，不能归因为单一参数的独立因果实验。

T2动作顺序：run_tests（0过4败）→read_file module.py→write_file（成功）→run_tests（4过0败）→done。5次模型调用均received，已知用量1117+1288+1747+1798+1848=7798；最后全绿测试晚于写入，断点hash一致、冻结原测试完整。导出补丁800字节，未手工修改。补丁删除了顶部任务说明docstring，保留函数说明，未将此结果宣称为最小补丁或完全遵守所有格式偏好。独立副本直接git apply后原4测试通过。

证据.cache/optimization/cpu7b-norepack-20261020/包含server.json/日志、health.json、report、verification.json、export、replay.json及负/正例测试日志。复验完成后按PID/启动时间/路径/端口核对，只关闭本轮临时服务PID18208；原Ollama、GPT/Codex和网络代理保留，文件/日志未清理。关闭前的时间比较曾因PowerShell把ISO文本自动转DateTime而拒绝动作，经只读确认UTC时间相同后正常关闭；未误关其他服务。

本轮产品代码/源任务/原测试均未修改，app SHA256仍bd7bd7cedf1e75e317366d04910ad1764f2f8f7cacb9713cc85e6217e07aec8c。未重复离线门禁或浏览器测试，静态案例站未新增此T2案例。真实库主文件hash保持、未查询/写入/迁移，无提交、发布、扩额或自动续跑。T1/T3的历史成功不能与本次拼接为相同源码/模型/引擎的3/3；后续若需要统一配置集合验收，应单独明确范围。

## 同配置补验T1/T3并复用T2（22:58）

同源码、同7B低内存配置的固定三任务现已各一次通过：T1原2测试、T2原4测试、T3原5测试全部通过，三个导出补丁也在全新原任务副本直接应用后通过。T1为5830 token/70.224秒，T2为7798 token/120.649秒，T3为8893 token/73.300秒；合计22521已知token、0未知，三项各低于20000预算。T2复用此前同配置已验证结果，T1/T3本轮各运行一次，不是一个连续批次，也不代表稳定成功率。

|任务|run|本轮模型请求|状态|原测试|已知token/未知|观察耗时|
|---|---|---|---|---|---|---|
|T1|72716ab4aade|本轮一次|done/verified|2PASS|5830/0|70.224s|
|T2|fa828cc35d24|复用前次，无重跑|done/verified|4PASS|7798/0|120.649s|
|T3|578f47c747be|本轮一次|done/verified|5PASS|8893/0|73.300s|

app SHA256均bd7bd7cedf1e75e317366d04910ad1764f2f8f7cacb9713cc85e6217e07aec8c，完整RunOptions逐字段相等；同一已有7B GGUF文件/大小及llama-server启动参数相等（no-repack、mmap、CPU、4096ctx、128/64批次、1并发、6线程），同端点/别名、300秒单请求超时、repeat1/retries0，无扩额。T2与补验非连续同批，缓存和运行时刻可能不同；耗时为这三次观察值，不能作稳定性能或成功率结论。

本轮新增两次run：T1四工具（初测1PASS/1FAIL→读utils.py→edit_file→原2PASS→done）；T3五工具（初测2PASS/3FAIL→search_file→read_file→edit_file→原5PASS→done）。独立临时数据库integrity、原任务/冻结基线、所有completed动作、断点hash与导出逐项核对。新副本先保持对应失败，再git apply原导出补丁，分别2PASS与5PASS，无手工补码、改测试或新模型调用。T1导出保留了原来的过时bug注释，没有手工美化；不宣称所有注释质量完美。

可公开报告（含三项原始补丁）见[evidence/local-7b-fixed-suite-2026-10-20.json](evidence/local-7b-fixed-suite-2026-10-20.json)。私有核验文件在.cache/optimization/local-7b-completion-20261020：report、verification.json、export、replay.json、same-config-suite、server.json/日志、health.json、closeout.json。T2原证据目录只读复用，不重新运行或覆盖。旧失败、旧模型单项记录与7项静态案例保留，不混入本配置成功率分母。静态页本轮未修改，未重复浏览器检查。

验收与复验后已核实PID7980/启动时间/路径/端口，仅关闭本轮临时模型服务，保留原Ollama、GPT/Codex及代理。没有下载、付费、后端修改、原测试/源任务修改、真实库查询/写入/迁移、提交、PR、部署或自动续跑；286PASS/1SKIP、88.59%是当前未变后端的既有门禁，本轮不重跑。Docker/Linux/云CI仍未实跑，符号链接1项SKIP。此前“统一配置未验证”段落属于历史时点，最新结论以本节及文件开头为准。
