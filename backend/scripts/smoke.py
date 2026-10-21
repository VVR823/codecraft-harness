"""No-key smoke: imports SDK, checks health/readiness, runs one offline task."""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
import openai
import jsonschema
from app.main import create_app
from app.runtime.settings import Settings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    args = parser.parse_args()
    assert callable(openai.OpenAI) and callable(jsonschema.validate)
    app = create_app(Settings(data_dir=args.data_dir.resolve()))
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/ready").status_code == 200
        response = client.post("/api/tasks", json={"task_id": "t1_single_fix", "mode": "offline_demo"})
        response.raise_for_status()
        run_id = response.json()["run_id"]
        result = app.state.service.wait(run_id, 30)
        assert result["status"] == "done" and result["verified"], result["error_code"]
        print(json.dumps({"run_id": run_id, "mode": "offline_demo", "verified": True}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
