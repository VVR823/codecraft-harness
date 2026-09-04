"""工具注册表行为测试：权限分级 + edit_file 精准编辑 + 语法体检。"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.tools.registry import (  # noqa: E402
    Perm,
    ToolError,
    describe_tools,
    get_tool,
    list_tool_names,
)


@pytest.fixture
def ws(tmp_path):
    """带一个可编辑 .py 的工作区。"""
    (tmp_path / "utils.py").write_text(
        'def trim_whitespace(text: str) -> str:\n'
        '    """去空白。"""\n'
        '    return text.strip(" ")  # bug\n',
        encoding="utf-8")
    return tmp_path


# ---------- 注册表结构 ----------

def test_all_tools_have_unique_perm_defined():
    names = list_tool_names()
    assert {"read_file", "edit_file", "write_file", "run_tests"} <= set(names)
    for n in names:
        spec = get_tool(n)
        assert spec.name == n and spec.perm in Perm
    # 权限分级关系：写 > 读；run_tests 走沙箱是 LOW
    assert get_tool("write_file").perm > get_tool("read_file").perm
    assert get_tool("edit_file").perm == get_tool("write_file").perm == Perm.MED
    assert get_tool("run_tests").perm == Perm.LOW


def test_unknown_tool_returns_none():
    assert get_tool("rm_rf") is None


def test_describe_tools_covers_all():
    doc = describe_tools()
    for n in list_tool_names():
        assert n in doc


# ---------- edit_file ----------

def test_edit_file_success(ws):
    r = get_tool("edit_file").handler(ws, {
        "path": "utils.py",
        "old": 'return text.strip(" ")  # bug',
        "new": "return text.strip()",
    })
    assert r.ok and "已替换" in r.output
    assert 'return text.strip()' in (ws / "utils.py").read_text(encoding="utf-8")


def test_edit_file_old_not_found(ws):
    with pytest.raises(ToolError, match="找不到"):
        get_tool("edit_file").handler(ws, {
            "path": "utils.py", "old": "不存在的行", "new": "x"})


def test_edit_file_old_not_unique(ws):
    (ws / "dup.py").write_text("a = 1\nb = 1\n", encoding="utf-8")
    with pytest.raises(ToolError, match="不唯一"):
        get_tool("edit_file").handler(ws, {
            "path": "dup.py", "old": "= 1", "new": "= 2"})


def test_edit_file_empty_old_rejected(ws):
    with pytest.raises(ToolError, match="不能为空"):
        get_tool("edit_file").handler(ws, {"path": "utils.py", "old": " ", "new": "x"})


def test_edit_file_outside_workspace_blocked(ws):
    with pytest.raises(ToolError, match="越界"):
        get_tool("edit_file").handler(ws, {
            "path": "../escape.py", "old": "x", "new": "y"})


# ---------- 语法体检（write & edit 共用） ----------

def test_write_file_syntax_error_reported(ws):
    r = get_tool("write_file").handler(ws, {
        "path": "bad.py", "content": "def f(:\n    pass"})
    assert "[语法检查失败]" in r.output   # ok 仍为 True（已落盘，靠提示让模型自查）
    r2 = get_tool("write_file").handler(ws, {
        "path": "good.py", "content": "def f():\n    return 1\n"})
    assert "[语法检查失败]" not in r2.output


def test_edit_file_syntax_error_reported(ws):
    r = get_tool("edit_file").handler(ws, {
        "path": "utils.py",
        "old": 'return text.strip(" ")  # bug',
        "new": "return text.strip("})
    assert "[语法检查失败]" in r.output
