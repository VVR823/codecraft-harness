"""MCP 半挂测试 server：initialize 正常回，其它请求一律不回。

模拟真实故障态：server 进程活着（未退出）但对请求不再响应——client 若
裸 readline 会永久阻塞整个 loop；正确行为应超时抛错并报废 client。
"""
import json
import sys
import time


def read_frame():
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        line = line.decode("utf-8", errors="replace").strip()
        if not line:
            break
        key, _, value = line.partition(":")
        headers[key.strip().lower()] = value.strip()
    length = int(headers.get("content-length", "0") or 0)
    body = sys.stdin.buffer.read(length) if length else b""
    return json.loads(body.decode("utf-8", errors="replace")) if body else None


def send(obj):
    body = json.dumps(obj).encode("utf-8")
    sys.stdout.buffer.write(
        f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
    sys.stdout.buffer.flush()


while True:
    req = read_frame()
    if req is None:
        break
    method = req.get("method") or ""
    if method == "initialize":
        # 握手正常（让 client 能过 start()），其余全挂
        send({"jsonrpc": "2.0", "id": req.get("id"),
              "result": {"protocolVersion": "2024-11-05",
                         "capabilities": {"tools": {}},
                         "serverInfo": {"name": "hang", "version": "1"}}})
    else:
        # tools/list / tools/call / notification：半挂——活着但不响应
        time.sleep(3600)
