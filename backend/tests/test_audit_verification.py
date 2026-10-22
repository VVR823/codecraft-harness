"""Regressions discovered by the 2026-10-22 independent boundary audit."""

import json

import pytest

from app.runtime.loop import HarnessLoop
from app.runtime.run_service import RunService
from app.runtime.settings import RunOptions, Settings
from app.store import db
from app.tools.registry import ToolRegistry, ToolResult


def _record_test(run_id, step, *, passed):
    action = db.begin_action(run_id, step, "run_tests", {})
    db.commit_action_checkpoint(
        action,
        {"ok": passed, "output": "passed" if passed else "failed", "data": {"verified": passed}},
        {
            "run_id": run_id,
            "step": step,
            "done_actions": [],
            "ctx_messages": [],
            "ctx_summary": "",
            "ws_hash": "",
            "tokens_used": 0,
        },
    )


def test_failed_reverification_revokes_live_completion(tmp_path, monkeypatch):
    results = iter([True, False])
    registry = ToolRegistry()

    def execute(name, workspace, args):
        success = next(results)
        return ToolResult("passed" if success else "failed", ok=success, data={"verified": success})

    monkeypatch.setattr(registry, "execute", execute)
    decisions = iter(
        [
            {"thought": "verify", "tool": "run_tests"},
            {"thought": "recheck", "tool": "run_tests"},
        ]
    )
    loop = HarnessLoop(
        tmp_path,
        "task",
        lambda _: json.dumps(next(decisions, {"thought": "done", "done": True})),
        registry=registry,
    )
    result = loop.run()
    assert result["status"] != "done"
    assert result["verified"] is False


def test_failed_reverification_is_not_displayed_as_verified_after_restart(tmp_path):
    service = RunService(Settings(data_dir=tmp_path / "data"))
    run_id = service.create("t1_single_fix", "", RunOptions())["run_id"]
    with service.database():
        _record_test(run_id, 1, passed=True)
        _record_test(run_id, 2, passed=False)
    assert RunService(service.settings).get(run_id)["verified"] is False


@pytest.mark.parametrize("latest_passed", [False, True])
def test_resume_rebuild_uses_the_latest_test_result(tmp_path, latest_passed):
    db.create_run("audit-resume", "task", "task")
    _record_test("audit-resume", 1, passed=True)
    _record_test("audit-resume", 2, passed=latest_passed)
    loop = HarnessLoop(tmp_path, "task", lambda _: "", run_id="audit-resume")
    loop._rebuild_verification_state()
    assert bool(loop._verify_before_done()) is (not latest_passed)
