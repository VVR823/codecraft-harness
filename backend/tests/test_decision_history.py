import json

import httpx
import pytest
from jsonschema import ValidationError, validate

from app.runtime.model_client import ModelClient
from app.runtime.protocol import decision_schema, extract_json_object
from app.runtime.run_service import RunService
from app.runtime.settings import RunOptions, Settings


@pytest.mark.parametrize("structured", [False, True])
@pytest.mark.parametrize("restart", [False, True])
def test_executed_history_obeys_decision_envelope_through_sdk_and_restart(tmp_path, structured, restart):
    decisions = iter(
        [
            {"tool": "read_file", "args": {"path": "utils.py"}},
            {
                "tool": "edit_file",
                "args": {"path": "utils.py", "old": 'return text.strip(" ")', "new": "return text.strip()"},
            },
            {"tool": "run_tests", "args": {}},
            {"tool": None, "args": {}, "done": True},
        ]
    )
    invalid_history = []
    seen_history = []

    def handle(request):
        body = json.loads(request.content)
        terminal_line = next(
            line for line in body["messages"][0]["content"].splitlines() if "当你确认任务已完成" in line
        )
        try:
            terminal = json.loads(extract_json_object(terminal_line))
            validate(terminal, decision_schema())
        except ValidationError as exc:
            invalid_history.append(type(exc).__name__)
        for msg in body["messages"]:
            if msg["role"] != "assistant":
                continue
            try:
                summary = json.loads(msg["content"])
                validate(summary, decision_schema())
            except (json.JSONDecodeError, ValidationError) as exc:
                invalid_history.append(type(exc).__name__)
            else:
                seen_history.append(summary)
                assert summary["thought"] != "private-model-reasoning"
        text = json.dumps({"thought": "private-model-reasoning", "done": False, **next(decisions)})
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"role": "assistant", "content": text}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    def factory(settings, options):
        return ModelClient(settings, options, http_client=httpx.Client(transport=httpx.MockTransport(handle)))

    settings = Settings(data_dir=tmp_path / "data", base_url="http://127.0.0.1:9999/v1")
    service = RunService(settings, model_factory=factory)
    created = service.create(
        "t1_single_fix",
        "",
        RunOptions(mode="local", structured_output=structured, token_budget=4 if restart else 20),
    )
    result = service.execute(created["run_id"])
    if restart:
        assert result["status"] == "budget_paused"
        result = RunService(settings, model_factory=factory).resume(created["run_id"], new_budget=20)
    assert result["status"] == "done" and result["verified"]
    assert not invalid_history
    assert seen_history and any(item["tool"] == "run_tests" for item in seen_history)
