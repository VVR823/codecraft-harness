"""M4 数字④：planner A/B 实测（plan vs no-plan）。

同一任务真 LLM 跑两档：关规划（现有 agentic loop）vs 开规划（先一次 plan phase，
计划注入上下文，执行原样）。统计对比：
- 全绿（两档都要绿——数字④ 前提是"加了规划不破坏 self-repair"）
- 总 token（含 planning；planning tokens 从 plans 表读，单列展示）
- 步数 / LLM 调用次数（plan 档比 no-plan 多 1 次规划调用）
- 耗时

用法（backend/ 下）：
    python scripts/measure_plan.py --tasks t1_single_fix,t2_missing_fn        # 每任务双档各一次
    python scripts/measure_plan.py --task t2_missing_fn --no-plan            # 只跑对照档
    python scripts/measure_plan.py --task t2_missing_fn --plan --repeat 3    # 规划档跑 3 次取中位
"""
import argparse
import statistics
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import DEFAULT_TOKEN_BUDGET  # noqa: E402
from app.runtime.llm import chat_with_usage  # noqa: E402
from app.runtime.loop import HarnessLoop  # noqa: E402
from app.store import db  # noqa: E402

TASKS = Path(__file__).resolve().parent.parent / "tasks"
REPORT_DIR = Path(__file__).resolve().parent.parent / "data"
REPO_ROOT = TASKS.parent.parent


def _restore(task: str):
    """A/B 公平前提：每档起跑前/后都把任务包 git 还原回 bug 态（含删 AI 新建文件）。"""
    for cmd in (
        ["git", "-C", str(REPO_ROOT), "restore", "--", f"backend/tasks/{task}"],
        ["git", "-C", str(REPO_ROOT), "clean", "-fd", "--", f"backend/tasks/{task}"],
    ):
        try:
            subprocess.run(cmd, check=True, capture_output=True)
        except subprocess.CalledProcessError:
            pass


def _run_once(task: str, model: str, use_plan: bool) -> dict:
    """单次真 LLM 跑（内部被 run_one 重试调用）。"""
    _restore(task)  # 起跑前必还原：两档起点一致（同 bug 态）
    task_dir = TASKS / task
    goal = (task_dir / "README.md").read_text(encoding="utf-8")
    usage_acc = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
                 "tokens": 0}  # 与 run_all 同款计量；预算护栏开着，口径真实
    calls = {"n": 0}

    def decider(messages):
        text, usage = chat_with_usage(messages, model=model)
        calls["n"] += 1
        usage_acc["prompt_tokens"] += usage.get("prompt_tokens", 0)
        usage_acc["completion_tokens"] += usage.get("completion_tokens", 0)
        usage_acc["total_tokens"] += usage.get("total_tokens", 0)
        usage_acc["tokens"] = usage_acc["total_tokens"]
        return text

    db.init_db()
    loop = HarnessLoop(task_dir, goal, decider=decider,
                       token_budget=DEFAULT_TOKEN_BUDGET, meter=usage_acc,
                       use_plan=use_plan)
    t0 = time.monotonic()
    try:
        result = loop.run()
    finally:
        _restore(task)  # 跑完必还原任务包回 bug 态（run_all 同标准，防残留污染下一档/回归）
    dt = round(time.monotonic() - t0, 1)
    tests = [a for a in result["actions"] if a["tool"] == "run_tests"]
    green = result["status"] == "done" and tests and "全绿" in tests[-1]["result_tail"]
    plan_row = db.get_plan(loop.run_id)
    return {"task": task, "plan": use_plan, "green": green, "steps": result["steps"],
            "calls": calls["n"], "tokens": usage_acc["total_tokens"],
            "planning_tokens": int(plan_row["planning_tokens"]) if plan_row else 0,
            "plan_status": (plan_row["status"] if plan_row else "-"),
            "status": result["status"],  # done|failed|budget_paused——护栏止损与正常失败可区分
            "duration_s": dt, "run_id": loop.run_id}


def run_one(task: str, model: str, use_plan: bool, max_retry: int = 2) -> dict:
    """真 LLM 跑一次（带自愈重试，与 run_all 同口径）。

    异常抛出的 RuntimeError = 基础设施故障（429 账户级限流 code 1302、空内容、
    网络错误——免费模型服务端高压下都常见），整局冷却后重跑；
    正常返回的 failed/budget_paused = 模型真实决策路径的结果，不重试（如实上报，
    否则会掩盖 plan 带偏/空转的真实信号）。
    """
    for attempt in range(max_retry + 1):
        if attempt:
            print(f"    ↻ 第{attempt}次整局重试（冷却 90s）…", flush=True)
            time.sleep(90)
        try:
            return _run_once(task, model, use_plan)
        except RuntimeError as e:
            msg = str(e)
            if "429" in msg or "速率限制" in msg or "1302" in msg:
                print(f"    ⚠️ LLM 限流（{msg[:70]}…）", flush=True)
                continue
            if "空内容" in msg or "LLM 返回空" in msg:
                print(f"    ⚠️ LLM 返回空内容（服务端高压偶发，{msg[:50]}…）", flush=True)
                continue
            raise
    return {"task": task, "plan": use_plan, "green": False, "steps": 0, "calls": 0,
            "tokens": 0, "planning_tokens": 0, "plan_status": "-", "status": "llm_failed",
            "duration_s": 0, "run_id": "", "error": "LLM 故障重试耗尽"}


# ---------- 多样本统计与即时落盘（O2/O3）----------

def _med(xs: list) -> float:
    return statistics.median(xs) if xs else 0


def _fmt(r: dict) -> str:
    return (f"| {r['task']} | {'plan' if r['plan'] else 'no-plan'} | "
            f"{'✅' if r['green'] else '❌'} | {r['steps']} | {r['calls']} | {r['tokens']} "
            f"| {r['planning_tokens']} | {r.get('status', r.get('error', ''))} "
            f"| {r['duration_s']} | {r['run_id'][:8]} |")


def _render(rows: list[dict], model: str, task_hint: str) -> str:
    """把当前全部样本渲染成 md（明细表 + 按任务/档分组的中位小结）。"""
    lines = [f"# planner A/B 实测（{time.strftime('%Y-%m-%d %H:%M')} | model={model}"
             + (f" | {task_hint}" if task_hint else "") + "）", "",
             "| 任务 | 档 | 全绿 | 步数 | LLM调用 | 总token | 规划token | 状态 | 耗时(s) | run_id |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        lines.append(_fmt(r))
    lines.append("")
    lines.append("## 小结（同档多样本取中位，样本数见括号）")
    # 按 (任务, 档) 分组
    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        groups.setdefault((r["task"], r["plan"]), []).append(r)
    for task in sorted({k[0] for k in groups}):
        base_rs = groups.get((task, False), [])
        plan_rs = groups.get((task, True), [])
        parts = []
        for label, rs in (("no-plan", base_rs), ("plan", plan_rs)):
            if not rs:
                continue
            green_n = sum(1 for x in rs if x["green"])
            parts.append(f"{label} ×{len(rs)}: 绿 {green_n}/{len(rs)} | "
                         f"步数中位 {_med([x['steps'] for x in rs])} | "
                         f"token中位 {_med([x['tokens'] for x in rs]):.0f}")
        line = f"- **{task}**: " + " → ".join(parts)
        if base_rs and plan_rs:
            d_tok = _med([x["tokens"] for x in plan_rs]) - _med([x["tokens"] for x in base_rs])
            line += f" | Δtoken {'+' if d_tok >= 0 else ''}{d_tok:.0f}"
            d_step = _med([x["steps"] for x in plan_rs]) - _med([x["steps"] for x in base_rs])
            line += f" | Δ步数 {'+' if d_step >= 0 else ''}{d_step:.0f}"
        lines.append(line)
    all_green = all(r["green"] for r in rows) and bool(rows)
    lines.append("")
    lines.append(f"**全部全绿: {'✅' if all_green else '❌'}**"
                 f"{'——plan 不破坏 self-repair（数字④前提成立）' if all_green else '——有档未绿，看明细'}")
    return "\n".join(lines)


def _save(rows: list[dict], model: str, stamp: str, task_hint: str = "") -> Path:
    """即时落盘：把当前进度写盘（每局后调用，崩了不丢已跑局）。返回文件路径。"""
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / f"measure_plan_{stamp}.md"
    path.write_text(_render(rows, model, task_hint), encoding="utf-8")
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", default=None, help="单个任务（与 --tasks 二选一）")
    ap.add_argument("--tasks", default=None, help="逗号分隔任务列表（默认全 T1~T3）")
    ap.add_argument("--model", default="glm-4.5-flash")
    ap.add_argument("--repeat", type=int, default=1,
                    help="每档重复次数（多样本取中位，防单次样本运气；默认 1）")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--plan", action="store_true", help="只跑规划档")
    mode.add_argument("--no-plan", dest="noplan", action="store_true", help="只跑对照档")
    args = ap.parse_args()

    if args.task:
        tasks = [args.task]
    elif args.tasks:
        tasks = [t.strip() for t in args.tasks.split(",") if t.strip()]
    else:
        tasks = ["t1_single_fix", "t2_missing_fn", "t3_cross_file"]
    want_plan = not args.noplan
    want_noplan = not args.plan
    if not (want_plan or want_noplan):
        sys.exit("--plan 与 --no-plan 不能同时给")

    print(f"measure_plan: tasks={tasks} | model={args.model} | repeat={args.repeat} | "
          f"{'规划档+对照档' if (want_plan and want_noplan) else ('只规划档' if want_plan else '只对照档')}")
    print("=" * 78)
    rows: list[dict] = []
    stamp = time.strftime("%Y%m%d_%H%M%S")
    report = None
    for task in tasks:
        print(f">>> {task}", flush=True)
        for rep in range(args.repeat):
            label = f"（第{rep + 1}/{args.repeat}次）" if args.repeat > 1 else ""
            if want_noplan:
                print(f"  [{task} 对照 no-plan{label}]", flush=True)
                rows.append(run_one(task, args.model, use_plan=False))
                r = rows[-1]
                print(f"    run={r['run_id'][:8]} | green={r['green']} | steps={r['steps']} "
                      f"| calls={r['calls']} | tokens={r['tokens']} | status={r.get('status', '')}",
                      flush=True)
                report = _save(rows, args.model, stamp)  # 即时落盘：崩了不丢已跑局
            if want_plan:
                print(f"  [{task} 规划 plan{label}]", flush=True)
                rows.append(run_one(task, args.model, use_plan=True))
                r = rows[-1]
                print(f"    run={r['run_id'][:8]} | green={r['green']} | steps={r['steps']} "
                      f"| calls={r['calls']} | tokens={r['tokens']} (+规划{r['planning_tokens']})"
                      f" | status={r.get('status', '')}", flush=True)
                report = _save(rows, args.model, stamp)  # 即时落盘

    print("\n" + "=" * 78)
    md = _render(rows, args.model, "")
    print(md)
    if report is None:
        report = _save(rows, args.model, stamp)
    print(f"\n报告已存: {report}")
    return 0 if all(r["green"] for r in rows) and rows else 1


if __name__ == "__main__":
    raise SystemExit(main())
