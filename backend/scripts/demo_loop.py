"""Day2 冒烟：agent loop v0 全链路（无真 LLM 也能跑）。

用"剧本式 Fake 决策器"预排一个完整自修剧本，验证：
    1) 坏 JSON 触发重试（Q11 重试生效：坏文本被消化，不会直接 failed）
    2) read_file -> run_tests(红) -> write_file(修复) -> run_tests(绿) -> done
    3) 每步 trace/checkpoint 落 SQLite（回放与断点续跑的地基）
跑完自动 git restore 被修复的 module.py，保持仓库干净。

用法（backend/ 目录下）：python scripts/demo_loop.py
"""
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # backend/

from app.runtime.loop import HarnessLoop  # noqa: E402

DEMO_PKG = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "demo_pkg"
REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# 注意：本文件约定不使用任何字面转义序列，多行内容一律真实换行
FIXED_MODULE = """def add(a: int, b: int) -> int:
    return a + b


def sub(a: int, b: int) -> int:
    return a - b
"""


class FakeDecider:
    """按剧本依次吐文本；记录调用次数。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def __call__(self, messages):
        self.calls += 1
        if self.script:
            return self.script.pop(0)
        return '{"thought": "全部完成", "done": true}'


def main():
    script = [
        "我看看这个任务。",   # 坏文本：无 JSON -> 触发 Q11 重试
        '{"thought": "先读模块源码", "tool": "read_file", "args": {"path": "module.py"}, "done": false}',
        '{"thought": "跑测试看红在哪", "tool": "run_tests", "args": {}, "done": false}',
        '{"thought": "修复 sub 的 bug", "tool": "write_file", "args": {"path": "module.py", "content": ' + json.dumps(FIXED_MODULE) + '}, "done": false}',
        '{"thought": "重测确认全绿", "tool": "run_tests", "args": {}, "done": false}',
        '{"thought": "测试全绿，任务完成", "done": true}',
    ]
    decider = FakeDecider(script)
    print("=== agent loop v0 全链路（Fake 决策器）===")
    loop = HarnessLoop(DEMO_PKG, "修复 module.py 里 sub 的 bug，让 pytest 全绿", decider)
    print("run_id:", loop.run_id, "| 任务: 修复 demo_pkg 到 pytest 全绿")
    result = loop.run()

    print("最终状态:", result["status"], "| 步数:", result["steps"], "| 决策器调用次数:", decider.calls)
    for a in result["actions"]:
        print("  step%-2d %-10s %s" % (a["step"], a["tool"], json.dumps(a["args"], ensure_ascii=False)[:70]))

    checks = {
        "状态=done": result["status"] == "done",
        "坏JSON被重试消化(未failed且剧本全消费)": result["status"] == "done" and decider.calls == len(script),
        "发生过 write_file": any(a["tool"] == "write_file" for a in result["actions"]),
        "最后一次 run_tests 全绿": _last_test_green(result),
    }
    from app.store import db as store_db
    traces = store_db.list_traces(result["run_id"])
    cp = store_db.last_checkpoint(result["run_id"])
    checks["trace 落库"] = len(traces) >= 4
    checks["checkpoint 落库"] = cp is not None and cp["step"] == result["steps"]
    print("traces:", len(traces), "条 | checkpoint step:", cp["step"] if cp else None)
    for k, v in checks.items():
        print("  [%s] %s" % ("PASS" if v else "FAIL", k))
    ok = all(checks.values())

    subprocess.run(["git", "-C", str(REPO_ROOT), "restore", "--",
                    "backend/tests/fixtures/demo_pkg/module.py"], check=True)
    print("已 git restore 还原 module.py（工作副本可还原 = 重置机制 OK）")
    print("Day2 loop 冒烟：", "PASS" if ok else "FAIL")


def _last_test_green(result):
    last_tests = [a for a in result["actions"] if a["tool"] == "run_tests"]
    return bool(last_tests) and "exit_code=0" in last_tests[-1]["result_tail"]


if __name__ == "__main__":
    main()
