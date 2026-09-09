"""MCP 真实生态验证（2026-09-09，W14）：自研 client 对接官方 server。

验证目标：自研 stdio MCP client（app/mcp/mcp_client.py，不引官方 SDK）能否
对接**真实第三方 MCP server**——官方 @modelcontextprotocol/server-everything
（MCP 参考实现，暴露 echo/add 等无副作用工具）。之前 M5 只对过自研 demo
server，生态兼容性是空口；本脚本补上真机证据。

用法：
  1. 装官方 server（首次）：cd backend/.mcp-verify && npm install @modelcontextprotocol/server-everything
  2. 跑验证：python scripts/verify_mcp_real.py
     （自动定位 .mcp-verify/node_modules；CI 用 npx 拉到 runner 缓存后同脚本可复跑）

退出码：0 = 全链通过（握手→tools/list→tools/call×2 全真）；非 0 = 失败。
"""
from __future__ import annotations

import sys
from pathlib import Path

# 允许脚本从 backend/ 下直接跑（python scripts/verify_mcp_real.py）
BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app.mcp.mcp_client import MCPClient, MCPError  # noqa: E402


def _find_everything_js() -> Path:
    """定位官方 server 入口（环境变量优先 → 本地 .mcp-verify → 报错指引）。"""
    env_dir = Path(__import__("os").environ.get(
        "MCP_EVERYTHING_DIR", "")).expanduser()
    candidates = []
    if env_dir.is_dir():
        candidates.append(env_dir / "dist" / "index.js")
    candidates.append(BACKEND / ".mcp-verify" / "node_modules"
                      / "@modelcontextprotocol" / "server-everything"
                      / "dist" / "index.js")
    for c in candidates:
        if c.is_file():
            return c
    sys.exit(
        "找不到官方 everything server 入口。先装：\n"
        f"  cd {BACKEND / '.mcp-verify'} && npm install @modelcontextprotocol/server-everything\n"
        "或用 MCP_EVERYTHING_DIR 指向其安装目录。")


def main() -> int:
    entry = _find_everything_js()
    print(f"官方 server: {entry.parent.parent.name} ({entry})")
    client = MCPClient(server_name="mcp_everything",
                       cmd=["node", str(entry)],
                       read_timeout=30.0)
    try:
        init = client.start()
        proto = init.get("protocolVersion", "?")
        print(f"① initialize 握手 ✅  protocolVersion={proto}")

        tools = client.list_tools()
        names = {t.name for t in tools}
        print(f"② tools/list ✅  {len(tools)} 个官方工具: "
              f"{sorted(names)[:8]}{'...' if len(names) > 8 else ''}")
        need = {"echo", "get-sum"}
        missing = need - names
        if missing:
            print(f"   ✗ 缺官方招牌工具: {missing}")
            return 2

        out = client.call_tool("echo", {"message": "codecraft-harness 对接官方 MCP ✅"})
        print(f"③ tools/call echo ✅  回显: {out.strip()[:60]}")

        out2 = client.call_tool("get-sum", {"a": 21, "b": 21})
        print(f"④ tools/call get-sum ✅  21+21 = {out2.strip()[:60]}")
        if "42" not in out2:
            print("   ✗ get-sum 结果里没有 42")
            return 3

        print("\n全链通过：自研 client 协议实现兼容官方 MCP server，非 demo 自证。")
        return 0
    except MCPError as e:
        print(f"✗ MCP 链路失败: {e}")
        return 4
    finally:
        client.stop()


if __name__ == "__main__":
    sys.exit(main())
