"""MCP 通知帧测试 server（2026-09-09 W14 新增）。

模拟真实第三方 server（官方 everything）的行为：握手后**主动推送**
notifications/tools/list_changed——旧版 client 把它当请求响应读 → 拿空。
本 server 每次回响应前都先推一条通知，验证 client 能跳过通知帧、
等到匹配 id 的真实响应。
"""
import json
import sys


def read_frame():
    line = sys.stdin.buffer.readline()
    if not line:
        return None
    text = line.decode("utf-8", errors="replace").strip()
    if not text:
        return None
    return json.loads(text)


def send(obj):
    sys.stdout.buffer.write(
        json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n")
    sys.stdout.buffer.flush()


_TOOLS = [
    {"name": "ping", "description": "回显",
     "inputSchema": {"type": "object",
                     "properties": {"msg": {"type": "string"}},
                     "required": ["msg"]}},
]

while True:
    req = read_frame()
    if req is None:
        break
    method = req.get("method") or ""
    req_id = req.get("id")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": req_id, "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {"listChanged": True}},
            "serverInfo": {"name": "notify-server", "version": "1"}}})
    else:
        # 每个请求响应前都先推一条通知（真实 server 的 list_changed 行为）
        send({"jsonrpc": "2.0",
              "method": "notifications/tools/list_changed"})
        if method == "tools/list":
            send({"jsonrpc": "2.0", "id": req_id,
                  "result": {"tools": _TOOLS}})
        elif method == "tools/call":
            msg = (req.get("params", {}).get("arguments", {}) or {}).get("msg", "")
            send({"jsonrpc": "2.0", "id": req_id, "result": {
                "content": [{"type": "text", "text": f"pong:{msg}"}]}})
        # notifications/* 不响应
