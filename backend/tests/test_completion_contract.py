import json

from app.runtime.loop import HarnessLoop
from app.store import db


def test_done_without_any_verification_is_not_success(tmp_path):
    result = HarnessLoop(tmp_path, "task", lambda _: '{"thought":"done","done":true}').run()
    assert result["status"] != "done"
    assert result["verified"] is False


def test_zero_tests_is_not_success(tmp_path):
    script = iter([{"thought": "test", "tool": "run_tests", "args": {}}])

    def decide(_):
        return json.dumps(next(script, {"thought": "done", "done": True}))

    result = HarnessLoop(tmp_path, "task", decide).run()
    assert result["status"] != "done"


def test_saved_plan_is_reinjected_without_checkpoint(tmp_path):
    from app.runtime.plan import PLAN_PROMPT

    db.create_run("saved-plan", "task", "task")
    db.save_plan("saved-plan", json.dumps({"objective": "unique-saved-plan", "steps": []}))
    seen = []

    def decide(messages):
        assert messages[0]["content"] != PLAN_PROMPT
        seen.append(messages)
        return '{"thought":"done","done":true}'

    HarnessLoop(tmp_path, "task", decide, run_id="saved-plan", use_plan=True).resume()
    assert "unique-saved-plan" in seen[0][1]["content"]
