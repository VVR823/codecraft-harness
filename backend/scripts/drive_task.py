"""通用真 LLM 驱动器：丢任意任务包目录进去，AI 自修到 pytest 全绿。

用法（backend/ 下）：
    python scripts/drive_task.py t2_missing_fn        # 跑 tasks/ 下某个任务包
    python scripts/drive_task.py t3_cross_file
    python scripts/drive_task.py t1_single_fix --skills --mcp --memory   # M5 能力全开
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
SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"
# M5 Skills 仓库（示例技能；`--skills` 时按 goal 语义匹配注入）


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("task_name", help="任务包目录名（tasks/ 下的子目录）")
    ap.add_argument("--model", default=None, help="覆盖 LLM 模型名（默认 config.LLM_MODEL，A/B 用）")
    ap.add_argument("--plan", action="store_true",
                    help="开 M4 planner：执行前先规划一次，计划注入上下文（advisory）")
    ap.add_argument("--skills", action="store_true",
                    help="开 M5 Skills：按 goal 匹配 SKILL.md 注入上下文")
    ap.add_argument("--mcp", action="store_true",
                    help="开 M5 MCP：连 demo server，动态注册其只读工具")
    ap.add_argument("--memory", action="store_true",
                    help="开 M5 长期记忆：注入同任务历史经验 + 结束沉淀")
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
    loop = HarnessLoop(
        task_dir, goal, decider=decider, use_plan=args.plan,
        use_skills=args.skills, skill_dir=str(SKILLS_DIR) if args.skills else None,
        use_mcp=args.mcp,
        use_memory=args.memory,
    )
    print(f"run_id: {loop.run_id} | task: {loop.task_id} | model: {model_name}"
          f" | skills={args.skills} mcp={args.mcp} memory={args.memory}")
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
