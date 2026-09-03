"""Day1 冒烟：沙箱 v0 演示。

用法（backend/ 目录下）：
    python scripts/demo_sandbox.py
预期：
    1) 正常跑 pytest：exit_code=1，stdout 含 "1 failed, 1 passed"（不乱码）
    2) 超时任务：timed_out=True 且 ~2s 返回（进程树被杀干净，不留孤儿）
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # backend/

from app.tools.sandbox_exec import run_in_sandbox  # noqa: E402

DEMO_PKG = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "demo_pkg"


def main() -> None:
    print("=== 1) 正常跑 pytest（预期 exit=1，1 failed 1 passed，无乱码）===")
    r = run_in_sandbox(DEMO_PKG)
    print(f"exit_code={r['exit_code']} | timed_out={r['timed_out']} | duration={r['duration_s']}s")
    print("--- stdout ---")
    print(r["stdout"])
    if r["stderr"].strip():
        print("--- stderr ---")
        print(r["stderr"])

    print("\n=== 2) 超时强杀验证（cmd=sleep 30, timeout=2s，预期 2s 内返回 timed_out=True）===")
    r2 = run_in_sandbox(DEMO_PKG, cmd=[sys.executable, "-c", "import time; time.sleep(30)"], timeout=2)
    print(f"exit_code={r2['exit_code']} | timed_out={r2['timed_out']} | duration={r2['duration_s']}s")
    ok = r2["timed_out"] and r2["duration_s"] < 10
    print(f"=> 进程树被杀干净且未等满 30s: {'PASS' if ok else 'FAIL'}")

    if r["exit_code"] == 1 and "1 failed, 1 passed" in r["stdout"] and ok:
        print("\nDay1 沙箱冒烟：PASS")
    else:
        print("\nDay1 沙箱冒烟：FAIL（见上方输出）")


if __name__ == "__main__":
    main()
