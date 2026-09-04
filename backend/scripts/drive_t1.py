"""Day3：驱动 T1 任务包自修到绿（Fake 决策器，无需 API Key）。

剧本：读 utils.py -> run_tests(红) -> 修复 -> run_tests(绿) -> done。
跑完 git restore 还原 utils.py，保持任务包永远是"带 bug 考卷"。
用法（backend/ 下）：python scripts/drive_t1.py
"""
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.runtime.loop import HarnessLoop  # noqa: E402

T1 = Path(__file__).resolve().parent.parent / "tasks" / "t1_single_fix"
REPO_ROOT = Path(__file__).resolve().parent.parent.parent

FIXED_UTILS = """def trim_whitespace(text: str) -> str:
    \"\"\"去掉字符串首尾的空白字符（空格、制表符、换行等）。\"\"\"
    return text.strip()
"""


class FakeDecider:
    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def __call__(self, messages):
        self.calls += 1
        return self.script.pop(0) if self.script else '{"thought": "完成", "done": true}'


def main():
    script = [
        '{"thought": "读实现", "tool": "read_file", "args": {"path": "utils.py"}, "done": false}',
        '{"thought": "跑测试看红", "tool": "run_tests", "args": {}, "done": false}',
        '{"thought": "修复: 用无参 strip 去所有空白", "tool": "write_file", "args": {"path": "utils.py", "content": ' + json.dumps(FIXED_UTILS) + '}, "done": false}',
        '{"thought": "重测", "tool": "run_tests", "args": {}, "done": false}',
        '{"thought": "全绿完成", "done": true}',
    ]
    decider = FakeDecider(script)
    loop = HarnessLoop(T1, "修复 utils.trim_whitespace 的 bug，让 pytest 全绿", decider)
    print("run_id:", loop.run_id)
    result = loop.run()
    print("status:", result["status"], "| steps:", result["steps"])
    last_test = [a for a in result["actions"] if a["tool"] == "run_tests"][-1]
    green = "全绿" in last_test["result_tail"]
    print("最后一次 run_tests 全绿:", "PASS" if green else "FAIL")
    print("RUN_ID=" + loop.run_id)
    try:
        subprocess.run(["git", "-C", str(REPO_ROOT), "restore", "--",
                        "backend/tasks/t1_single_fix/utils.py"], check=True)
        print("已还原 utils.py（考卷重置）")
    except subprocess.CalledProcessError:
        print("提示: utils.py 未被 git 跟踪，跳过自动还原（先 commit 任务包）")


if __name__ == "__main__":
    main()
