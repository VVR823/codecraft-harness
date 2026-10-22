"""CLI startup failures should be actionable and leave old databases untouched."""

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from contextlib import closing

import pytest


@pytest.mark.parametrize("kind", ["legacy", "corrupt"])
def test_cli_reports_storage_failure_without_traceback_or_migration(tmp_path, kind):
    directory = tmp_path / "data"
    directory.mkdir()
    path = directory / "harness.db"
    if kind == "legacy":
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute("CREATE TABLE prior_history(value TEXT)")
    else:
        path.write_bytes(b"not a sqlite database")
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "CODECRAFT_MODE": "offline_demo"}
    result = subprocess.run(
        [sys.executable, "scripts/drive_task.py", "--data-dir", str(directory), "--json"],
        cwd=Path(__file__).parents[1],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
    )
    assert result.returncode == 1
    assert "Traceback" not in result.stderr
    error = json.loads(result.stderr)
    assert error["error_code"] == "storage_unavailable"
    assert "数据目录" in error["error_message"]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
