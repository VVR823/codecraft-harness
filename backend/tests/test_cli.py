import json
import os
from pathlib import Path
import subprocess
import sys


def test_cli_offline_uses_explicit_data_directory(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            "scripts/drive_task.py",
            "t1_single_fix",
            "--mode",
            "offline_demo",
            "--data-dir",
            str(tmp_path / "data"),
            "--json",
        ],
        cwd=Path(__file__).parents[1],
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=40,
    )
    assert result.returncode == 0, result.stderr
    run = json.loads(result.stdout)
    assert run["verified"] is True and run["mode"] == "offline_demo"
    assert Path(run["workspace_path"]).is_relative_to(tmp_path)
