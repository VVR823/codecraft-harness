"""Exported patches must be usable by a person without editing the diff."""

from pathlib import Path
import subprocess

import pytest

from app.export_run import export_run
from app.runtime.run_service import RunService
from app.runtime.settings import RunOptions, Settings
from app.store import db


@pytest.mark.parametrize(
    "before,after",
    [
        ("value = 1", "value = 2"),
        ("value = 1", "value = 2\n"),
        ("value = 1\n", "value = 2"),
        (None, "value = 2\n"),
        (None, "value = 2"),
        (None, ""),
        ("", None),
        ("value = 1\n", None),
        ("value = 1\n", "value = 2\n"),
        ("value = 1\r\n", "value = 2\r\n"),
        ("value = 1\r\n", "value = 2\n"),
        ("value = 1\n", "value = 2\r\n"),
    ],
)
@pytest.mark.parametrize("filename", ["module.py", "目录/a file.py"])
def test_exported_patch_applies_to_the_original_file(tmp_path, before, after, filename):
    source = tmp_path / "source"
    source.mkdir()
    original = source / filename
    original.parent.mkdir(parents=True, exist_ok=True)
    if before is not None:
        original.write_bytes(before.encode())
    service = RunService(Settings(data_dir=tmp_path / "data", base_url="http://127.0.0.1:9999/v1"))
    run = service.create("t1_single_fix", "task", RunOptions(mode="local"), source=source)
    target = Path(run["workspace_path"]) / filename
    if after is None:
        target.unlink()
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(after.encode())
    with service.database():
        db.update_run_status(run["run_id"], "paused")
    export_directory = tmp_path / "export"
    bundle = export_run(service, run["run_id"], export_directory)
    patch = export_directory / "changes.patch"
    applied = subprocess.run(
        ["git", "-c", "core.autocrlf=false", "apply", "--", str(patch)],
        cwd=source,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert applied.returncode == 0, applied.stderr + "\n" + bundle["patch"]
    if after is None:
        assert not original.exists()
    else:
        assert original.read_bytes() == after.encode()
