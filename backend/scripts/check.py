"""Offline quality gate. Stops at the first failure; writes no production data."""

import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--unit-only", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    commands = (
        []
        if args.unit_only
        else [
            ["-m", "ruff", "check", "app", "scripts", "tests"],
            ["-m", "ruff", "format", "--check", "app", "scripts", "tests"],
            ["-m", "mypy"],
        ]
    )
    with tempfile.TemporaryDirectory(prefix="codecraft-check-") as temporary:
        commands += [
            [
                "-m",
                "pytest",
                "tests",
                "-q",
                "--tb=short",
                "--cov=app",
                "--cov-report=term-missing",
                "--cov-report=json",
                "--basetemp",
                str(Path(temporary) / "pytest"),
            ]
        ]
        env = {**os.environ, "TEMP": temporary, "TMP": temporary, "PYTHONIOENCODING": "utf-8"}
        for command in commands:
            print("check:", " ".join(command), flush=True)
            result = subprocess.run([sys.executable, *command], cwd=root, env=env)
            if result.returncode:
                return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
