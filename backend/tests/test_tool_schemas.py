import importlib

import pytest


def test_registry_rejects_unknown_and_invalid_arguments(tmp_path):
    reg = importlib.import_module("app.tools.registry")
    registry = reg.ToolRegistry()
    with pytest.raises(reg.ToolError):
        registry.execute("read_file", tmp_path, {"path": "x", "offset": "not-int"})
    with pytest.raises(reg.ToolError):
        registry.execute("read_file", tmp_path, {"path": "x", "unexpected": True})
    with pytest.raises(reg.ToolError):
        registry.execute("invented_tool", tmp_path, {})


def test_registry_requires_edit_arguments(tmp_path):
    reg = importlib.import_module("app.tools.registry")
    with pytest.raises(reg.ToolError):
        reg.ToolRegistry().execute("edit_file", tmp_path, {"path": "x"})
