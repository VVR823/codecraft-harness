from types import SimpleNamespace

import pytest

from app.tools import registry as reg


class FakeClient:
    def __init__(self, value):
        self.value = value
        self.closed = False

    def call_tool(self, name, args):
        if self.closed:
            raise RuntimeError("closed")
        return self.value

    def stop(self):
        self.closed = True


def tool():
    return SimpleNamespace(
        server="demo",
        name="ping",
        description="ping",
        args_hint="{}",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
    )


def test_per_run_registry_and_reconnect(tmp_path):
    a, b = reg.ToolRegistry(), reg.ToolRegistry()
    ca, cb = FakeClient("a"), FakeClient("b")
    a.register_mcp("demo", ca, [tool()], readonly={"ping"})
    b.register_mcp("demo", cb, [tool()], readonly={"ping"})
    assert a.execute("mcp_demo__ping", tmp_path, {}).output == "a"
    assert b.execute("mcp_demo__ping", tmp_path, {}).output == "b"
    a.close()
    assert ca.closed and not cb.closed
    a.register_mcp("demo", FakeClient("new"), [tool()], readonly={"ping"})
    assert a.execute("mcp_demo__ping", tmp_path, {}).output == "new"


def test_unknown_mcp_side_effect_defaults_to_high(tmp_path):
    r = reg.ToolRegistry()
    r.register_mcp("demo", FakeClient("must-not-run"), [tool()])
    assert r.get("mcp_demo__ping").perm == reg.Perm.HIGH
    with pytest.raises(reg.ToolError):
        r.execute("mcp_demo__ping", tmp_path, {})
