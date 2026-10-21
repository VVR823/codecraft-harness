import importlib

import pytest
import subprocess
import sys


def test_same_run_cannot_be_owned_twice_and_lock_releases(tmp_path):
    mod = importlib.import_module("app.runtime.execution_lock")
    with mod.execution_lock(tmp_path, "run"):
        with pytest.raises(mod.ExecutionBusy):
            with mod.execution_lock(tmp_path, "run"):
                raise AssertionError("second owner entered")
    with mod.execution_lock(tmp_path, "run"):
        pass


def test_lock_excludes_other_process(tmp_path):
    from app.runtime.execution_lock import execution_lock

    code = "from pathlib import Path; from app.runtime.execution_lock import execution_lock; import sys\nwith execution_lock(Path(sys.argv[1]), 'run'): pass"
    with execution_lock(tmp_path, "run"):
        result = subprocess.run([sys.executable, "-c", code, str(tmp_path)], capture_output=True, timeout=15)
        assert result.returncode != 0
        assert b"ExecutionBusy" in result.stderr
    assert (
        subprocess.run(
            [sys.executable, "-c", code, str(tmp_path)], capture_output=True, timeout=15
        ).returncode
        == 0
    )
