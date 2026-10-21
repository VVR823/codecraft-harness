import importlib
from pathlib import Path

import pytest

from app.runtime.settings import RunOptions, Settings
from app.store import db


@pytest.fixture
def service(tmp_path):
    cls = importlib.import_module("app.runtime.run_service").RunService
    return cls(Settings(data_dir=tmp_path / "data"))


@pytest.mark.parametrize("task", ["t1_single_fix", "t2_missing_fn", "t3_cross_file"])
def test_offline_service_verified_and_source_preserved(service, task, monkeypatch):
    import socket

    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: pytest.fail("network forbidden"))
    source = Path(__file__).parents[1] / "tasks" / task
    before = {p.name: p.read_bytes() for p in source.iterdir() if p.is_file()}
    created = service.create(task, "", RunOptions())
    result = service.execute(created["run_id"])
    assert result["status"] == "done"
    assert result["verified"] is True
    assert result["mode"] == "offline_demo"
    assert result["known_tokens"] == 0 and result["unknown_calls"] == 0
    assert {p.name: p.read_bytes() for p in source.iterdir() if p.is_file()} == before
    with pytest.raises(ValueError):
        service.resume(created["run_id"])


def test_traversal_and_missing_mode_configuration_rejected_before_create(service):
    with pytest.raises(ValueError):
        service.create("../../backend", "", RunOptions())
    with pytest.raises(ValueError):
        service.create("t1_single_fix", "", RunOptions(mode="local"))
    assert service.list() == []


def test_new_run_never_changes_paused_workspace(service):
    a = service.create("t1_single_fix", "", RunOptions())
    p = Path(a["workspace_path"]) / "utils.py"
    p.write_text("x=42\n")
    with service.database():
        db.update_run_status(a["run_id"], "paused")
    b = service.create("t1_single_fix", "", RunOptions())
    assert a["workspace_path"] != b["workspace_path"]
    assert p.read_text() == "x=42\n"


def test_model_exception_is_durable_and_does_not_leak_key(tmp_path):
    cls = importlib.import_module("app.runtime.run_service").RunService

    class Broken:
        def chat(self, messages):
            raise RuntimeError("Bearer secret-test-value")

        def close(self):
            pass

    service = cls(
        Settings(data_dir=tmp_path / "data", base_url="http://127.0.0.1:9999/v1"),
        model_factory=lambda *a: Broken(),
    )
    run = service.create("t1_single_fix", "", RunOptions(mode="local"))
    result = service.execute(run["run_id"])
    assert result["status"] == "paused"
    assert result["error_code"] == "usage_unknown"
    assert result["unknown_calls"] == 1
    assert "secret-test-value" not in str(result)
    fresh = cls(service.settings)
    assert fresh.get(run["run_id"])["error_code"] == "usage_unknown"


@pytest.mark.parametrize(
    "failure,detail",
    [
        ("timeout", "model_timeout"),
        ("connection", "model_connection_error"),
        ("http", "model_http_error HTTP 500"),
        ("invalid_response", "model_response_error"),
        ("unexpected", "model_error"),
        ("missing_usage", "model_usage_unknown"),
    ],
)
def test_safe_model_diagnostic_survives_restart_and_refused_resume(tmp_path, failure, detail):
    import httpx

    from app.runtime.model_client import ModelClient, ModelReply
    from app.runtime.run_service import RunService

    secret = "private-prompt-source Bearer secret-diagnostic-key"
    requests = []

    def handle(request):
        requests.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout(secret, request=request)
        if failure == "connection":
            raise httpx.ConnectError(secret, request=request)
        return httpx.Response(500, json={"error": {"message": secret}}, headers={"x-request-id": secret})

    class Broken:
        def chat(self, messages):
            requests.append(None)
            if failure == "missing_usage":
                return ModelReply(secret)
            if failure == "invalid_response":
                raise ValueError(secret)
            raise RuntimeError(secret)

        def close(self):
            pass

    def factory(settings, options):
        if failure in ("timeout", "connection", "http"):
            return ModelClient(
                settings, options, http_client=httpx.Client(transport=httpx.MockTransport(handle))
            )
        return Broken()

    service = RunService(
        Settings(data_dir=tmp_path / "data", base_url="http://127.0.0.1:9999/v1"),
        model_factory=factory,
    )
    created = service.create("t1_single_fix", "", RunOptions(mode="local"))
    result = service.execute(created["run_id"])
    assert result["status"] == "paused" and result["error_code"] == "usage_unknown"
    assert result["unknown_calls"] == 1 and result["known_tokens"] == 0
    assert detail in result["error_message"]
    assert secret not in str(result) and "secret-diagnostic-key" not in str(result)
    assert len(requests) == 1
    with service.database():
        assert not db.committed_actions(created["run_id"])
        assert not db.list_traces(created["run_id"])
        assert "secret-diagnostic-key" not in (tmp_path / "data/harness.db").read_bytes().decode(
            "utf-8", errors="ignore"
        )
    fresh = RunService(service.settings, model_factory=lambda *a: pytest.fail("must not retry"))
    assert fresh.get(created["run_id"])["error_message"] == result["error_message"]
    with pytest.raises(ValueError, match="用量未知"):
        fresh.resume(created["run_id"])
    assert fresh.get(created["run_id"])["error_message"] == result["error_message"]
    with pytest.raises(ValueError, match="用量未知"):
        fresh.start(created["run_id"], resume=True)
    assert fresh.get(created["run_id"])["error_message"] == result["error_message"]
    assert len(requests) == 1


def test_finite_budget_resumes_with_saved_options(tmp_path):
    from app.runtime.model_client import ModelReply

    cls = importlib.import_module("app.runtime.run_service").RunService

    class Counted:
        def chat(self, messages):
            return ModelReply('{"thought":"inspect","tool":"read_file","args":{"path":"utils.py"}}', 5, 5, 10)

        def close(self):
            pass

    service = cls(
        Settings(data_dir=tmp_path / "data", base_url="http://127.0.0.1:9999/v1"),
        model_factory=lambda *a: Counted(),
    )
    run = service.create(
        "t1_single_fix", "", RunOptions(mode="local", token_budget=10, context_compress=True)
    )
    assert service.execute(run["run_id"])["status"] == "budget_paused"
    with pytest.raises(ValueError):
        service.resume(run["run_id"])
    result = service.resume(run["run_id"], new_budget=20)
    assert result["status"] == "budget_paused" and result["known_tokens"] == 20
    assert result["options"]["context_compress"] is True
    assert result["options"]["token_budget"] == 20


def test_legacy_can_be_viewed_but_not_resumed(service):
    with service.database():
        db.create_run("legacy", "t1_single_fix")
    assert service.get("legacy")["legacy"] is True
    with pytest.raises(ValueError):
        service.resume("legacy")
    assert service.get("legacy")["error_code"] == "legacy_run"


def test_service_mcp_default_is_bound_to_its_database(service, monkeypatch):
    from app.runtime.loop import HarnessLoop

    seen = []
    monkeypatch.setattr(HarnessLoop, "_setup_mcp", lambda loop: seen.extend(loop._mcp_servers))
    run = service.create("t1_single_fix", "", RunOptions(use_mcp=True))
    assert service.execute(run["run_id"])["status"] == "done"
    assert len(seen) == 1
    assert "--db-dir" in seen[0]["cmd"]
    assert str(service.settings.data_dir.resolve()) in seen[0]["cmd"]


def test_invalid_json_retries_stop_at_budget_without_unknown_usage(tmp_path):
    from app.runtime.run_service import RunService
    from app.runtime.model_client import ModelReply

    calls = []

    class Model:
        def chat(self, messages):
            calls.append(messages)
            return ModelReply("invalid-json", 5, 5, 10)

        def close(self):
            pass

    svc = RunService(
        Settings(data_dir=tmp_path / "data", base_url="http://127.0.0.1:9999/v1"),
        model_factory=lambda *a: Model(),
    )
    run = svc.create("t1_single_fix", "", RunOptions(mode="local", token_budget=10))
    result = svc.execute(run["run_id"])
    assert len(calls) == 1
    assert result["status"] == "budget_paused"
    assert result["error_code"] != "usage_unknown" and result["unknown_calls"] == 0
    assert result["approvals"][-1]["action"] == "requested"


@pytest.mark.parametrize("content", [None, "{", "[]", '{"../escape":"invalid"}'])
def test_missing_manifest_persists_specific_recovery_error(service, content):
    run = service.create("t1_single_fix", "", RunOptions())
    path = Path(run["workspace_path"]).parent / "manifest.json"
    if content is None:
        path.unlink()
    else:
        path.write_text(content)
    with pytest.raises(ValueError, match="漂移"):
        service.execute(run["run_id"])
    result = service.get(run["run_id"])
    assert result["status"] == "interrupted" and result["error_code"] == "workspace_drift"
