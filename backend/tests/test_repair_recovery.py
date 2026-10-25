"""Real failed writes must recover or stop, without spending a whole run repeating them."""

import json
from pathlib import Path

import pytest

from app.runtime.model_client import ModelReply
from app.runtime.run_service import RunService
from app.runtime.settings import RunOptions, Settings
from app.runtime.workspace import load_snapshot, verify_baseline, workspace_hash
from app.store import db


ORIGINAL = '"""说明（保留文档字符串）。"""\ndef clean(text):\n    return text\n'
FIXED = ORIGINAL.replace("return text", "return text.strip()")


class Script:
    def __init__(self, steps, check=None):
        self.steps = iter(steps)
        self.check = check
        self.calls = 0

    def chat(self, messages, **kwargs):
        self.calls += 1
        if self.check:
            self.check(self.calls, messages)
        step = next(self.steps, {"tool": None, "args": {}, "done": True})
        return ModelReply(json.dumps({"thought": "offline regression", "done": False, **step}), 0, 0, 0)

    def close(self):
        pass


def write(content, path="worker.py"):
    return {"tool": "write_file", "args": {"path": path, "content": content}}


def read():
    return {"tool": "read_file", "args": {"path": "worker.py"}}


def failure(tool="write_file", path="worker.py"):
    if tool == "write_file":
        return write("说明（缺失引号）\ndef clean(text):\n    return text.strip()\n", path)
    return {"tool": "edit_file", "args": {"path": path, "old": "missing original line", "new": "fixed"}}


def setup_service(tmp_path, script):
    source = tmp_path / "source"
    source.mkdir()
    (source / "worker.py").write_text(ORIGINAL, encoding="utf-8")
    (source / "other.py").write_text(ORIGINAL, encoding="utf-8")
    (source / "test_worker.py").write_text(
        "from worker import clean\n\ndef test_clean():\n    assert clean(' ok ') == 'ok'\n",
        encoding="utf-8",
    )
    settings = Settings(data_dir=tmp_path / "data", base_url="http://127.0.0.1:9/v1")
    service = RunService(settings, model_factory=lambda *_: script)
    run = service.create("t1_single_fix", "修实现并保留原测试", RunOptions(mode="local"), source=source)
    return service, run, source


def finish():
    return [write(FIXED), {"tool": "run_tests", "args": {}}]


@pytest.mark.parametrize("tool", ["write_file", "edit_file"])
@pytest.mark.parametrize("reread", [False, True])
def test_three_failed_repairs_pause_before_another_decision_and_persist_state(tmp_path, tool, reread):
    steps = []
    for index in range(3):
        steps.append(failure(tool, "./worker.py" if index == 1 else "worker.py"))
        if reread and index < 2:
            steps.append(read())
    calls_before_pause = len(steps)
    script = Script(steps + finish())
    service, run, source = setup_service(tmp_path, script)
    result = service.execute(run["run_id"])
    assert result["status"] == "paused" and result["error_code"] == "repair_stalled"
    assert not result["verified"]
    assert script.calls == calls_before_pause
    assert (Path(result["workspace_path"]) / "worker.py").read_text(encoding="utf-8") == ORIGINAL
    assert (source / "worker.py").read_text(encoding="utf-8") == ORIGINAL
    with service.database():
        actions = db.committed_actions(run["run_id"])
        cp = db.last_checkpoint(run["run_id"])
        assert db.inspect_recovery(run["run_id"])["can_auto_resume"]
    assert len(actions) == calls_before_pause and cp["step"] == calls_before_pause
    assert "暂停" in json.loads(cp["ctx_messages"])[-1]["content"]
    assert cp["ws_hash"] == workspace_hash(Path(result["workspace_path"]))
    verify_baseline(load_snapshot(Path(result["workspace_path"])))
    restarted = RunService(service.settings)
    assert restarted.get(run["run_id"])["error_code"] == "repair_stalled"
    assert script.calls == calls_before_pause  # Viewing/restarting must not execute anything.


def test_failure_guidance_allows_reread_and_a_unique_full_line_edit(tmp_path):
    def check(calls, messages):
        if calls == 3:
            assert "三引号" in messages[-1]["content"]
            assert "完整" in messages[-1]["content"] and "唯一" in messages[-1]["content"]

    script = Script(
        [
            failure(),
            failure(),
            read(),
            {
                "tool": "edit_file",
                "args": {"path": "worker.py", "old": "    return text\n", "new": "    return text.strip()\n"},
            },
            {"tool": "run_tests", "args": {}},
        ],
        check,
    )
    service, run, source = setup_service(tmp_path, script)
    result = service.execute(run["run_id"])
    assert result["status"] == "done" and result["verified"]
    assert (Path(result["workspace_path"]) / "worker.py").read_text(encoding="utf-8") == FIXED
    assert (source / "worker.py").read_text(encoding="utf-8") == ORIGINAL


def test_actual_successful_change_resets_the_repair_failure_streak(tmp_path):
    script = Script(
        [
            failure(),
            failure(),
            write(ORIGINAL.replace("return text", "return text.rstrip()")),
            failure(),
            failure(),
            *finish(),
        ]
    )
    service, run, _ = setup_service(tmp_path, script)
    result = service.execute(run["run_id"])
    assert result["status"] == "done" and result["verified"]


def test_noop_write_does_not_disguise_repeated_failures_as_progress(tmp_path):
    script = Script([failure(), failure(), write(ORIGINAL), failure(), *finish()])
    service, run, _ = setup_service(tmp_path, script)
    result = service.execute(run["run_id"])
    assert result["status"] == "paused" and result["error_code"] == "repair_stalled"
    assert script.calls == 4


def test_failures_on_different_files_do_not_share_a_counter(tmp_path):
    script = Script([failure(), failure(), failure(path="other.py"), failure(path="other.py"), *finish()])
    service, run, _ = setup_service(tmp_path, script)
    result = service.execute(run["run_id"])
    assert result["status"] == "done" and result["verified"]


def test_explicit_resume_starts_a_new_bounded_repair_segment(tmp_path):
    script = Script([failure(), failure(), failure(), *finish()])
    service, run, _ = setup_service(tmp_path, script)
    paused = service.execute(run["run_id"])
    assert paused["status"] == "paused" and paused["error_code"] == "repair_stalled"
    recovery = Script([failure(), read(), *finish()])
    restarted = RunService(service.settings, model_factory=lambda *_: recovery)
    result = restarted.resume(run["run_id"])
    assert result["status"] == "done" and result["verified"]
    assert not result["error_code"]
    assert recovery.calls == 5
    with restarted.database():
        assert len(db.committed_actions(run["run_id"])) == 7


def test_resume_can_reread_immediately_after_the_failed_repair_pause(tmp_path):
    script = Script([read(), failure(), failure(), failure(), *finish()])
    service, run, _ = setup_service(tmp_path, script)
    assert service.execute(run["run_id"])["error_code"] == "repair_stalled"
    seen = []

    def check(calls, messages):
        if calls == 2:
            seen.append(messages[-1]["content"])

    recovery = Script([read(), *finish()], check)
    restarted = RunService(service.settings, model_factory=lambda *_: recovery)
    result = restarted.resume(run["run_id"])
    assert result["status"] == "done" and result["verified"]
    assert seen[0].startswith("[工具结果 read_file]"), (
        "Recovery needs a fresh read, not stale duplicate blocking"
    )
    with restarted.database():
        actions = db.committed_actions(run["run_id"])
    assert len(actions) == 7 and actions[4]["tool"] == "read_file"


def test_absolute_and_relative_paths_share_the_same_failed_repair_limit(tmp_path):
    script = Script([])
    service, run, _ = setup_service(tmp_path, script)
    absolute_path = str(Path(run["workspace_path"]) / "worker.py")
    script.steps = iter([failure(), failure(path=absolute_path), failure(), *finish()])
    result = service.execute(run["run_id"])
    assert result["status"] == "paused" and result["error_code"] == "repair_stalled"
    assert script.calls == 3
    assert (Path(run["workspace_path"]) / "worker.py").read_text(encoding="utf-8") == ORIGINAL
