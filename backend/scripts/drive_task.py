"""通用真 LLM 驱动器：丢任意任务包目录进去，AI 自修到 pytest 全绿。

用法（backend/ 下）：
    python scripts/drive_task.py t2_missing_fn        # 跑 tasks/ 下某个任务包
    python scripts/drive_task.py t3_cross_file
    python scripts/drive_task.py t1_single_fix --skills --mcp --memory   # M5 能力全开
跑完 git restore 还原任务包（保持"带 bug 考卷"态）。

决策退化自动续跑：run 因连续坏 JSON failed（解析/校验类）且模型没写文件时，
自动从最后 checkpoint 续跑换采样重试 ≤AUTO_RESUME_LIMIT 次（T4 run7/11 实证）。
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

# 决策退化自动续跑（T4 run7/11 实证，2026-09-07）：免费模型在长 run 上偶发
# 连续输出坏 JSON（4 连败整局 failed），run7 死在通读后、run11 死在已摸到
# escaped_cells 代码、下一步就能 edit 的节骨眼。步内重试（Q11，喂回错误重出）
# 救不了陷入沟槽的模型；run 级重试 = 从最后 checkpoint 续跑换采样（新 JSON）。
# 仅当失败原因是决策解析/校验类、且续跑不触发 ws_hash 漂移（模型没写文件）时
# 才自动续跑，限 AUTO_RESUME_LIMIT 次防死循环。
AUTO_RESUME_LIMIT = 2
_DEGRADE_MARKERS = ("字段校验失败", "JSON 解析失败", "模型输出为空", "找不到 JSON",
                    "JSON 不是对象")


def _is_degraded_failure(result: dict) -> bool:
    """决策退化失败：status=failed 且 reason 是解析/校验类（非工作区漂移等）。"""
    if result.get("status") != "failed":
        return False
    reason = result.get("reason") or ""
    return any(m in reason for m in _DEGRADE_MARKERS)


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
    auto = 0
    while _is_degraded_failure(result) and auto < AUTO_RESUME_LIMIT:
        auto += 1
        print(f"\n[auto-resume {auto}/{AUTO_RESUME_LIMIT}] 决策退化"
              f"（{(result.get('reason') or '')[:80]}）→ 从 checkpoint 续跑换采样重试")
        try:
            result = loop.resume()
        except Exception as e:  # noqa: BLE001 - ws 漂移等续跑被拒：保持失败结果
            print(f"[auto-resume] 续跑被拒（{str(e)[:100]}）→ 保持原失败结果")
            break
    print("=" * 60)
    print(f"status: {result['status']} | steps: {result['steps']}"
          + (f" | auto-resume: {auto}" if auto else ""))
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
