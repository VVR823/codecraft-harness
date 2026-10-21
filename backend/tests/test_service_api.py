from fastapi.testclient import TestClient
import pytest

from app.runtime.settings import Settings


def test_api_executes_same_offline_contract(tmp_path):
    from app.main import create_app

    app = create_app(Settings(data_dir=tmp_path / "api"))
    with TestClient(app) as client:
        response = client.post(
            "/api/tasks", json={"task_id": "t1_single_fix", "mode": "offline_demo", "token_budget": 20}
        )
        assert response.status_code == 200
        run_id = response.json()["run_id"]
        app.state.service.wait(run_id)
        result = client.get(f"/api/tasks/{run_id}").json()
        assert result["status"] == "done" and result["verified"]
        assert result["options"]["token_budget"] == 20
        assert client.post(f"/api/tasks/{run_id}/resume", json={}).status_code == 409
        assert client.get(f"/api/tasks/{run_id}/traces?limit=2&offset=1").json()["total"] >= 4


@pytest.mark.parametrize(
    "payload", [{"task_id": "../../backend"}, {"task_id": "t1_single_fix", "mode": "local"}]
)
def test_api_rejects_before_dispatch(tmp_path, payload):
    from app.main import create_app

    with TestClient(create_app(Settings(data_dir=tmp_path / "data"))) as client:
        assert client.post("/api/tasks", json=payload).status_code == 400
        assert client.get("/api/runs").json()["runs"] == []
