import importlib
import json

import pytest

from app.runtime.settings import RunOptions, Settings


def test_structured_option_is_explicit_and_accepted_by_api_body():
    from app.main import TaskCreate

    assert getattr(RunOptions(), "structured_output", None) is False
    body = TaskCreate(task_id="t1_single_fix", mode="local", structured_output=True)
    options = Settings().options(**body.model_dump(exclude={"task_id", "goal"}, exclude_none=True))
    assert options.structured_output is True


@pytest.mark.parametrize("mode", ["offline_demo", "remote"])
def test_structured_option_rejects_nonlocal_modes_before_network(mode):
    with pytest.raises(ValueError, match="local"):
        Settings(base_url="https://example.invalid/v1", allow_remote=True, api_key="test").validate_mode(
            RunOptions(mode=mode, structured_output=True)
        )


@pytest.mark.parametrize("entry", ["cli", "bench"])
def test_explicit_structured_flag_reaches_shared_options(entry, tmp_path, monkeypatch):
    seen = []
    monkeypatch.setenv("CODECRAFT_BASE_URL", "http://127.0.0.1:9999/v1")

    if entry == "cli":
        mod = importlib.import_module("scripts.drive_task")

        class Service:
            def __init__(self, settings):
                pass

            def create(self, task, goal, options, source=None):
                seen.append(options)
                return {"run_id": "cli"}

            def execute(self, run_id):
                return {"run_id": run_id, "status": "paused", "verified": False}

        monkeypatch.setattr(mod, "RunService", Service)
        result = mod.main(["--mode", "local", "--structured-output", "--data-dir", str(tmp_path), "--json"])
        assert result == 2
    else:
        mod = importlib.import_module("app.bench")

        def benchmark(settings, tasks, output, **kwargs):
            seen.append(kwargs["options"])
            return {"samples": 1, "verified_successes": 0}

        monkeypatch.setattr(mod, "run_benchmark", benchmark)
        result = mod.main(
            [
                "--mode",
                "local",
                "--structured-output",
                "--data-dir",
                str(tmp_path),
                "--output",
                str(tmp_path / "report"),
            ]
        )
        assert result == 1
    assert seen[0].structured_output is True


def test_resume_cannot_override_saved_structured_option(tmp_path, monkeypatch):
    mod = importlib.import_module("scripts.drive_task")

    class Service:
        def __init__(self, settings):
            pass

        def resume(self, *args, **kwargs):
            pytest.fail("must reject option override before resuming")

    monkeypatch.setattr(mod, "RunService", Service)
    assert mod.main(["--resume", "saved", "--structured-output", "--data-dir", str(tmp_path)]) == 1


def completion(text):
    return {
        "id": "mock",
        "object": "chat.completion",
        "created": 1,
        "model": "test",
        "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": text}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


def test_sdk_transmits_explicit_schema_without_implicit_retry():
    import httpx
    from app.runtime.model_client import ModelClient

    bodies = []
    schema = {"type": "object", "properties": {"thought": {"type": "string"}}, "required": ["thought"]}

    def handle(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=completion('{"thought":"ok"}'))

    with ModelClient(
        Settings(base_url="http://127.0.0.1:9999/v1"),
        RunOptions(mode="local", structured_output=True),
        http_client=httpx.Client(transport=httpx.MockTransport(handle)),
    ) as client:
        reply = client.chat([{"role": "user", "content": "test"}], response_schema=schema)
    assert reply.total_tokens == 2 and len(bodies) == 1
    assert bodies[0]["response_format"]["type"] == "json_schema"
    assert bodies[0]["response_format"]["json_schema"]["schema"] == schema
    assert bodies[0]["response_format"]["json_schema"]["strict"] is True


@pytest.mark.parametrize("mode", ["local", "remote"])
def test_default_transport_keeps_original_request_format(mode):
    import httpx
    from app.runtime.model_client import ModelClient

    bodies = []

    def handle(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=completion("ok"))

    with ModelClient(
        Settings(
            base_url="http://127.0.0.1:9999/v1" if mode == "local" else "https://example.invalid/v1",
            allow_remote=True,
            api_key="mock-only",
        ),
        RunOptions(mode=mode),
        http_client=httpx.Client(transport=httpx.MockTransport(handle)),
    ) as client:
        assert client.chat([{"role": "user", "content": "test"}]).text == "ok"
    assert len(bodies) == 1 and "response_format" not in bodies[0]


def test_schema_cannot_bypass_explicit_option():
    import httpx
    from app.runtime.model_client import ModelClient

    with ModelClient(
        Settings(base_url="http://127.0.0.1:9999/v1"),
        RunOptions(mode="local"),
        http_client=httpx.Client(
            transport=httpx.MockTransport(lambda _: pytest.fail("must reject before request"))
        ),
    ) as client:
        with pytest.raises(ValueError, match="structured_output"):
            client.chat([], response_schema={"type": "object"})


def test_demo_client_explicitly_rejects_schema_without_running_a_step():
    from app.runtime.demo import DemoClient

    with pytest.raises(ValueError, match="离线"):
        DemoClient("t1_single_fix", "not-created").chat([], response_schema={"type": "object"})


def test_service_limits_schema_to_execution_and_preserves_it_across_resume(tmp_path):
    import httpx
    from jsonschema import validate, ValidationError
    from app.runtime.model_client import ModelClient
    from app.runtime.plan import PLAN_PROMPT
    from app.runtime.run_service import RunService

    bodies = []
    decision = {"thought": "read", "tool": "read_file", "args": {"path": "utils.py"}, "done": False}

    def handle(request):
        body = json.loads(request.content)
        bodies.append(body)
        if body["messages"][0]["content"] == PLAN_PROMPT:
            assert "response_format" not in body
            text = '{"objective":"mock plan","steps":[]}'
        else:
            assert "response_format" in body
            schema = body["response_format"]["json_schema"]["schema"]
            validate(decision, schema)
            validate({"thought": "done", "tool": None, "args": {}, "done": True}, schema)
            with pytest.raises(ValidationError):
                validate({"path": "utils.py", "old": "bad", "new": "good"}, schema)
            text = json.dumps(decision)
        return httpx.Response(200, json=completion(text))

    def factory(settings, options):
        return ModelClient(settings, options, http_client=httpx.Client(transport=httpx.MockTransport(handle)))

    settings = Settings(data_dir=tmp_path / "data", base_url="http://127.0.0.1:9999/v1")
    service = RunService(settings, model_factory=factory)
    run = service.create(
        "t1_single_fix", "", RunOptions(mode="local", structured_output=True, use_plan=True, token_budget=4)
    )
    assert service.execute(run["run_id"])["status"] == "budget_paused"
    fresh = RunService(settings, model_factory=factory)
    assert fresh.get(run["run_id"])["options"]["structured_output"] is True
    result = fresh.resume(run["run_id"], new_budget=8)
    assert result["status"] == "budget_paused" and result["known_tokens"] == 8
    assert result["options"]["structured_output"] is True
    assert sum(body["messages"][0]["content"] == PLAN_PROMPT for body in bodies) == 1
    assert all("response_format" in body for body in bodies if body["messages"][0]["content"] != PLAN_PROMPT)


def test_unsupported_schema_pauses_without_downgrade_or_replay(tmp_path):
    import httpx
    from app.runtime.model_client import ModelClient
    from app.runtime.run_service import RunService

    bodies = []

    def handle(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(400, json={"error": {"message": "Bearer private-server-detail"}})

    service = RunService(
        Settings(data_dir=tmp_path / "data", base_url="http://127.0.0.1:9999/v1"),
        model_factory=lambda settings, options: ModelClient(
            settings, options, http_client=httpx.Client(transport=httpx.MockTransport(handle))
        ),
    )
    run = service.create("t1_single_fix", "", RunOptions(mode="local", structured_output=True))
    result = service.execute(run["run_id"])
    assert result["status"] == "paused" and result["unknown_calls"] == 1
    assert "model_http_error HTTP 400" in result["error_message"]
    assert "private-server-detail" not in str(result)
    assert len(bodies) == 1 and "response_format" in bodies[0]
    with pytest.raises(ValueError, match="用量未知"):
        service.resume(run["run_id"])
    assert len(bodies) == 1
