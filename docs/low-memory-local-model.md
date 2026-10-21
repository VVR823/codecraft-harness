# How to 用低内存CPU运行已有7B模型

使用已有GGUF权重和本地推理引擎运行CodeCraft，不购买API额度、不下载新模型。这里给出本机已成功完成一次T2修复的配置，不保证任意16GB电脑或任意任务都能通过。

## 准备条件

- 已按[项目README](../README.md)安装Python依赖。
- 已有可用的7B GGUF权重。本次实测为qwen2.5:7b的Q4_K_M权重，约4.7GB；Ollama权重文件可以没有`.gguf`后缀。
- 已安装提供OpenAI兼容接口、JSON Schema输出并支持`--no-repack`的`llama-server`。先运行`& $serverExe --help`检查参数；本项目不替你安装推理引擎。
- 先保存工作并退出闲置应用。本机实测加载前约5.68GiB可用内存，加载后约1.42GiB；这不是所有机器的最低内存保证。
- 本机端口11435未被占用。服务只绑定127.0.0.1；保留原Ollama、GPT及网络代理，不需要重配它们。

## 1. 启动独立模型服务

在第一个PowerShell终端中，将以下两个路径替换为自己的已有文件。不要把模型别名当成需要重新下载的模型。

```powershell
$serverExe = 'D:\your-tools\llama-server.exe'
$modelFile = 'D:\your-models\qwen2.5-7b.gguf'
& $serverExe --model $modelFile --alias qwen2.5-7b-cpu-norepack `
  --host 127.0.0.1 --port 11435 --no-repack --load-mode mmap `
  --gpu-layers 0 --ctx-size 4096 --batch-size 128 --ubatch-size 64 `
  --parallel 1 --threads 6
```

`--no-repack`关闭CPU权重重排；此前本机曾因约2.9GiB的CPU_REPACK缓冲申请失败而无法加载。`mmap`使用文件映射，CPU模式避开此前GPU加载失败；小批次和单并发限制额外开销。释放应用内存与引擎参数共同构成这次成功条件，不能认定只改一个参数就一定成功。

等待日志出现`model loaded`和本地监听地址，再在第二个终端检查：

```powershell
curl.exe --noproxy "*" http://127.0.0.1:11435/health
```

预期HTTP 200且JSON为`{"status":"ok"}`。健康检查只证明服务已加载，不代表代码修复成功。不要把服务绑定到公网或暴露为匿名代码执行站。

## 2. 显式运行一次T2

在第二个PowerShell终端进入仓库的`backend`目录，使用新的数据与报告目录：

```powershell
$env:CODECRAFT_BASE_URL = 'http://127.0.0.1:11435/v1'
$env:CODECRAFT_MODEL_TIMEOUT = '300'
.\.venv\Scripts\python.exe -m app.bench --mode local `
  --model qwen2.5-7b-cpu-norepack --tasks t2_missing_fn `
  --structured-output --budget 20000 --repeat 1 --retries 0 `
  --data-dir .\data-local-7b-t2 --output .\reports\local-7b-t2
```

这条命令会真实调用本机模型；不会回退远端或自动扩额。每次明确开展新验收时使用新的目录名，保留以前的失败。CPU速度低于此前GPU上的3B模型，本机这次T2约121秒；300秒是每次模型请求的超时，并非整轮任务总时长限制。

## 3. 核对结果并退出

查看`reports/local-7b-t2/report.json`：应同时检查`status=done`、`verified=true`、已知用量、未知调用及工具记录，不能只看退出时一句“完成”。本机实测run `fa828cc35d24`：4工具、7798已知token、0未知，原4项测试通过；导出补丁应用到全新任务副本也4PASS。完整证据见[本地验收记录](local-acceptance-2026-10-20.md)。

用[操作指南](operations.md#导出)的命令导出对应run的补丁。实际任务在私有副本执行，内置源任务和冻结原测试保持不变。结束后在第一个模型终端按`Ctrl+C`退出本次服务，释放模型内存；不需要关闭原Ollama或其他应用。

## 故障处理

| 现象 | 处理 |
| --- | --- |
| 不认识`--no-repack`或`--load-mode` | 当前引擎不支持本配置。先核对其帮助，不删除参数后宣称使用了同一实测配置。 |
| CPU_REPACK或其他内存分配失败 | 核对本次服务确实使用`--no-repack`、可用内存及后台应用；保留日志，不循环重试或强杀未知进程。 |
| 11435被占用 | 选择空闲端口，并同步修改服务启动、健康检查与`CODECRAFT_BASE_URL`。 |
| Schema错误、超时或`usage_unknown` | 保留报告和用量记录，不清零或静默降级重发；按[恢复规则](operations.md#人工恢复一段)处理。 |
| `budget_paused`或原测试仍失败 | 这次尚未完成；不修改测试凑通过，不把历史其他模型的成绩算进这次结果。 |

此方案未修改系统分页文件，也不要求升级内存。不同模型、源码版本和推理引擎的成功记录分开保存；单次T2通过不能证明统一配置下T1/T2/T3全套通过或稳定成功率。

后续已在相同源码、RunOptions及引擎参数下补验T1/T3，并只读复用本次T2结果，固定三任务各一次完成3/3，所有补丁独立重放通过。详情见[固定集报告](evidence/local-7b-fixed-suite-2026-10-20.json)；不是一次连续批次，也不构成稳定成功率估计。
