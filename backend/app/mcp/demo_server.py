"""MCP demo server（M5-B2，stdio transport，自研）。

演示能力：暴露一个只读工具 `sqlite_query`——在**指定白名单目录**内执行只读
SQLite 查询（SELECT），供 agent 当"外部数据源"调用。权限上对齐本地工具分级：
只读、路径白名单、绝不写库——这是 MCP server 侧的安全自述（可审计）。

协议实现（与 mcp_client 对称）：
- stdio 帧：Content-Length 头 + JSON body
- 处理 initialize → 回 capabilities；tools/list → 工具清单；tools/call → 执行

用法：python -m app.mcp.demo_server（被 client spawn，也可手动起测）
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

_PROTOCOL_VERSION = "2024-11-05"
# 白名单目录：只允许查 data/ 下显式列出的库（默认 harness.db 只读查询）
_DB_DIR = Path(__file__).resolve().parent.parent.parent / "data"
_ALLOWED_DBS = {"harness.db"}

# MCP 工具清单（inputSchema 用 JSON Schema 子集）
_TOOLS = [
    {
        "name": "sqlite_query",
        "description": "对只读 SQLite 库执行 SELECT 查询并返回行（只允许 SELECT，禁止写操作）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "database": {"type": "string",
                             "description": f"库文件名，仅允许白名单: {sorted(_ALLOWED_DBS)}"},
                "sql": {"type": "string", "description": "只读 SELECT 语句"},
            },
            "required": ["database", "sql"],
        },
    },
]


def _read_frame() -> dict | None:
    headers: dict[str, str] = {}
    while True:
        line = sys.stdin.readline()
        if not line:
            return None  # EOF：client 关闭
        line = line.rstrip("\r\n")
        if not line:
            break
        key, _, value = line.partition(":")
        headers[key.strip().lower()] = value.strip()
    length = int(headers.get("content-length", "0"))
    body = sys.stdin.read(length)
    if not body.strip():
        return None
    return json.loads(body)


def _write_frame(msg: dict) -> None:
    body = json.dumps(msg).encode("utf-8")
    sys.stdout.write(f"Content-Length: {len(body)}\r\n\r\n")
    sys.stdout.write(body.decode("utf-8"))
    sys.stdout.flush()


def _handle_call(params: dict, db_dir: Path) -> dict:
    name = params.get("name", "")
    if name != "sqlite_query":
        return {"content": [{"type": "text", "text": f"未知工具: {name}"}],
                "isError": True}
    args = params.get("arguments", {}) or {}
    db_name = str(args.get("database", ""))
    sql = str(args.get("sql", "")).strip()
    # 安全：白名单库名 + 只允许 SELECT 开头
    if db_name not in _ALLOWED_DBS:
        return {"content": [{"type": "text",
                             "text": f"库不在白名单: {db_name}，允许: {sorted(_ALLOWED_DBS)}"}],
                "isError": True}
    if not sql.upper().lstrip().startswith("SELECT"):
        return {"content": [{"type": "text", "text": "只允许 SELECT 查询（server 侧拒绝写操作）"}],
                "isError": True}
    db_path = Path(db_dir) / db_name
    if not db_path.is_file():
        return {"content": [{"type": "text", "text": f"库文件不存在: {db_path}"}],
                "isError": True}
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            cur = conn.execute(sql)
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description] if cur.description else []
        finally:
            conn.close()
    except sqlite3.Error as e:
        return {"content": [{"type": "text", "text": f"查询失败: {e}"}], "isError": True}
    lines = [" | ".join(cols)] + [" | ".join(str(x) for x in r) for r in rows[:20]]
    text = "\n".join(lines) if lines else "(空结果)"
    if len(rows) > 20:
        text += f"\n…(共 {len(rows)} 行，只显示前 20)"
    return {"content": [{"type": "text", "text": text}], "isError": False}


def main(argv: list[str] | None = None) -> int:
    """--db-dir DIR：覆盖白名单库目录（测试传 tmp 隔离库；缺省 data/ 真库）。"""
    argv = sys.argv[1:] if argv is None else argv
    db_dir = _DB_DIR
    if "--db-dir" in argv:
        i = argv.index("--db-dir")
        if i + 1 < len(argv):
            db_dir = Path(argv[i + 1])
    while True:
        msg = _read_frame()
        if msg is None:
            return 0
        method = msg.get("method", "")
        req_id = msg.get("id")
        if method == "initialize":
            _write_frame({"jsonrpc": "2.0", "id": req_id, "result": {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "codecraft-demo-server", "version": "0.1"},
            }})
        elif method == "tools/list":
            _write_frame({"jsonrpc": "2.0", "id": req_id,
                          "result": {"tools": _TOOLS}})
        elif method == "tools/call":
            _write_frame({"jsonrpc": "2.0", "id": req_id,
                          "result": _handle_call(msg.get("params", {}), db_dir)})
        # notifications/* 不响应


if __name__ == "__main__":
    raise SystemExit(main())
