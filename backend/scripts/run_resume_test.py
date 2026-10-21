"""Offline recovery verification; no task/source restore, model requests, or retries."""

from pathlib import Path
import subprocess
import sys

if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    raise SystemExit(
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "tests/test_action_recovery.py",
                "tests/test_execution_lock.py",
                "tests/test_runtime_acceptance.py",
                "-q",
            ],
            cwd=root,
        ).returncode
    )
