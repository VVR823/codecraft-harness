"""通用真 LLM 驱动器：丢任意任务包目录进去，AI 自修到 pytest 全绿。

用法（backend/ 下）：
    python scripts/drive_task.py t2_missing_fn        # 跑 tasks/ 下某个任务包
    python scripts/drive_task.py t3_cross_file
    python scripts/drive_task.py t1_single_fix --skills --mcp --memory   # M5 能力全开
跑完 git restore 还原任务包（保持"带 bug 考卷"态）。

决策退化自动续跑：run 因连续坏 JSON failed（解析/校验类）且模型没写文件时，
自动从最后 checkpoint 续跑换采样重试 ≤AUTO_RESUME_LIMIT 次（T4 run7/11 实证）。

paused 自动续跑：MAX_STEPS 段内步数触顶（真实库任务探索开销大，一段跑不完）
且 run 还没成功 → 同样自动续跑（每段新配额，T4 run12 实证：30 步触顶时已
摸到修复点，旧绝对步数语义下 resume 立即再触顶续不动）。

人工续跑：python scripts/drive_task.py t4_github_pipe_escape --resume RUN_ID
从该 run 最后 checkpoint 续跑一段（不自动循环，看结果决定是否再续）。
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


def _auto_resumable(result: dict) -> bool:
    """值得自动续跑：决策退化 failed（坏 JSON）或 MAX_STEPS 触顶 paused。

    paused 一律续：无论模型是否写过文件（resume 的 ws_hash 漂移检查会拦住
    外部改动的 run；模型自己写入的 run，checkpoint 在写入后保存 → hash 一致
    可续）。触顶说明任务 >30 步且模型仍在正常推进，段配额新开一段。
    """
    return _is_degraded_failure(result) or result.get("status") == "paused"


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
    ap.add_argument("--resume", default=None, metavar="RUN_ID",
                    help="续跑指定 run_id（从最后 checkpoint 跑一段，不自动循环）")
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
        stall_warning=True,   # 周期式空转提醒注入：长 run 侦察空转需主动拉回（T4 run12/14 实证）
        run_id=args.resume,   # 人工续跑复用指定 run_id；新跑为 None（自动生成）
    )
    print(f"run_id: {loop.run_id} | task: {loop.task_id} | model: {model_name}"
          f" | skills={args.skills} mcp={args.mcp} memory={args.memory}")
    print("=" * 60)
    if args.resume:
        print(f"[resume] 人工续跑 {args.resume}（一段 MAX_STEPS 步配额）")
        result = loop.resume()
        auto = 0
    else:
        result = loop.run()
        auto = 0
        while _auto_resumable(result) and auto < AUTO_RESUME_LIMIT:
            auto += 1
            why = (result.get("reason") or result.get("status") or "")[:60]
            print(f"\n[auto-resume {auto}/{AUTO_RESUME_LIMIT}] {why}"
                  f" → 从 checkpoint 续跑一段")
            try:
                result = loop.resume()
            except Exception as e:  # noqa: BLE001 - ws 漂移等续跑被拒：保持原结果
                print(f"[auto-resume] 续跑被拒（{str(e)[:100]}）→ 保持原结果")
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
