# CodeCraft运行机制与边界

CodeCraft让模型逐步使用工具修复一个可信Python项目，并保存每步动作与验证结果。CLI和Web共用RunService；同一任务的不同运行使用不同副本，源文件无需重置。

执行决策使用thought/tool/args/done封套。新动作回填给模型的assistant消息也使用这一JSON格式；thought是程序生成的工具调用摘要，不是模型原始思考。完成示例显式使用tool=null、args={}、done=true，与可选Schema的必填字段保持一致。完成仍须通过原测试校验，不因格式正确就放行。旧断点中的历史提示与自然语言摘要原样保留，不自动改写历史运行；这些修正针对新建运行及其后续恢复。

```text
CLI / Web控制台
      |
FastAPI ---- 鉴权 / 输入边界 / 有界限流
      |
RunService ---- OS文件锁 + SQLite条件状态迁移
      |
HarnessLoop ---- ModelClient 或明确标记的DemoClient
      |             |
      |          模型调用意图 -> 已知用量 / 未知调用
      |
每run ToolRegistry -> 文件读写 / pytest / 可选MCP
      |
动作意图 -> 工具结果 + trace + checkpoint（同一SQL事务）
      |
data/runs/<id>/{workspace,baseline,original,manifest.json}
```

## 为什么需要三份文件

`workspace`是可修改副本，`baseline`保存冻结测试与pytest配置，`original`保存初始源码供导出差异。清单与基线在Agent可写目录之外。复制时拒绝链接/junction，排除.env、私钥、缓存、虚拟环境和数据目录，最多2000文件/50MiB。磁盘开销会高于源文件大小；当前保留所有运行，不自动删除。

工具不能修改原测试、conftest.py、pytest.ini、pyproject.toml、setup.cfg、tox.ini、Git元数据或工作区外路径。Python候选修改先检查语法再落盘。pytest在新的临时副本执行，覆盖冻结测试；退出0、至少1项通过、未超时且基线完整，才产生结构化verified。done还要求验证晚于最后一次成功写入。

## 断点与副作用

每个工具先保存started意图，之后把结果、trace、checkpoint一起提交。数据库事务无法包含文件系统或GitHub操作：如果工具执行后进程中断，started仍在，恢复返回interrupted，不自动重放。已经completed的动作从断点之后继续。工作区hash包含相对路径和内容，移动同名文件也会造成漂移。

同run的文件锁跨线程/进程互斥，进程退出由操作系统释放；数据库条件更新约束状态转换。重启只读取记录，不自动执行。服务设计支持一个worker，最多2个并行run；没有持久化队列或多租户隔离。

Git推送、PR创建等操作超时只表示结果未知，不能证明未成功；对应意图保留started并停止，不能进入普通工具错误的自动纠正重试。安全停在断点的旧running记录可由控制台明确发起“检查中断并尝试恢复”，后端文件锁判定执行权。

## 用量与预算

每次模型请求先落调用意图，收到响应后保存usage。请求失败或usage缺失保留unknown，不当成零成本；未知用量会暂停。包括planner、坏JSON、失败run在内的已知用量均累计。预算达到上限时阻止下一次调用，人工续跑设置新的有限总额。单次请求返回的实际用量可能超过剩余额度，预算不是精确预付费账单。

离线演示使用固定公开修复脚本，无模型调用，token为0。它支持T1/T2/T3原始目标，不能理解任意需求或替代真实模型。local模式只允许loopback端点，关闭环境代理与重定向；remote适配默认禁用，不属于默认验证路径。

## MCP、Skills和记忆

Skills按目标匹配本地技能文本，开启后注入提示；计划与消息随断点保存。长期记忆按任务保存成功路径/失败经验，默认关闭，不能当成未经核查的事实。

每run拥有独立MCP客户端和工具表，保留inputSchema并校验参数；未知副作用默认为HIGH且拒绝。内置demo只读查询当前服务数据目录，关闭时释放进程。自定义命令属于可信操作者配置，不通过Web暴露任意命令输入。

客户端实现stdio JSON-RPC的initialize、通知、tools/list与文本tools/call子集。每行一条JSON的格式在[2024-11-05官方规范](https://modelcontextprotocol.io/specification/2024-11-05/basic/transports)已存在；项目早期Content-Length实现属于实现修正，不能讲成规范迁移。当前本地官方server回归只证明该子集，未覆盖资源、提示、HTTP传输或所有第三方服务。

## 取舍

SQLite与本机文件锁让运行状态容易核查和备份，适合可信操作者的单机工具；无法直接扩成多worker或分布式任务平台。复制执行、超时清理、环境白名单和冻结测试减少误操作，但运行的Python代码仍拥有宿主用户权限，也可能联网。这不是恶意代码安全沙箱，不提供匿名公网执行服务。

操作与API见[operations.md](operations.md)，验证证据见[testing.md](testing.md)，首次运行见[README](../README.md)。
