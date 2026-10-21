import json
from pathlib import Path

import pytest

from app.runtime.loop import HarnessLoop
from app.runtime.run_service import RunService
from app.runtime.settings import RunOptions, Settings
from app.tools.registry import get_tool


def write_args(tool, content, *, changed=False):
    if tool == "write_file":
        return {
            "path": "worker.py",
            "content": content.replace("return 1", "return 2") if changed else content,
        }
    return {"path": "worker.py", "old": "return 1", "new": "return 2" if changed else "return 1"}


@pytest.mark.parametrize("tool", ["write_file", "edit_file"])
def test_identical_content_does_not_touch_the_file(tmp_path, monkeypatch, tool):
    content = "def value():\n    return 1\n"
    (tmp_path / "worker.py").write_text(content, encoding="utf-8")
    with monkeypatch.context() as guard:
        guard.setattr(Path, "write_text", lambda *a, **k: pytest.fail("no-op must not write the file"))
        result = get_tool(tool).handler(tmp_path, write_args(tool, content))
    assert result.ok and result.data["changed"] is False


def prepared(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    content = "def value():\n    return 1\n"
    (source / "worker.py").write_text(content, encoding="utf-8")
    (source / "test_worker.py").write_text(
        "from worker import value\n\ndef test_value():\n    assert value() == 1\n", encoding="utf-8"
    )
    service = RunService(Settings(data_dir=tmp_path / "data", base_url="http://127.0.0.1:9999/v1"))
    created = service.create("t1_single_fix", "验证已有实现", RunOptions(mode="local"), source=source)
    return service, created, content


def loop_for(service, created, decisions):
    script = iter(decisions)

    def decide(_):
        return json.dumps({"thought": "exercise verification", **next(script, {"done": True})})

    return HarnessLoop(Path(created["workspace_path"]), "验证已有实现", decide, run_id=created["run_id"])


@pytest.mark.parametrize("tool", ["write_file", "edit_file"])
def test_noop_after_green_can_finish_without_a_redundant_test(tmp_path, tool):
    service, created, content = prepared(tmp_path)
    with service.database():
        result = loop_for(
            service,
            created,
            [{"tool": "run_tests", "args": {}}, {"tool": tool, "args": write_args(tool, content)}],
        ).run()
    assert result["status"] == "done" and result["verified"]
    assert service.get(created["run_id"])["verified"]


@pytest.mark.parametrize("tool", ["write_file", "edit_file"])
def test_noop_verification_survives_restart_and_checkpoint_rebuild(tmp_path, monkeypatch, tool):
    service, created, content = prepared(tmp_path)
    monkeypatch.setattr("app.runtime.loop.MAX_STEPS", 2)
    with service.database():
        result = loop_for(
            service,
            created,
            [{"tool": "run_tests", "args": {}}, {"tool": tool, "args": write_args(tool, content)}],
        ).run()
    assert result["status"] == "paused"
    fresh = RunService(service.settings)
    assert fresh.get(created["run_id"])["verified"]
    with fresh.database():
        resumed = loop_for(fresh, created, []).resume()
    assert resumed["status"] == "done" and resumed["verified"]


@pytest.mark.parametrize("tool", ["write_file", "edit_file"])
def test_actual_change_after_green_still_requires_new_tests(tmp_path, monkeypatch, tool):
    service, created, content = prepared(tmp_path)
    monkeypatch.setattr("app.runtime.loop.MAX_STEPS", 3)
    with service.database():
        result = loop_for(
            service,
            created,
            [
                {"tool": "run_tests", "args": {}},
                {"tool": tool, "args": write_args(tool, content, changed=True)},
            ],
        ).run()
    assert result["status"] != "done" and not result["verified"]
    assert not service.get(created["run_id"])["verified"]
