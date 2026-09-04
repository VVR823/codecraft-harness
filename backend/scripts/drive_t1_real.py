"""Day5：真 LLM 驱动 T1 自修（智谱 GLM-4-Flash，需要 backend/.env 的 ZHIPU_API_KEY）。

剧本：HarnessLoop 丢进真 LLM 决策器 -> 自主 读码/跑测/修复/重测 直到 pytest 全绿。
跑完 git restore 还原 utils.py，保持任务包永远是"带 bug 考卷"。
用法（backend/ 下）：python scripts/drive_t1_real.py
"""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.runtime.llm import chat  # noqa: E402
from app.runtime.loop import HarnessLoop  # noqa: E402
from app.store import db  # noqa: E402

T1 = Path(__file__).resolve().parent.parent / "tasks" / "t1_single_fix"
REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def main():
    # 模拟"丢目标"：goal 从任务包 README 现读，不硬编码在脚本里
    goal = (T1 / "README.md").read_text(encoding="utf-8")
    db.init_db()

    loop = HarnessLoop(T1, goal, decider=chat)
    print(f"run_id: {loop.run_id} | task: {loop.task_id} | decider: GLM-4-Flash")
    print("=" * 60)
    result = loop.run()
    print("=" * 60)
    print(f"status: {result['status']} | steps: {result['steps']}")
    if result.get("reason"):
        print("reason:", result["reason"])

    tests = [a for a in result["actions"] if a["tool"] == "run_tests"]
    if tests:
        last = tests[-1]["result_tail"]
        green = "全绿" in last
        print(f"共跑测试 {len(tests)} 次；最后一次全绿: {'PASS' if green else 'FAIL'}")
        print("--- 最后一次测试结果 ---")
        print(last[-500:])
    else:
        print("注意: 全程没有调用 run_tests（异常行为，需排查）")

    # 还原任务包，保持"带 bug 考卷"状态
    try:
        subprocess.run(["git", "-C", str(REPO_ROOT), "restore", "--",
                        "backend/tasks/t1_single_fix/utils.py"], check=True)
        print("已还原 utils.py（考卷重置）")
    except subprocess.CalledProcessError:
        print("提示: utils.py 未被 git 跟踪，跳过自动还原（先 commit 任务包）")

    print("RUN_ID=" + loop.run_id)


if __name__ == "__main__":
    main()
