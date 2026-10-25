# How to 运行、恢复和取回任务结果

以下命令均在仓库`backend`目录运行。使用Python3.13，先按[README](../README.md)安装。Windows示例使用PowerShell；Linux把`.venv\Scripts\python.exe`换成`.venv/bin/python`。

## 本地模型与自带源码

准备自己已有的OpenAI兼容本地模型服务，不自动安装或下载模型。模型地址和名称由操作者配置；本地模式只允许loopback端点，不会转到付费服务。

本机已有7B采用低内存CPU配置后，T2已真实完成并经导出补丁独立复验，原4测试通过、7798 token、约121秒。具体准备、启动、健康检查、单次运行和退出步骤见[低内存本地模型指南](low-memory-local-model.md)。早期GPU/CPU加载失败及3B未完成记录仍保存在[真实验收记录](local-acceptance-2026-10-20.md)，不能当作当前唯一结论，也不能与不同版本合并计算成功率。

以下为普通本地服务配置与自带源码模板，替换成自己的路径和模型：

```powershell
$env:CODECRAFT_BASE_URL = 'http://127.0.0.1:11434/v1'
$env:CODECRAFT_MODEL = '你的已有模型名'
.\.venv\Scripts\python.exe scripts/drive_task.py t1_single_fix --mode local --workspace D:\your-project --goal '修复已有测试暴露的问题' --data-dir .\data-local
```

`--workspace`明确授权读取该本地目录，执行发生在其副本；Web只选择内置任务，不允许提交任意宿主路径。源码项目需要可在当前Python环境运行的pytest测试；不自动安装项目依赖。GitHub交付另需显式`--github --workspace <干净独立仓库>`、可用Git/gh及操作者已有凭证；副本保留来源提交历史，源仓库不变。拒绝Git worktree的.git指向文件和origin中嵌入的凭证。GitHub模式会产生远端副作用；2026-10-25已在专用合成夹具分支完成真实工具交付，见[新版验收](real-acceptance-2026-10-25.md)。固定控制器通过不代表模型自主交付。

## 显式启用本地结构化决策

默认关闭；仅local执行决策支持，规划请求沿用原格式。当前通过CLI、评测或API开启，控制台暂不提供该开关：

```powershell
$env:CODECRAFT_BASE_URL = 'http://127.0.0.1:11434/v1'
$env:CODECRAFT_MODEL_TIMEOUT = '300'
.\.venv\Scripts\python.exe scripts/drive_task.py t1_single_fix --mode local --model qwen2.5:3b --structured-output --budget 20000 --data-dir .\data-local-schema
```

使用新数据目录。评测入口`python -m app.bench`也接受`--structured-output`；API创建任务字段为`"structured_output": true`。此选项随run保存，恢复时不再传CLI开关，也不能借恢复覆盖它；旧run没有字段时按默认false解释，无需迁移。

开启后发送thought/tool/args/done的JSON Schema，仍保留AgentStep解析、工具参数校验、权限与原测试保护。offline_demo/remote开启该选项会明确拒绝；本地服务不支持Schema而返回错误时，保留诊断和未知用量并暂停，不静默降级重发。Schema约束结构，不保证模型能提出正确修复。

早期Schema接入后的3B/T1曾因精确编辑失败而预算暂停，记录见[结构化输出记录](local-structured-output-probe-2026-10-20.md)。随后T1/T2/T3分别有真实成功记录，但模型和源码版本不同；最新状态以[逐项验收](acceptance.md)为准，Schema本身不保证任务成功。

## 人工恢复一段

`repair_stalled`表示当前执行段对同一文件连续3次`edit_file`/`write_file`失败，任务已暂停。先查看错误并调整修复方式，再显式恢复；刷新页面或重启服务不会自动续跑。恢复后可直接重新读取该文件，并获得一段新的有限修复机会；累计token预算仍然保留。

失败后工具反馈会提醒重新读取、保留文档字符串成对三引号，以及使用完整、唯一的代码行做局部替换。读取文件和内容未变的写入不算修复进展；真正成功修改会重置失败计数。相对路径和工作区内绝对路径按同一文件判断。该规则不放宽语法检查、原测试保护或完成判定。

```powershell
.\.venv\Scripts\python.exe scripts/drive_task.py --resume RUN_ID --data-dir .\data-demo
.\.venv\Scripts\python.exe scripts/drive_task.py --resume RUN_ID --new-budget 60000 --data-dir .\data-demo
```

第二条用于预算暂停，新值必须大于已知累计且不超过1,000,000。兼容`--approve-budget`默认追加30,000，仍有上限。恢复复用原配置，不接受重传model、模式或源码路径。退出0仅表示done且verified；失败1，暂停/待核查2。每次只运行一段，不自动续跑。

重复提交完全相同的文件内容不会重写文件，也不会使此前通过的验证失效；实际内容发生变化后仍必须重新测试。这个规则在执行、重启恢复和查询中保持一致。历史动作没有记录“内容未变”的证据时仍保守按修改处理，不反推旧run成功；验证保持有效也不会自动把paused改成done。

| 状态或错误 | 操作 |
| --- | --- |
| paused、无未知调用 | 人工恢复一段，检查是否仍有进展 |
| budget_paused | 设置新的有限总预算 |
| usage_unknown | 保留调用记录核查；当前没有自动结算或强行绕过命令，可另建新run |
| action_unknown / interrupted | 检查工作区与started动作，不猜测副作用未发生，不自动重试 |
| workspace_drift | 保留现场，检查外部改动；从同任务新建run，不删除旧断点绕过 |
| legacy_run | 只查看，缺配置/测试基线的旧run不自动迁入恢复 |
| done | 查看或导出，不再恢复 |

模型请求失败仍使用`error_code=usage_unknown`并暂停；新增的固定分类保存在`error_message`，重启及被拒绝的人工恢复不会覆盖原诊断：

| 诊断类别 | 含义 |
| --- | --- |
| model_timeout | 请求超时，不能认定服务端没有执行 |
| model_connection_error | 客户端连接/传输异常 |
| model_http_error HTTP 500 | 服务返回HTTP错误；记录实际数字状态码，不保存响应正文 |
| model_response_error | 客户端响应解析或处理异常 |
| model_error | 未归入上述类型的请求异常 |
| model_usage_unknown | 已返回响应但用量缺失或无效，或没有更具体的诊断 |

这些分类不记录原始异常、提示词、源码、凭证或服务端request-id，也不能仅凭HTTP 500判断为内存不足。旧run没有保存过的异常无法事后还原；新增诊断不授权自动重试、清除未知调用或扩额。

修复提示会明确业务实现文件是修改目标，原测试与测试配置只读。模型尝试修改受保护文件时，原保护继续拒绝，并提示根据测试import或search_file定位实现、read_file后再编辑实现。该提示不放宽权限、不注入任务答案，也不保证小模型一定遵循；以实际冻结测试结果判定修复。

## 导出

```powershell
.\.venv\Scripts\python.exe -m app.export_run RUN_ID --data-dir .\data-demo --output .\exports\RUN_ID
```

输出`result.json`和`changes.patch`，只含允许列表摘要、相对路径文本补丁和摘要trace，不打包.env、.git、数据库或完整prompt。导出取得同run执行锁，核对冻结测试和最后断点的工作区hash，读补丁后再核对；外部改动的当前文件不能沿用旧的verified结论。补丁每文件最多256KiB、合计最多2MiB；旧run没有original时`patch_available=false`。工作区仍是完整产物入口。常见凭证模式会脱敏，公开前仍需人工检查；脱敏过的补丁可能不能直接应用。导出目录不能位于运行工作区内。

## 显式升级历史库

先停止所有使用该库的进程，保留整个data目录（数据库、WAL/SHM以及runs）；不要只拷一个正在写入的主文件。

```powershell
.\.venv\Scripts\python.exe -m app.store.migrations .\data\harness.db --backup-dir .\data-backups
```

命令用SQLite backup API生成唯一备份并检查完整性，然后事务升级v0到v2；已有v2不重复迁移。失败保留备份，schema改动回滚。本轮仅在临时旧结构库验证，未迁移仓库真实数据。迁移保留历史记录，但不补造旧run的工作区或验证证据。恢复备份时停服，把备份复制到**新的数据目录**，避免覆盖正在用的库；指向该目录检查后再决定切换。

## 配置参考

不隐式读取.env。CLI只有显式`--env-file <文件>`才读取dotenv。API通过启动环境配置；更改密钥无须写入源码。

| 环境变量 | 默认 / 范围 |
| --- | --- |
| CODECRAFT_DATA_DIR | backend/data；首次体验推荐新的data-demo目录 |
| CODECRAFT_MODE | offline_demo；另支持local、显式允许的remote |
| CODECRAFT_MODEL | local-model；1–128字符 |
| CODECRAFT_BASE_URL | 空；local仅loopback http(s)，不允许凭证/query/fragment |
| CODECRAFT_API_KEY | 空；SecretStr，不通过API返回 |
| CODECRAFT_ALLOW_REMOTE | false；本轮不启用 |
| CODECRAFT_TOKEN_BUDGET | 30000；1–1000000 |
| CODECRAFT_MODEL_TIMEOUT / SANDBOX_TIMEOUT | 120秒；1–600 |
| CODECRAFT_API_TOKEN | 空；设置后所有/api路由要求Bearer |
| CODECRAFT_RATE_LIMIT | 每peer每60秒5次POST；1–1000 |
| CODECRAFT_LOG_FORMAT | json，也可text |

remote明确开启且未给CODECRAFT_API_KEY时才兼容ZHIPU_API_KEY；模型名与“免费额度”都不能保证零费用。普通使用保持offline_demo，或使用自己已有的本地服务。

## API参考

鉴权用`Authorization: Bearer <自行设置的token>`，控制台token只保留在当前页面内存。默认监听127.0.0.1；外部访问需操作者自行配置token、HTTPS反向代理与访问控制，不属于匿名服务方案。不信任X-Forwarded-For、不设置宽泛CORS。

| 方法与路径 | 参数 / 行为 |
| --- | --- |
| GET /health | 存活，不需要密钥 |
| GET /ready | 检查schema、写锁和当前默认模式配置；未就绪503 |
| GET /api/packs | 内置包与offline_demo支持标记 |
| GET /api/runs | limit默认30，1–100 |
| POST /api/tasks | task_id、goal（≤20000字符）、mode、model（≤128）、token_budget、use_plan/context_compress/use_skills/use_mcp/use_memory |
| GET /api/tasks/{run_id} | status、mode、verified、options、用量、持久错误、审批、近期trace与私有工作区位置 |
| GET /api/tasks/{run_id}/traces | offset≥0、limit默认50/最大200 |
| POST /api/tasks/{run_id}/resume | new_budget可选；兼容approve_budget；拒绝更换原配置 |
| GET /api/tasks/{run_id}/result | 终止/暂停后可取脱敏摘要与补丁 |

请求体最多64KiB；鉴权失败401、参数422、配置/任务错误400、恢复冲突409、速率/并发超限429。POST共用限流，客户端表最多10000项、空闲120秒淘汰。API单worker，最多2个run，不隐式排队；服务重启不自动继续任务。

## Docker部署

2026-10-21已在本机WSL2/Ubuntu/Docker实际完成构建、UID10001运行、离线任务/导出及保卷重建。完整`/data`卷保存DB、workspace、baseline、original及manifest。构建上下文按应用/内置任务/UI/必要脚本允许列表选择，不复制整个backend目录或运行数据。详细证据见[容器实测说明](container-validation-2026-10-21.md)。以下在Linux或WSL内的仓库根目录执行：

```sh
docker compose --env-file /dev/null up --build
docker compose run --rm backend smoke --data-dir /data/smoke
docker compose run --rm backend drive t1_single_fix --mode offline_demo --data-dir /data
```

默认映射127.0.0.1:8000；容器内监听0.0.0.0方便映射。重建时保留harness_data卷；不要执行`down -v`删除产物。旧schema启动只给迁移说明，不自动升级。

在WSL终端按上文执行 docker compose up 并保持终端打开，Ctrl+C停止应用；镜像与数据卷保留。需要释放WSL内存时，先确认没有其他WSL任务，再由操作者执行 wsl --shutdown。安装路径与镜像下载网络由各自环境配置，不依赖作者电脑上的启动脚本或代理。

故障核查与实测范围见[testing.md](testing.md)，状态和事务设计见[architecture.md](architecture.md)。
