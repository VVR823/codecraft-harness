"""M5-B2 MCP 单测：client 握手/工具发现/调用（连真 demo server）+ 协议放行 + 注册表路由。"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.mcp.mcp_client import MCPClient, spawn_client  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_registry():
    """每用例前后清掉动态注册的 mcp_ 工具（全局注册表，避免用例间污染）。"""
    from app.tools import registry
    yield
    for n in list(registry.list_tool_names()):
        if n.startswith("mcp_"):
            registry.TOOLS.pop(n, None)


@pytest.fixture()
def client():
    """spawn 真 demo server 的 client（每条用例独立进程，测完必停）。"""
    c = spawn_client("test")
    c.start()
    yield c
    c.stop()


# ---------- client 生命周期 + 协议 ----------

def test_handshake_and_list_tools(client: MCPClient):
    tools = client.list_tools()
    names = [t.name for t in tools]
    assert "sqlite_query" in names
    t = tools[0]
    assert t.server == "test"
    assert "database" in t.args_hint and "sql" in t.args_hint


def test_call_tool_readonly_query(client: MCPClient):
    # 对 demo server 白名单库（harness.db）跑只读查询（init_db 过必有 runs 表）
    out = client.call_tool("sqlite_query",
                           {"database": "harness.db",
                            "sql": "SELECT name FROM sqlite_master WHERE type='table'"})
    assert "runs" in out and "checkpoints" in out


def test_call_tool_rejects_write(client: MCPClient):
    with pytest.raises(Exception) as ei:
        client.call_tool("sqlite_query",
                         {"database": "harness.db",
                          "sql": "DELETE FROM runs"})
    assert "只允许 SELECT" in str(ei.value)


def test_call_tool_rejects_non_whitelist_db(client: MCPClient):
    with pytest.raises(Exception) as ei:
        client.call_tool("sqlite_query",
                         {"database": "evil.db", "sql": "SELECT 1"})
    assert "白名单" in str(ei.value)


def test_stop_terminates_child():
    c = spawn_client("test2")
    c.start()
    proc = c.proc
    assert proc.poll() is None
    c.stop()
    assert proc.poll() is not None
    assert c.proc is None  # stop 后引用清空


# ---------- 协议放行（mcp_ 前缀） ----------

def test_protocol_allows_mcp_prefix_tool():
    from app.runtime.protocol import AgentStep
    s = AgentStep(thought="查外部数据", tool="mcp_demo__sqlite_query", args={})
    assert s.tool_name == "mcp_demo__sqlite_query"


def test_protocol_rejects_random_tool_name():
    from app.runtime.protocol import AgentStep, StepParseError, parse_step
    with pytest.raises(Exception):  # Pydantic ValidationError
        AgentStep(thought="x", tool="hack_tool")
    with pytest.raises(StepParseError):
        parse_step('{"thought":"x","tool":"hack_tool","args":{},"done":false}')


def test_protocol_builtin_tool_still_enum():
    from app.runtime.protocol import AgentStep, Tool
    s = AgentStep(thought="读文件", tool="read_file")
    assert isinstance(s.tool, Tool)
    assert s.tool_name == "read_file"


# ---------- 注册表路由 + loop 集成 ----------

def test_register_mcp_tools_into_registry(client: MCPClient):
    from app.tools import registry
    tools = client.list_tools()
    n = registry.register_mcp_tools("test", client, tools)
    assert n == 1
    name = "mcp_test__sqlite_query"
    assert registry.get_tool(name) is not None
    assert name in registry.describe_tools()  # 动态工具进 system 说明书
    # handler 真转发：注册后直接调
    spec = registry.get_tool(name)
    result = spec.handler(Path("."), {"database": "harness.db",
                                      "sql": "SELECT count(*) FROM runs"})
    assert result.ok and "count(*)" in result.output


def test_loop_mcp_system_prompt_includes_tool():
    """use_mcp=True 的 loop，初始 system 说明书含 mcp_ 工具；False 不含（口径不动）。"""
    from app.runtime.loop import HarnessLoop
    from app.mcp.mcp_client import spawn_client

    class FakeDecider:
        def __call__(self, messages):
            return None

    pkg = Path(__file__).resolve().parent / "fixtures" / "demo_pkg"
    # 不开 MCP：纯基线（避免污染，先确认全局注册表此刻没有 mcp_ 前缀残留）
    loop_off = HarnessLoop(pkg, "读文件即可", decider=FakeDecider(), use_mcp=False)
    assert "mcp_" not in loop_off._system_prompt()
    # 开 MCP：手动 setup 后 system 含工具（独立 server 名 loopmcp，避免跨用例残留干扰）
    c = spawn_client("loopmcp")
    c.start()
    from app.tools import registry
    registry.register_mcp_tools("loopmcp", c, c.list_tools())
    try:
        loop_on = HarnessLoop(pkg, "读文件即可", decider=FakeDecider(), use_mcp=True)
        sp = loop_on._system_prompt()  # use_mcp=True → 动态 describe_tools() 含已注册 mcp 工具
        assert "mcp_loopmcp__sqlite_query" in sp
    finally:
        c.stop()
