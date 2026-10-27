"""轻量沙箱执行器。

设计：
- 把任务包复制进临时目录再执行，避免受测进程污染原任务包。
- 强制子进程输出 utf-8（PYTHONIOENCODING / PYTHONUTF8），避免 Windows GBK 乱码撕烂解析器。
- 超时则杀整个进程树（Windows taskkill /T /F；POSIX 杀进程组），不留孤儿进程。

用法：
    from app.tools.sandbox_exec import run_in_sandbox
    r = run_in_sandbox(task_dir)            # 默认跑 python -m pytest -q
    r = run_in_sandbox(task_dir, cmd=[...], timeout=5)
返回：{exit_code, timed_out, duration_s, stdout, stderr, work_dir}
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import threading
from pathlib import Path

from ..config import SANDBOX_TIMEOUT


def _kill_tree(proc: subprocess.Popen) -> None:
    """杀整个进程树（含子进程），Windows / POSIX 分别处理。"""
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",  # 兼容 taskkill 输出中的混合编码。
            timeout=10,
        )
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def run_in_sandbox(
    task_dir: str | Path,
    cmd: list[str] | None = None,
    timeout: float = SANDBOX_TIMEOUT,
    python: str | None = None,
    baseline_dir=None,
) -> dict:
    """复制 task_dir 到临时隔离目录并执行 cmd（默认 pytest -q）。"""
    task_dir = Path(task_dir)
    if not task_dir.is_dir():
        raise FileNotFoundError(f"task_dir 不存在: {task_dir}")
    python = python or sys.executable
    cmd = cmd or [python, "-m", "pytest", "-q"]

    tmp_root = Path(tempfile.mkdtemp(prefix="harness_sbx_"))
    try:
        work = tmp_root / "work"
        shutil.copytree(task_dir, work)
        if baseline_dir is not None:
            shutil.copytree(baseline_dir, work, dirs_exist_ok=True)

        allowed = {
            "PATH",
            "PATHEXT",
            "SYSTEMROOT",
            "WINDIR",
            "COMSPEC",
            "TEMP",
            "TMP",
            "TMPDIR",
            "LANG",
            "LC_ALL",
        }
        env = {k: v for k, v in os.environ.items() if k.upper() in allowed}
        env["PYTHONIOENCODING"] = "utf-8"  # 强制子进程输出 utf-8
        env["PYTHONUTF8"] = "1"
        env.pop("PYTHONPATH", None)  # 隔离：不让子进程看到宿主 app 包

        start = time.monotonic()
        try:
            kwargs = {}
            if sys.platform == "win32":
                kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                kwargs["start_new_session"] = True  # 独立进程组，便于整组击杀
            proc = subprocess.Popen(
                cmd,
                cwd=work,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                **kwargs,
            )
            buffers = [bytearray(), bytearray()]
            truncated = [False, False]

            def drain(stream, index):
                with stream:
                    while chunk := stream.read(8192):
                        remaining = 65536 - len(buffers[index])
                        buffers[index].extend(chunk[:remaining])
                        if len(chunk) > remaining:
                            truncated[index] = True

            readers = [
                threading.Thread(target=drain, args=(stream, i), daemon=True)
                for i, stream in enumerate((proc.stdout, proc.stderr))
            ]
            for reader in readers:
                reader.start()
            try:
                proc.wait(timeout=timeout)
                timed_out = False
            except subprocess.TimeoutExpired:
                _kill_tree(proc)  # 超时后终止整个进程树
                proc.wait(timeout=10)
                timed_out = True
            for reader in readers:
                reader.join(timeout=5)
            out, err = buffers
            return {
                "exit_code": proc.returncode,
                "timed_out": timed_out,
                "duration_s": round(time.monotonic() - start, 3),
                "stdout": out[:65536].decode("utf-8", errors="replace"),
                "stderr": err[:65536].decode("utf-8", errors="replace"),
                "output_truncated": any(truncated),
                "work_dir": str(work),
            }
        except Exception as e:  # Popen 启动失败等
            return {
                "exit_code": None,
                "timed_out": False,
                "duration_s": round(time.monotonic() - start, 3),
                "stdout": "",
                "stderr": f"sandbox error: {e}",
                "work_dir": str(work),
            }
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)  # 用完即焚隔离区
