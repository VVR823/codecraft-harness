import importlib

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.runtime.settings import Settings


def test_api_token_and_spoofed_forwarding_headers(tmp_path):
    with TestClient(create_app(Settings(data_dir=tmp_path / "data", api_token="test-only-token"))) as client:
        assert client.get("/api/runs").status_code == 401
        assert client.post("/api/tasks", json={"task_id": "t1_single_fix"}).status_code == 401
        assert client.get("/api/runs", headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert client.get("/api/runs", headers={"Authorization": "Bearer test-only-token"}).status_code == 200
        assert client.get("/health").status_code == 200


def test_rate_limit_uses_peer_and_recovers_with_clock(tmp_path):
    now = [0.0]
    app = create_app(Settings(data_dir=tmp_path / "data"), clock=lambda: now[0])
    with TestClient(app) as client:
        for i in range(5):
            assert (
                client.post(
                    "/api/tasks", json={"task_id": "missing"}, headers={"X-Forwarded-For": str(i)}
                ).status_code
                == 400
            )
        blocked = client.post("/api/tasks", json={"task_id": "missing"})
        assert blocked.status_code == 429 and int(blocked.headers["Retry-After"]) > 0
        now[0] = 61
        assert client.post("/api/tasks", json={"task_id": "missing"}).status_code == 400


def test_limiter_capacity_and_idle_eviction():
    limiter = importlib.import_module("app.api_security").RateLimiter(max_clients=2)
    assert limiter.consume("a", 0)[0] and limiter.consume("b", 0)[0]
    assert not limiter.consume("c", 1)[0]
    assert limiter.consume("c", 121)[0]
    assert len(limiter.clients) <= 2


@pytest.mark.parametrize(
    "path",
    [
        "/api/runs?limit=101",
        "/api/runs?limit=0",
        "/api/tasks/nope/traces?limit=201",
        "/api/tasks/nope/traces?offset=-1",
    ],
)
def test_pagination_bounds(tmp_path, path):
    with TestClient(create_app(Settings(data_dir=tmp_path / "data"))) as client:
        assert client.get(path).status_code == 422


def test_body_limit_even_without_content_length(tmp_path):
    with TestClient(create_app(Settings(data_dir=tmp_path / "data"))) as client:
        response = client.post(
            "/api/tasks",
            content=iter([b'{"goal":"', b"x" * 66000, b'"}']),
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 413
        assert (
            client.post("/api/tasks", json={"task_id": "t1_single_fix", "goal": "x" * 20001}).status_code
            == 422
        )
        assert (
            client.post("/api/tasks", json={"task_id": "t1_single_fix", "model": "x" * 129}).status_code
            == 422
        )


def test_active_run_limit_no_persistent_queue(tmp_path):
    import threading
    from app.runtime.run_service import RunService
    from app.runtime.settings import RunOptions
    from app.runtime.model_client import ModelReply

    entered, release = threading.Event(), threading.Event()

    class Model:
        def chat(self, messages):
            entered.set()
            assert release.wait(10)
            return ModelReply('{"thought":"read","tool":"read_file","args":{"path":"utils.py"}}', 5, 5, 10)

        def close(self):
            pass

    svc = RunService(
        Settings(data_dir=tmp_path / "data", base_url="http://127.0.0.1:9999/v1"),
        model_factory=lambda *a: Model(),
    )
    first = svc.submit("t1_single_fix", "", RunOptions(mode="local", token_budget=10))
    second = svc.submit("t1_single_fix", "", RunOptions(mode="local", token_budget=10))
    try:
        assert entered.wait(5)
        with TestClient(create_app(service=svc)) as client:
            assert client.post("/api/tasks", json={"task_id": "t1_single_fix"}).status_code == 429
            assert len(client.get("/api/runs").json()["runs"]) == 2
    finally:
        release.set()
        svc.wait(first["run_id"])
        svc.wait(second["run_id"])
