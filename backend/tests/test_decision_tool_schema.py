"""The local generation contract must bind the selected tool to its real inputs."""

import json

import httpx
import pytest
from jsonschema import ValidationError, validate

from app.runtime.model_client import ModelClient
from app.runtime.protocol import decision_schema
from app.runtime.run_service import RunService
from app.runtime.settings import RunOptions, Settings
from app.store import db
from app.tools.registry import Perm, ToolRegistry, ToolSpec


@pytest.mark.parametrize(
    "decision",
    [
        {"tool": "edit_file", "args": {"path": "worker.py", "old": "return value"}},
        {"tool": "write_file", "args": {"path": "worker.py"}},
        {"tool": "read_file", "args": {"path": "worker.py", "offset": "1"}},
        {"tool": "read_file", "args": {"path": "worker.py", "unexpected": True}},
        {"tool": None, "args": {}},
        {"tool": "invented", "args": {}},
        {"tool": "read_file", "args": {"path": "worker.py"}, "done": True},
    ],
)
def test_generation_rejects_incomplete_or_mismatched_tool_decisions(decision):
    with pytest.raises(ValidationError):
        validate({"thought": "修复", "done": False, **decision}, decision_schema())


def test_generation_keeps_valid_edit_delete_page_and_completion():
    schema = decision_schema()
    for decision in [
        {"tool": "edit_file", "args": {"path": "worker.py", "old": "bad", "new": ""}},
        {"tool": "read_file", "args": {"path": "worker.py", "offset": 10, "limit": 2}},
        {"tool": None, "args": {}, "done": True},
    ]:
        validate({"thought": "修复", "done": False, **decision}, schema)


def test_schema_uses_the_current_registry_without_mutating_it_or_exposing_disabled_tools():
    registry = ToolRegistry(github=False)
    registry.tools["mcp_fixture__echo"] = ToolSpec(
        "mcp_fixture__echo",
        "echo",
        '{"text":"hi"}',
        Perm.LOW,
        lambda *_: None,
        {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
    )
    schema = decision_schema(registry)
    validate({"thought": "调用", "tool": "mcp_fixture__echo", "args": {"text": "hi"}, "done": False}, schema)
    for tool, args in [("mcp_fixture__echo", {}), ("git_push", {}), ("install_package", {"package": "x"})]:
        with pytest.raises(ValidationError):
            validate({"thought": "调用", "tool": tool, "args": args, "done": False}, schema)
    assert "properties" not in schema  # llama.cpp unions must own complete object branches.
    schema["oneOf"][1]["properties"]["args"].setdefault("required", []).append("mutation")
    assert "mutation" not in registry.get("read_file").input_schema["required"]


def test_sdk_receives_the_registry_tool_contract(tmp_path):
    bodies = []

    def handle(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": '{"thought":"完成","tool":null,"args":{},"done":true}',
                        }
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    def factory(settings, options):
        return ModelClient(settings, options, http_client=httpx.Client(transport=httpx.MockTransport(handle)))

    service = RunService(
        Settings(data_dir=tmp_path / "data", base_url="http://127.0.0.1:9999/v1"), model_factory=factory
    )
    run = service.create(
        "t1_single_fix", "", RunOptions(mode="local", structured_output=True, token_budget=2)
    )
    service.execute(run["run_id"])
    schema = bodies[0]["response_format"]["json_schema"]["schema"]
    with pytest.raises(ValidationError):
        validate(
            {"thought": "改", "tool": "edit_file", "args": {"path": "x", "old": "x"}, "done": False}, schema
        )


def test_external_schema_refs_fail_before_any_model_request_or_usage_record(tmp_path, monkeypatch):
    registry = ToolRegistry(github=False)
    registry.tools["mcp_fixture__unsafe"] = ToolSpec(
        "mcp_fixture__unsafe",
        "unsafe",
        "{}",
        Perm.LOW,
        lambda *_: None,
        {"$ref": "https://example.invalid/private-schema"},
    )
    monkeypatch.setattr("app.runtime.run_service.ToolRegistry", lambda **_: registry)
    requests = []

    def handle(request):
        requests.append(request)
        pytest.fail("unsafe external refs must not be sent to the model server")

    def factory(settings, options):
        return ModelClient(settings, options, http_client=httpx.Client(transport=httpx.MockTransport(handle)))

    service = RunService(
        Settings(data_dir=tmp_path / "data", base_url="http://127.0.0.1:9999/v1"), model_factory=factory
    )
    run = service.create("t1_single_fix", "", RunOptions(mode="local", structured_output=True))
    result = service.execute(run["run_id"])
    assert result["status"] == "failed" and not result["verified"]
    assert requests == []
    with service.database():
        assert db.call_totals(run["run_id"]) == {"tokens": 0, "calls": 0, "unknown_calls": 0}
