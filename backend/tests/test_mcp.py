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
def client(mcp_client_factory, _isolated_db):
    """spawn 连 tmp 隔离库的 demo server（每条用例独立进程，测完必停）。

    隔离库经 conftest init_db 建表（runs/checkpoints…同 schema），只读查询
    断言落在 tmp 库上——不碰 data/harness.db，clone 机器无真库也能跑。
    """
    c = mcp_client_factory("test", db_dir=_isolated_db)
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
    # 对 demo server 白名单库跑只读查询（client fixture 已 --db-dir 指 tmp
    # 隔离库，conftest init_db 建过表 → 必有 runs/checkpoints）
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


# ---------- 读超时（半挂 server 不卡死） ----------

def _hang_client(read_timeout: float = 2.0) -> MCPClient:
    """spawn 半挂 server（握手后对所有请求不回帧）的 client。"""
    py = sys.executable
    server_py = Path(__file__).resolve().parent / "fixtures" / "hang_server.py"
    return MCPClient("hang", [py, str(server_py)], read_timeout=read_timeout)


def test_read_timeout_on_half_dead_server():
    """半挂 server：读应在 read_timeout 内抛 MCPError，不永久阻塞整个 loop。"""
    import time as _time
    from app.mcp.mcp_client import MCPError
    c = _hang_client(read_timeout=2.0)
    t0 = _time.monotonic()
    c.start()  # initialize 正常回；initialized notify 后 server 挂死
    try:
        with pytest.raises(MCPError) as ei:
            c.list_tools()  # 写请求 → 读超时 → 抛错（不是挂死）
        assert ("超时" in str(ei.value)) or ("半挂" in str(ei.value))
        assert c._broken
        assert _time.monotonic() - t0 < 10  # 2s 超时 + 余量，绝非无限阻塞
        # broken 态下任何请求直接拒绝，不再触碰悬挂读线程
        with pytest.raises(MCPError):
            c.list_tools()
    finally:
        c.stop()  # terminate 半挂进程（悬挂读线程随 EOF 退出），不抛
    assert c.proc is None


def test_start_recovers_after_broken():
    """broken 后 start() 自动清掉半挂旧进程再重建（可再次握手）。"""
    from app.mcp.mcp_client import MCPError
    c = _hang_client(read_timeout=2.0)
    c.start()
    with pytest.raises(MCPError):
        c.list_tools()  # 触发 broken
    assert c._broken
    c.start()  # broken → 内部先 stop 旧进程 → 重新 Popen → 握手成功
    assert not c._broken and c.proc is not None
    c.stop()


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
