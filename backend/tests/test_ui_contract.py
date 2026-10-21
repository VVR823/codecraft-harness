from pathlib import Path

from fastapi.testclient import TestClient
from app.main import create_app
from app.runtime.settings import Settings


def test_console_and_showcase_are_separate(tmp_path):
    with TestClient(create_app(Settings(data_dir=tmp_path / "data"))) as client:
        response = client.get("/console/")
        assert response.status_code == 200
        assert 'type="module"' in response.text
        assert client.get("/console/app.js").status_code == 200
    root = Path(__file__).parents[2]
    assert (root / "showcase/index.html").is_file()
    assert "ZHIPU_API_KEY=" not in (root / "showcase/app.js").read_text(encoding="utf-8")
