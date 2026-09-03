"""Day4 冒烟：checkpoint/resume——进程被杀后能续跑（MVP 底线 2 最小证据）。

模拟真实故障：
    1. 子进程驱动 T1，跑到第 4 次决策前 os._exit(137)（等同 kill -9，非优雅退出，
       无任何清理；此时每步 checkpoint 已落 SQLite 到 step3）
    2. 主进程看到子进程被杀后，用同一 run_id resume：
       从 checkpoint step=3 续跑（已完成动作不重放），跑完到绿
验证：子进程退出码 137 / resume 从 step4 继续 / 步号连续无重复 / 最终 done 全绿

用法（backend/ 下）：
    python scripts/demo_resume.py            # 主流程
    python scripts/demo_resume.py child      # 子进程模式（内部调用）
"""
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.runtime.loop import HarnessLoop  # noqa: E402

T1 = Path(__file__).resolve().parent.parent / "tasks" / "t1_single_fix"
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
GOAL = "修复 utils.trim_whitespace 的 bug，让 pytest 全绿"

FIXED_UTILS = """def trim_whitespace(text: str) -> str:
    \"\"\"去掉字符串首尾的空白字符（空格、制表符、换行等）。\"\"\"
    return text.strip()
"""


class KillDecider:
    """前 kill_at-1 次正常给剧本；第 kill_at 次调用硬死（模拟进程被杀）。"""

    def __init__(self, script, kill_at):
        self.script = list(script)
        self.kill_at = kill_at
        self.calls = 0

    def __call__(self, messages):
        self.calls += 1
        if self.calls >= self.kill_at:
            sys.stdout.flush()
            os._exit(137)  # 硬死：不执行任何清理，等同 kill -9
        return self.script.pop(0)


def run_child() -> None:
    """子进程：跑 3 步后在下次决策前被杀。"""
    from app.store import db
    db.init_db()
    script = [
        '{"thought": "读实现", "tool": "read_file", "args": {"path": "utils.py"}, "done": false}',
        '{"thought": "跑测试", "tool": "run_tests", "args": {}, "done": false}',
        '{"thought": "再看一眼测试", "tool": "read_file", "args": {"path": "test_utils.py"}, "done": false}',
    ]
    decider = KillDecider(script, kill_at=4)
    loop = HarnessLoop(T1, GOAL, decider)
    print("CHILD_RUN_ID=" + loop.run_id)
    sys.stdout.flush()
    loop.run()  # 会在 decider 第 4 次调用时硬死


def main() -> None:
    print("=== Day4: checkpoint/resume 杀进程续跑验证 ===")
    from app.store import db
    db.init_db()
    proc = subprocess.run([sys.executable, __file__, "child"],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    out = proc.stdout + proc.stderr
    run_id = ""
    for line in out.splitlines():
        if line.startswith("CHILD_RUN_ID="):
            run_id = line.split("=", 1)[1].strip()
    print("子进程退出码:", proc.returncode, "（137 = kill -9 硬死）| run_id:", run_id)
    assert proc.returncode == 137 and run_id, "子进程未按预期被杀"

    cp = db.last_checkpoint(run_id)
    print("被杀时已存档 checkpoint: step", cp["step"] if cp else None)
    assert cp is not None and cp["step"] == 3, "被杀时 checkpoint 未存到 step3"

    # ---- 模拟重启：同一 run_id resume ----
    resume_script = [
        '{"thought": "上次已确认 bug，直接修复", "tool": "write_file", "args": {"path": "utils.py", "content": ' + json.dumps(FIXED_UTILS) + '}, "done": false}',
        '{"thought": "重测", "tool": "run_tests", "args": {}, "done": false}',
        '{"thought": "全绿完成", "done": true}',
    ]
    loop2 = HarnessLoop(T1, GOAL, _seq_decider(resume_script), run_id=run_id)
    result = loop2.resume()

    print("resume 最终状态:", result["status"], "| 总步数:", result["steps"])
    steps = [a["step"] for a in result["actions"]]
    print("done_actions step 序列:", steps)
    checks = {
        "resume 后 done": result["status"] == "done",
        "步号连续无重复(不重放前3步)": sorted(steps) == list(range(1, len(steps) + 1))
        and len(steps) == len(set(steps)) and 1 in steps and 2 in steps and 3 in steps,
        "最后测试全绿": _last_green(result),
    }
    traces = db.list_traces(run_id)
    trace_steps = [t["step"] for t in traces]
    checks["trace 步号连续(1~6,含done)"] = sorted(set(trace_steps)) == list(range(1, 7))
    for k, v in checks.items():
        print("  [%s] %s" % ("PASS" if v else "FAIL", k))

    subprocess.run(["git", "-C", str(REPO_ROOT), "restore", "--",
                    "backend/tasks/t1_single_fix/utils.py"], check=True)
    ok = all(checks.values())
    print("Day4 kill-resume 冒烟：", "PASS" if ok else "FAIL")


def _seq_decider(script):
    holder = {"script": list(script)}

    def decider(messages):
        return holder["script"].pop(0) if holder["script"] else '{"thought": "完成", "done": true}'

    return decider


def _last_green(result) -> bool:
    tests = [a for a in result["actions"] if a["tool"] == "run_tests"]
    return bool(tests) and "exit_code=0" in tests[-1]["result_tail"]


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "child":
        run_child()
    else:
        main()
