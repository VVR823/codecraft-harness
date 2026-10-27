# 低内存本地模型运行指南

本指南用于连接已经安装的 OpenAI 兼容本地模型服务。CodeCraft 不自动下载模型，也不会把本地模式切换到远端付费端点。

## 适用条件

- 已有可运行的 7B 量化模型和兼容服务端。
- 模型服务仅绑定 `127.0.0.1`。
- 运行前有足够可用内存，并愿意接受 CPU 推理速度。
- 服务端支持 OpenAI 兼容的聊天接口；结构化输出能力需要单独确认。

低内存机器优先使用内存映射、关闭权重重排、单并发和较小批次。具体参数取决于所用推理引擎，不能直接把其他引擎的参数名照搬过来。

## 启动示例

下面以 `llama-server` 为例，模型路径和可执行文件路径需要替换为本机已有位置：

```powershell
& 'D:\path\to\llama-server.exe' `
  --model 'D:\path\to\model.gguf' `
  --alias local-7b `
  --host 127.0.0.1 --port 11435 `
  --gpu-layers 0 --ctx-size 4096 `
  --batch-size 128 --ubatch-size 64 `
  --parallel 1 --threads 6
```

若引擎提供 `--no-repack` 或内存映射选项，可以在确认帮助文档后使用。启动失败时保留日志并调整参数，不循环重试，也不强制结束无法确认归属的进程。

健康检查：

```powershell
curl.exe --noproxy "*" http://127.0.0.1:11435/health
```

HTTP 200 只表示模型服务已加载，不表示代码修复能力已经通过。

## 运行一次固定任务

在仓库 `backend` 目录使用新的数据和报告目录：

```powershell
$env:CODECRAFT_BASE_URL = 'http://127.0.0.1:11435/v1'
$env:CODECRAFT_MODEL_TIMEOUT = '300'
.\.venv\Scripts\python.exe -m app.bench --mode local `
  --model local-7b --tasks t2_missing_fn `
  --structured-output --budget 20000 --repeat 1 --retries 0 `
  --data-dir .\data-local-7b-t2 --output .\reports\local-7b-t2
```

命令不会自动扩额或回退到远端服务。结果需要同时检查：

- `status=done`；
- `verified=true`；
- 原始测试全部通过；
- 已知 token 和未知调用符合预期；
- 导出补丁能在全新副本中重放。

历史固定集的机器可读结果见 [`local-7b-fixed-suite-2026-10-20.json`](evidence/local-7b-fixed-suite-2026-10-20.json)，解释边界见[评测结果](evaluation.md)。

## 常见问题

| 现象 | 处理 |
| --- | --- |
| 内存分配失败 | 降低上下文和批次，确认内存映射/权重重排配置；不要把失败记录改成成功 |
| 端口被占用 | 使用新的 loopback 端口，并同步修改 `CODECRAFT_BASE_URL` |
| Schema 不受支持 | 关闭结构化输出重新创建独立评测；不要在同一运行中静默降级重发 |
| `usage_unknown` | 保留调用记录并暂停，不能按 0 token 结算 |
| `budget_paused` | 本次任务尚未完成；如需继续，显式设置更大的有限总预算 |
| 测试仍失败 | 保留补丁与日志，不修改原测试凑通过 |

结束后只关闭本次启动且身份可确认的模型进程。不同模型、源码版本、预算和推理引擎的结果应分别记录。
