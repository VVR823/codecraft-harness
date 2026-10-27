"""An unfinished decision without a tool must use bounded protocol correction."""

import json

import httpx
import pytest

from app.runtime.model_client import ModelClient
from app.runtime.protocol import StepParseError, parse_step
from app.runtime.run_service import RunService
from app.runtime.settings import RunOptions, Settings
from app.store import db


@pytest.mark.parametrize("fields", [{"tool": None}, {}])
def test_unfinished_decision_requires_an_executable_tool(fields):
    with pytest.raises(StepParseError, match="工具"):
        parse_step(json.dumps({"thought": "继续", "args": {}, "done": False, **fields}))


def test_null_tool_completion_remains_valid():
    assert parse_step('{"thought":"完成","tool":null,"args":{},"done":true}').done


def test_repeated_empty_actions_stop_at_the_existing_protocol_retry_limit(tmp_path, monkeypatch):
    from app.runtime.loop import HarnessLoop

    monkeypatch.setattr("app.runtime.loop.LLM_RETRY", 2)
    calls = []

    def decide(messages):
        calls.append(messages)
        return '{"thought":"继续","tool":null,"args":{},"done":false}'

    loop = HarnessLoop(tmp_path, "不能无限重试", decide, run_id="empty-decision")
    result = loop.run()
    assert result["status"] == "failed" and not result["verified"]
    assert len(calls) == 3
    assert not db.committed_actions(loop.run_id)


def test_sdk_empty_action_is_corrected_before_dispatch_without_losing_the_run(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "worker.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    (source / "test_worker.py").write_text(
        "from worker import value\ndef test_value():\n    assert value() == 1\n", encoding="utf-8"
    )
    steps = iter(
        [
            {"tool": "read_file", "args": {"path": "worker.py"}},
            {"tool": None, "args": {}},
            {"tool": "run_tests", "args": {}},
            {"tool": None, "args": {}, "done": True},
        ]
    )
    requests = []

    def handle(request):
        requests.append(json.loads(request.content))
        text = json.dumps({"thought": "执行", "done": False, **next(steps)})
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"role": "assistant", "content": text}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    def factory(settings, options):
        return ModelClient(settings, options, http_client=httpx.Client(transport=httpx.MockTransport(handle)))

    service = RunService(
        Settings(data_dir=tmp_path / "data", base_url="http://127.0.0.1:9999/v1"), model_factory=factory
    )
    run = service.create(
        "t1_single_fix",
        "保持正确实现",
        RunOptions(mode="local", token_budget=20, structured_output=True),
        source=source,
    )
    result = service.execute(run["run_id"])
    assert result["status"] == "done" and result["verified"]
    assert len(requests) == 4
    assert any("合法 JSON 决策" in m["content"] for m in requests[2]["messages"])
    with service.database():
        actions = db.committed_actions(run["run_id"])
        totals = db.call_totals(run["run_id"])
    assert [a["tool"] for a in actions] == ["read_file", "run_tests"]
    assert totals == {"tokens": 8, "calls": 4, "unknown_calls": 0}
