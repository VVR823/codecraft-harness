from app.store import db
import pytest


def test_started_action_needs_reconciliation():
    db.create_run("crash", "task")
    action = db.begin_action("crash", 1, "edit_file", {"path": "x.py"})
    result = db.inspect_recovery("crash")
    assert result["status"] == "interrupted"
    assert result["can_auto_resume"] is False
    assert action


def test_action_and_checkpoint_commit_together():
    db.create_run("complete", "task")
    action = db.begin_action("complete", 1, "read_file", {"path": "x.py"})
    cp = {
        "run_id": "complete",
        "step": 1,
        "done_actions": [],
        "ctx_messages": [],
        "ctx_summary": "",
        "ws_hash": "hash",
        "tokens_used": 10,
    }
    db.commit_action_checkpoint(action, {"output": "ok", "ok": True, "data": {}}, cp)
    assert db.inspect_recovery("complete")["status"] == "safe"
    assert db.last_checkpoint("complete")["step"] == 1
    assert len(db.list_traces("complete")) == 1
    with pytest.raises(ValueError):
        db.commit_action_checkpoint(action, {"ok": True}, cp)
    assert len(db.list_traces("complete")) == 1


def test_failed_transaction_leaves_only_started_intent(monkeypatch):
    db.create_run("rollback", "task")
    action = db.begin_action("rollback", 1, "write_file", {})
    cp = {
        "run_id": "rollback",
        "step": 1,
        "done_actions": [],
        "ctx_messages": [],
        "ctx_summary": "",
        "ws_hash": "hash",
        "tokens_used": 0,
    }
    original = db._now
    calls = 0

    def fail_checkpoint():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected commit failure")
        return original()

    monkeypatch.setattr(db, "_now", fail_checkpoint)
    with pytest.raises(OSError):
        db.commit_action_checkpoint(action, {"ok": True}, cp)
    assert db.inspect_recovery("rollback")["status"] == "interrupted"
    assert db.list_traces("rollback") == []
    assert db.last_checkpoint("rollback") is None


def test_tool_side_effect_before_checkpoint_is_never_replayed(tmp_path, monkeypatch):
    from app.runtime.loop import HarnessLoop, LoopError
    from app.runtime.protocol import AgentStep

    loop = HarnessLoop(tmp_path, "task", lambda _: pytest.fail("must not decide"))
    db.create_run(loop.run_id, "task")
    loop._execute(AgentStep(thought="write", tool="write_file", args={"path": "x.py", "content": "x=1\n"}), 1)

    def crash(*args):
        raise OSError("injected commit failure")

    monkeypatch.setattr(db, "commit_action_checkpoint", crash)
    with pytest.raises(OSError):
        loop._checkpoint(1)
    with pytest.raises(LoopError):
        loop.resume()
    assert (tmp_path / "x.py").read_text() == "x=1\n"
    assert db.get_run(loop.run_id)["status"] == "interrupted"


def test_rebuild_uses_structured_committed_results(tmp_path):
    from app.runtime.loop import HarnessLoop

    loop = HarnessLoop(tmp_path, "task", lambda _: "")
    db.create_run(loop.run_id, "task")
    db.append_trace(loop.run_id, 1, "run_tests", "", "100 passed", verdict="ok")
    loop._rebuild_verification_state()
    assert loop._last_green_test_step == -1
