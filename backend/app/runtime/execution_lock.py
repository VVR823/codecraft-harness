"""Cross-process run ownership; the OS releases locks on process exit."""

import sys
import re
from contextlib import contextmanager


class ExecutionBusy(RuntimeError):
    pass


@contextmanager
def execution_lock(data_dir, run_id):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", run_id):
        raise ValueError("非法run_id")
    root = data_dir.resolve() / "locks"
    root.mkdir(parents=True, exist_ok=True)
    with (root / (run_id + ".lock")).open("a+b") as lock:
        lock.seek(0, 2)
        if lock.tell() == 0:
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        try:
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ExecutionBusy("该run已有执行者") from exc
        try:
            yield
        finally:
            lock.seek(0)
            if sys.platform == "win32":
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock, fcntl.LOCK_UN)
