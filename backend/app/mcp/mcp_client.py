"""MCP 最小客户端（M5-B2，自研 stdio transport，不引官方 SDK）。

MCP = Model Context Protocol，四层概念：协议层（JSON-RPC 2.0）/ transport 层
（stdio / SSE / HTTP）/ client / server。这里实现协议子集 + stdio transport：

- 握手: initialize → initialized notification
- 能力发现: tools/list → 返回 [{name, description, inputSchema}]
- 工具调用: tools/call {name, arguments} → {content:[{type:text, text}]}

帧格式（2026-09-09 真实生态验证驱动升级）：MCP stdio transport 在
2025-03-26 规范起从「LSP 式 Content-Length 头 + body」改为 **newline-delimited
JSON**（每行一个 JSON 消息）——官方 SDK server 只认新格式，旧帧会被当行解析
失败。本实现随官方演进用 jsonl（自研 demo server / 测试 fixture 同步），
协议演进本身是可讲的点：两种帧都实现过、理解其动机。

设计（延续自研浓度叙事）：
- 进程生命周期：spawn server 子进程（python -m app.mcp.demo_server 或官方
  server），父进程退出自动 terminate（Popen + finally），不留孤儿
- 无第三方依赖：json / subprocess / threading 即可
- 生产换官方 SDK 是半小时的事，但协议理解不依赖 SDK——这是面试可讲的点
"""
from __future__ import annotations

import json
import queue
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

# stdio 帧：newline-delimited JSON（MCP 2025-03-26+ 规范）
def _encode(msg: dict) -> bytes:
    return json.dumps(msg, ensure_ascii=False).encode("utf-8") + b"\n"


class MCPError(Exception):
    pass


@dataclass
class MCPTool:
    server: str        # server 名（注册表命名空间用）
    name: str          # 原始工具名
    description: str
    args_hint: str     # 简化 inputSchema → 给 LLM 的示例 JSON


class MCPClient:
    """stdio MCP client：连一个 server 子进程，暴露工具发现与调用。"""

    def __init__(self, server_name: str, cmd: list[str],
                 read_timeout: float = 15.0):
        self.server_name = server_name
        self.cmd = cmd
        self.read_timeout = read_timeout   # 单帧读超时（秒）；半挂 server 兜底
        self._cwd: str | None = None    # spawn_client 会设为 backend/（保证 -m 可导入）
        self.proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._req_id = 0
        self._broken = False   # 上次读超时 → client 报废，需 stop 后重建

    # ---------- 生命周期 ----------
    def start(self) -> None:
        if self._broken:
            self.stop()  # 清掉半挂进程与其悬挂读线程（stop 后 _broken 由下方重置）
        if self.proc is not None and self.proc.poll() is None:
            return  # 已在跑
        self._broken = False
        self.proc = subprocess.Popen(
            self.cmd,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, bufsize=0,
            cwd=self._cwd,
        )
        # 握手：initialize + initialized 通知（MCP 协议要求）
        init = self._request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "codecraft-harness", "version": "0.1"},
        })
        self._notify("notifications/initialized")
        return init

    def stop(self) -> None:
        if self.proc is not None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=3)
            except Exception:  # noqa: BLE001
                try:
                    self.proc.kill()
                except Exception:  # noqa: BLE001
                    pass
            self.proc = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *a):
        self.stop()

    # ---------- JSON-RPC 底层 ----------
    def _request(self, method: str, params: dict) -> dict:
        if self._broken:
            raise MCPError(f"MCP client {self.server_name} 处于 broken 态"
                           "（上次读超时，悬挂读线程未清），请 stop() 后重建")
        if self.proc is None or self.proc.poll() is not None:
            raise MCPError(f"MCP server {self.server_name} 未启动")
        self._req_id += 1
        req = {"jsonrpc": "2.0", "id": self._req_id,
               "method": method, "params": params}
        try:
            with self._lock:
                self.proc.stdin.write(_encode(req))
                self.proc.stdin.flush()
                while True:
                    resp = self._read_response()
                    # 跳过 server 主动推送的通知帧（JSON-RPC 无 id，如
                    # notifications/tools/list_changed）——直到读到本请求的响应
                    # （2026-09-09 官方 server 对接实证：everything 握手后会推
                    # list_changed，旧实现把它当响应读 → tools/list 得空）
                    if resp.get("id") == self._req_id:
                        break
        except (BrokenPipeError, OSError) as e:
            raise MCPError(f"MCP server {self.server_name} 通信失败: {e}") from e
        if "error" in resp:
            raise MCPError(f"MCP {method} 错误: {resp['error']}")
        return resp.get("result", {})

    def _notify(self, method: str) -> None:
        if self.proc is None:
            return
        msg = {"jsonrpc": "2.0", "method": method, "params": {}}
        with self._lock:
            self.proc.stdin.write(_encode(msg))
            self.proc.stdin.flush()

    def _timed_read(self, fn, what: str, timeout: float):
        """带超时执行一次管道读（daemon 线程 + join）。

        为什么用线程：Windows 的 select 不支持 pipe，跨平台可靠做法就是
        daemon 线程 + join(timeout)。超时（server 进程活着但不回帧=半挂）→
        抛 MCPError 并标记 _broken：
        - 悬挂读线程不 kill（无法 kill）——由调用方 stop() 终止 server 进程，
          管道 EOF 后线程自然退出，不留竞态
        - _broken 期间 _request 直接拒绝，防新请求与悬挂线程抢同一管道
        """
        q: queue.Queue = queue.Queue(maxsize=1)

        def _read():
            try:
                q.put(fn())
            except BaseException as e:  # noqa: BLE001 —— 读异常也回传，不静默
                q.put(e)

        t = threading.Thread(target=_read, daemon=True,
                             name=f"mcp-read-{self.server_name}")
        t.start()
        t.join(timeout)
        if t.is_alive():
            self._broken = True
            raise MCPError(
                f"MCP server '{self.server_name}' {what}超时（>{timeout:g}s），"
                "疑似半挂；client 已标记 broken，需 stop() 后重建")
        got = q.get()
        if isinstance(got, BaseException):
            raise MCPError(
                f"MCP server '{self.server_name}' 读管道失败: {got}") from got
        return got

    def _read_response(self) -> dict:
        """读一帧：jsonl 一行 = 一个 JSON 消息。超时保护（真实现，见 _timed_read）。"""
        line = self._timed_read(self.proc.stdout.readline, "响应",
                                self.read_timeout)
        if not line:
            raise MCPError(f"MCP server {self.server_name} 提前退出")
        text = line.decode("utf-8", errors="replace").strip()
        if not text:
            # 空行不该出现在 jsonl 帧流里——当协议异常处理（防死循环）
            raise MCPError(f"MCP server {self.server_name} 返回空行（帧协议异常）")
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise MCPError(
                f"MCP server {self.server_name} 返回非法 JSON 帧: {e} | {text[:200]}") from e

    # ---------- MCP 能力 ----------
    def list_tools(self) -> list[MCPTool]:
        """tools/list → 转成 MCPTool（注册表可直接吃）。"""
        result = self._request("tools/list", {})
        out: list[MCPTool] = []
        for t in result.get("tools", []):
            schema = t.get("inputSchema", {})
            props = schema.get("properties", {})
            required = schema.get("required", [])
            hint = {k: _json_type(v) for k, v in props.items()}
            for k in required:
                hint[k] = hint.get(k, "?")
            out.append(MCPTool(
                server=self.server_name,
                name=t.get("name", ""),
                description=t.get("description", ""),
                args_hint=json.dumps(hint, ensure_ascii=False),
            ))
        return out

    def call_tool(self, name: str, arguments: dict) -> str:
        """tools/call → 取 content 文本拼成结果（和本地工具同形态的 str）。"""
        result = self._request("tools/call", {"name": name, "arguments": arguments})
        parts = []
        for item in result.get("content", []):
            if item.get("type") == "text":
                parts.append(item.get("text", ""))
        if result.get("isError"):
            raise MCPError(f"MCP {name} 执行错误: {' '.join(parts) or '(无消息)'}")
        return "\n".join(parts)


def _json_type(v: dict) -> str:
    """inputSchema 属性 → 示例值（给 LLM 看参数形状，够用即可）。"""
    t = v.get("type", "string")
    if t == "integer":
        return "0"
    if t == "boolean":
        return "true"
    return "string"


def spawn_client(server_name: str, module: str = "app.mcp.demo_server") -> MCPClient:
    """spawn 一个 server 的 client（cmd = 当前 python -m <module>）。

    cwd 固定到 backend/（app 包根）：`python -m app.mcp.demo_server` 需要包在
    sys.path 上，不依赖调用方当前目录（脚本/API/测试都可能在不同 cwd 起进程）。
    """
    import sys as _sys
    backend = Path(_sys.argv[0]).resolve().parent if "__main__" in str(_sys.argv[0]) else None
    # 更稳：直接从本文件推导 backend/ = app/mcp 的上两级
    backend_dir = Path(__file__).resolve().parent.parent.parent
    py = Path(_sys.executable)
    client = MCPClient(server_name, [str(py), "-m", module])
    client._cwd = str(backend_dir)
    return client
