import importlib

import pytest

from app.tools.registry import ToolError, get_tool


def test_write_and_edit_cannot_weaken_tests(tmp_path):
    test = tmp_path / "test_code.py"
    test.write_text("def test_code():\n    assert 1 == 2\n")
    with pytest.raises(ToolError):
        get_tool("edit_file").handler(
            tmp_path, {"path": "test_code.py", "old": "assert 1 == 2", "new": "assert True"}
        )
    with pytest.raises(ToolError):
        get_tool("write_file").handler(tmp_path, {"path": "conftest.py", "content": "# disable tests"})
    assert "assert 1 == 2" in test.read_text()


def test_syntax_error_does_not_replace_file(tmp_path):
    path = tmp_path / "code.py"
    path.write_text("x = 1\n")
    result = get_tool("edit_file").handler(tmp_path, {"path": "code.py", "old": "x = 1", "new": "def f(:"})
    assert not result.ok
    assert path.read_text() == "x = 1\n"


def test_external_test_drift_is_detected(tmp_path):
    mod = importlib.import_module("app.runtime.workspace")
    source = tmp_path / "source"
    source.mkdir()
    (source / "test_code.py").write_text("def test_code():\n    assert 1 == 2\n")
    snapshot = mod.prepare_workspace(source, "r", tmp_path / "data")
    (snapshot.root / "test_code.py").write_text("def test_code():\n    assert True\n")
    with pytest.raises(ValueError, match="测试"):
        mod.verify_baseline(snapshot)
