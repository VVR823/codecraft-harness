import threading

import pytest

from app.runtime.execution_lock import ExecutionBusy
from app.runtime.model_client import ModelReply
from app.runtime.run_service import RunService
from app.runtime.settings import RunOptions, Settings
from app.store import db


def test_competing_services_only_one_dispatches(tmp_path):
    entered, release = threading.Event(), threading.Event()

    class Blocking:
        def chat(self, messages):
            entered.set()
            assert release.wait(10)
            return ModelReply('{"thought":"read","tool":"read_file","args":{"path":"utils.py"}}', 5, 5, 10)

        def close(self):
            pass

    settings = Settings(data_dir=tmp_path / "data", base_url="http://127.0.0.1:9999/v1")
    first = RunService(settings, model_factory=lambda *a: Blocking())
    second = RunService(settings, model_factory=lambda *a: pytest.fail("duplicate owner"))
    run = first.create("t1_single_fix", "", RunOptions(mode="local", token_budget=10))
    try:
        first.start(run["run_id"])
        assert entered.wait(5)
        with pytest.raises(ExecutionBusy):
            second.start(run["run_id"], resume=True)
    finally:
        release.set()
        first.wait(run["run_id"])
    assert first.get(run["run_id"])["status"] == "budget_paused"


def test_restarting_service_does_not_execute_pending_or_running(tmp_path):
    settings = Settings(data_dir=tmp_path / "data")
    first = RunService(settings)
    run = first.create("t1_single_fix", "", RunOptions())
    with first.database():
        db.update_run_status(run["run_id"], "running")
    fresh = RunService(settings, model_factory=lambda *a: pytest.fail("restart must not run"))
    assert fresh.get(run["run_id"])["status"] == "running"
    assert fresh.get(run["run_id"])["model_calls"] == 0


def test_model_request_intent_survives_lost_result(tmp_path):
    svc = RunService(Settings(data_dir=tmp_path / "data"))
    run = svc.create("t1_single_fix", "", RunOptions())
    with svc.database():
        db.begin_model_call(run["run_id"], "lost", "execute")
        db.update_run_status(run["run_id"], "running")
    assert svc.get(run["run_id"])["unknown_calls"] == 1
    with pytest.raises(ValueError):
        svc.resume(run["run_id"])


def test_safe_checkpoint_running_after_restart_can_resume_without_replay(tmp_path, monkeypatch):
    from app.runtime import loop

    monkeypatch.setattr(loop, "MAX_STEPS", 2)
    settings = Settings(data_dir=tmp_path / "data")
    first = RunService(settings)
    run = first.create("t1_single_fix", "", RunOptions())
    assert first.execute(run["run_id"])["status"] == "paused"
    with first.database():
        before = [a["action_id"] for a in db.committed_actions(run["run_id"])]
        db.update_run_status(run["run_id"], "running")
    fresh = RunService(settings)
    assert fresh.resume(run["run_id"])["status"] == "paused"
    assert fresh.resume(run["run_id"])["status"] == "done"
    with fresh.database():
        actions = db.committed_actions(run["run_id"])
        assert [a["action_id"] for a in actions[:2]] == before
        assert len(actions) == 4
