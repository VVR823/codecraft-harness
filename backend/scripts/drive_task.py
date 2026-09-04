"""通用真 LLM 驱动器：丢任意任务包目录进去，AI 自修到 pytest 全绿。

用法（backend/ 下）：
    python scripts/drive_task.py t2_missing_fn        # 跑 tasks/ 下某个任务包
    python scripts/drive_task.py t3_cross_file
跑完 git restore 还原任务包（保持"带 bug 考卷"态）。
"""
import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.runtime.llm import chat  # noqa: E402
from app.runtime.loop import HarnessLoop  # noqa: E402
from app.store import db  # noqa: E402

TASKS = Path(__file__).resolve().parent.parent / "tasks"
REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("task_name", help="任务包目录名（tasks/ 下的子目录）")
    ap.add_argument("--model", default=None, help="覆盖 LLM 模型名（默认 config.LLM_MODEL，A/B 用）")
    args = ap.parse_args()

    task_dir = TASKS / args.task_name
    if not task_dir.is_dir():
        sys.exit(f"任务包不存在: {task_dir}")

    goal = (task_dir / "README.md").read_text(encoding="utf-8")
    db.init_db()

    def decider(messages):
        if args.model:
            return chat(messages, model=args.model)
        return chat(messages)

    model_name = args.model or "config 默认"
    loop = HarnessLoop(task_dir, goal, decider=decider)
    print(f"run_id: {loop.run_id} | task: {loop.task_id} | model: {model_name}")
    print("=" * 60)
    result = loop.run()
    print("=" * 60)
    print(f"status: {result['status']} | steps: {result['steps']}")
    if result.get("reason"):
        print("reason:", result["reason"])

    tests = [a for a in result["actions"] if a["tool"] == "run_tests"]
    if tests:
        last = tests[-1]["result_tail"]
        print(f"共跑测试 {len(tests)} 次；最后一次全绿: {'PASS' if '全绿' in last else 'FAIL'}")
        print("--- 最后一次测试结果 ---")
        print(last[-400:])
    else:
        print("注意: 全程没有调用 run_tests")

    try:
        subprocess.run(["git", "-C", str(REPO_ROOT), "restore", "--",
                        f"backend/tasks/{args.task_name}"], check=True)
        print("已还原任务包（考卷重置）")
    except subprocess.CalledProcessError:
        print("提示: 任务包未被 git 跟踪，跳过自动还原")
    print("RUN_ID=" + loop.run_id)


if __name__ == "__main__":
    main()
