from contextlib import closing
from pathlib import Path
import os
import sqlite3
import subprocess
import sys

from fastapi.testclient import TestClient

from app.main import create_app
from app.runtime.settings import Settings
from app.store import db
from app.store.migrations import inspect_schema


def test_health_and_readiness_distinguish_mode_config(tmp_path):
    for mode, expected in [("offline_demo", 200), ("local", 503)]:
        with TestClient(create_app(Settings(data_dir=tmp_path / mode, default_mode=mode))) as client:
            assert client.get("/health").status_code == 200
            assert client.get("/ready").status_code == expected


def test_legacy_store_is_never_silently_migrated_on_start(tmp_path):
    root = tmp_path / "legacy"
    root.mkdir()
    path = root / "harness.db"
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript(db._SCHEMA)
    with TestClient(create_app(Settings(data_dir=root))) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/ready").status_code == 503
        assert client.get("/api/runs").status_code == 503
    assert inspect_schema(path) == 0


def test_explicit_migration_command_creates_readable_backup(tmp_path):
    path = tmp_path / "harness.db"
    backup = tmp_path / "backups"
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript(db._SCHEMA)
    result = subprocess.run(
        [sys.executable, "-m", "app.store.migrations", str(path), "--backup-dir", str(backup)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert inspect_schema(path) == 2
    files = list(backup.glob("*.sqlite3"))
    assert len(files) == 1
    assert inspect_schema(files[0]) == 0


def test_deployment_excludes_secrets_and_persists_whole_data_tree():
    root = Path(__file__).parents[2]
    docker = (root / "Dockerfile").read_text(encoding="utf-8-sig")
    ignored = (root / ".dockerignore").read_text(encoding="utf-8-sig").splitlines()
    compose = (root / "docker-compose.yml").read_text(encoding="utf-8-sig")
    assert "USER codecraft" in docker and "CODECRAFT_MODE=offline_demo" in docker
    assert "127.0.0.1:8000:8000" in compose and "harness_data:/data" in compose
    for pattern in [".git", "**/.env", "**/.env.*", "**/data", "**/node_modules", "**/.mcp-verify"]:
        assert pattern in ignored


def test_docker_uses_allowlisted_context_and_copy_roots():
    import shlex

    root = Path(__file__).parents[2]
    ignored = (root / ".dockerignore").read_text(encoding="utf-8-sig")
    rules = [s.strip() for s in ignored.splitlines() if s.strip() and not s.startswith("#")]
    assert rules[0] == "**"
    assert "!backend/**" not in rules
    docker = (root / "Dockerfile").read_text(encoding="utf-8-sig")
    sources = []
    for line in docker.splitlines():
        if line.startswith("COPY "):
            parts = [part for part in shlex.split(line)[1:] if not part.startswith("--")]
            sources.extend(parts[:-1])
    assert "backend/" not in sources and "." not in sources
    assert all(
        source
        in {
            "backend/app/",
            "backend/tasks/",
            "backend/ui/",
            "backend/skills/",
            "backend/requirements.txt",
            "backend/scripts/drive_task.py",
            "backend/scripts/smoke.py",
            "docker-entrypoint.sh",
        }
        for source in sources
    )


def test_smoke_entry_runs_real_offline_loop(tmp_path):
    result = subprocess.run(
        [sys.executable, "scripts/smoke.py", "--data-dir", str(tmp_path / "data")],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        timeout=40,
    )
    assert result.returncode == 0, result.stderr
    assert '"verified": true' in result.stdout
