"""M2 证据脚本（底线 4：预算护栏 超限→暂停→人工批准→续跑）。

真实 LLM（默认免费 glm-4.5-flash）驱动 T1，配一个小预算强制触发暂停：
  1) run() 跑到 budget_paused（审批记录 requested 落 approvals 表）
  2) 打印审计记录，模拟"人工批准"（真实系统 = UI 按钮/审批 API）
  3) resume(approve_budget=True) 从 checkpoint 续跑 → 全绿 done

用法（backend/ 下）：python scripts/demo_budget.py [--budget 3000] [--model glm-4.5-flash]
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.runtime.llm import chat_with_usage  # noqa: E402
from app.runtime.loop import HarnessLoop  # noqa: E402
from app.store import db  # noqa: E402

TASKS = Path(__file__).resolve().parent.parent / "tasks"
REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", default="t1_single_fix")
    ap.add_argument("--model", default="glm-4.5-flash")
    ap.add_argument("--budget", type=int, default=3_000, help="token 预算（故意调小触发暂停）")
    args = ap.parse_args()

    task_dir = TASKS / args.task
    goal = (task_dir / "README.md").read_text(encoding="utf-8")
    db.init_db()

    meter = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "tokens": 0}
    calls = {"n": 0}

    def decider(messages):
        text, usage = chat_with_usage(messages, model=args.model)
        calls["n"] += 1
        # 注意：API usage 只有 prompt/completion/total_tokens，没有 "tokens" key
        meter["prompt_tokens"] += usage.get("prompt_tokens", 0)
        meter["completion_tokens"] += usage.get("completion_tokens", 0)
        meter["total_tokens"] += usage.get("total_tokens", 0)
        meter["tokens"] = meter["total_tokens"]  # 护栏读的 "tokens" 与 total 同步
        return text

    loop = HarnessLoop(task_dir, goal, decider=decider,
                       token_budget=args.budget, meter=meter)
    print(f"run_id: {loop.run_id} | task: {args.task} | model: {args.model} | 预算: {args.budget} token")
    print("=" * 64)

    # 阶段 1：跑到超预算暂停
    t0 = time.monotonic()
    r1 = loop.run()
    dt1 = time.monotonic() - t0
    print(f"[阶段1] status={r1['status']} | steps={r1['steps']} | "
          f"token累计={meter['tokens']} | LLM调用={calls['n']} | {dt1:.0f}s")
    if r1["status"] != "budget_paused":
        print("⚠️ 预算没触发暂停（预算不够小？）。run 状态:", r1["status"])
        return 1
    print("\n--- 审批审计（approvals 表）---")
    for a in db.list_approvals(loop.run_id):
        print(f"  [{a['kind']}] {a['action']}: {a['note']} @{a['ts'][11:19]}")
    assert db.last_approval(loop.run_id, "budget_continue")["action"] == "requested"

    # 阶段 2：模拟人工批准 → 续跑
    print(f"\n[人工批准] resume(approve_budget=True) …")
    t1 = time.monotonic()
    r2 = loop.resume(approve_budget=True)
    dt2 = time.monotonic() - t1
    print(f"[阶段2] status={r2['status']} | steps={r2['steps']} | "
          f"token累计={meter['tokens']} | LLM调用={calls['n']} | {dt2:.0f}s")
    tests = [a for a in r2["actions"] if a["tool"] == "run_tests"]
    green = r2["status"] == "done" and tests and "全绿" in tests[-1]["result_tail"]
    print(f"\n最终全绿: {'✅ PASS' if green else '❌ FAIL'}")
    if green:
        db.bump_usage(loop.run_id, meter["total_tokens"])

    print("\n--- 最终审批审计 ---")
    for a in db.list_approvals(loop.run_id):
        print(f"  [{a['kind']}] {a['action']}: {a['note']}")

    # 还原任务包（保持"带 bug 考卷"态）
    try:
        subprocess.run(["git", "-C", str(REPO_ROOT), "restore", "--",
                        f"backend/tasks/{args.task}"], check=True)
    except subprocess.CalledProcessError:
        pass
    return 0 if green else 1


if __name__ == "__main__":
    raise SystemExit(main())
