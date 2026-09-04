"""run_all：T1~T3 端到端回归表（数字① / 执行计划 Day6~7）。

每个任务包丢进真 LLM 跑 repeat 次，统计：
- 全绿次数 / 成功率
- token 用量（llm 调用累计，落 usage 表）→ 预算基数（中位数 ×1.5，M2 用）
- 步数 / 耗时 / 失败原因 / 重试次数

自愈：某一局因瞬时故障（免费模型偶发 400/429、偶发坏动作）未全绿，
自动 git 还原任务包重跑，最多 --retries 次（默认 2）。重试透明计入报告。

模型建议：
- 免费且稳健：glm-4.5-flash（实测 6/6 全绿，配 edit_file + 强制验证 + 重复动作护栏）
- 付费更强：glm-4-air-250414（单次 run 分钱级，T2/T3 一次过）

用法（backend/ 下）：
    python scripts/run_all.py --model glm-4.5-flash --repeat 2
输出：控制台表格 + data/run_all_report.md/json（含时间戳，可留档）。
"""
import argparse
import json
import statistics
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
ALL_TASKS = ["t1_single_fix", "t2_missing_fn", "t3_cross_file"]

REPORT_DIR = Path(__file__).resolve().parent.parent / "data"


def _restore_task(task_name: str) -> None:
    """git 还原任务包到"带 bug 考卷"态（重试安全：每次跑前都从干净态开始）。"""
    try:
        subprocess.run(["git", "-C", str(REPO_ROOT), "restore", "--",
                        f"backend/tasks/{task_name}"], check=True,
                       capture_output=True)
    except subprocess.CalledProcessError:
        pass


def run_once(task_name: str, model: str) -> dict:
    """跑一个任务一次，返回结构化结果。"""
    _restore_task(task_name)  # 确保从干净 bug 态开始（重试安全）
    task_dir = TASKS / task_name
    goal = (task_dir / "README.md").read_text(encoding="utf-8")
    usage_acc = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    calls = {"n": 0}

    def decider(messages):
        text, usage = chat_with_usage(messages, model=model)
        calls["n"] += 1
        for k in usage_acc:
            usage_acc[k] += usage.get(k, 0)
        return text

    db.init_db()
    loop = HarnessLoop(task_dir, goal, decider=decider)
    start = time.monotonic()
    result = loop.run()
    duration_s = round(time.monotonic() - start, 1)

    # 全绿判定：status=done 且最后一次 run_tests 报告全绿（防"没跑测试就宣布完成"）
    green = False
    tests = [a for a in result["actions"] if a["tool"] == "run_tests"]
    if result["status"] == "done" and tests:
        green = "全绿" in tests[-1]["result_tail"]
    if green:
        db.bump_usage(loop.run_id, usage_acc["total_tokens"])

    # 还原任务包（保持"带 bug 考卷"态）
    try:
        subprocess.run(["git", "-C", str(REPO_ROOT), "restore", "--",
                        f"backend/tasks/{task_name}"], check=True)
    except subprocess.CalledProcessError:
        pass

    return {
        "task": task_name, "run_id": loop.run_id,
        "status": result["status"], "green": green,
        "steps": result["steps"],
        "reason": (result.get("reason") or "")[:120],
        "llm_calls": calls["n"],
        "tokens": usage_acc["total_tokens"],
        "duration_s": duration_s,
    }


def run_once_with_retry(task_name: str, model: str, max_retry: int = 2) -> dict:
    """跑一个任务，未全绿则整局重试（git 还原后重跑），最多 max_retry 次。

    返回含 retried 字段（实际重试次数）。瞬时故障（400/429/偶发坏动作）靠重试自愈；
    持续故障（模型不可用/任务包真 bug）最终仍记失败——不掩盖真实问题。
    """
    last: dict | None = None
    for attempt in range(max_retry + 1):
        if attempt > 0:
            print(f"    ↻ 第{attempt}次重试（上一局未全绿）…", flush=True)
        try:
            last = run_once(task_name, model)
        except Exception as e:  # 整局崩（如 LLM 持续拒请求）→ 算失败，进重试
            last = {"task": task_name, "run_id": "ERR", "status": "exception",
                    "green": False, "steps": 0, "reason": str(e)[:120],
                    "llm_calls": 0, "tokens": 0, "duration_s": 0}
        last["retried"] = attempt
        if last.get("green"):
            return last
    return last


def fmt_md_table(rows: list[dict], model: str, repeat: int) -> str:
    """按任务聚合出 Markdown 表。"""
    from collections import OrderedDict

    agg = OrderedDict()
    for r in rows:
        agg.setdefault(r["task"], []).append(r)

    lines = [f"# run_all 回归表（{time.strftime('%Y-%m-%d %H:%M')} | model={model} | repeat={repeat}）", ""]
    lines.append("| 任务 | 全绿/次数 | 成功率 | token中位数 | 步数中位数 | LLM调用中位 | 耗时(s)中位 |")
    lines.append("|---|---|---|---|---|---|---|")
    for task, runs in agg.items():
        greens = sum(1 for r in runs if r["green"])
        med = lambda key: statistics.median([r[key] for r in runs])  # noqa: E731
        lines.append(f"| {task} | {greens}/{len(runs)} | "
                     f"{greens/len(runs)*100:.0f}% | {med('tokens'):.0f} | "
                     f"{med('steps'):.0f} | {med('llm_calls'):.0f} | {med('duration_s'):.0f} |")
    lines.append("")
    lines.append("## 明细")
    lines.append("| run_id | 任务 | status | 全绿 | 步数 | token | LLM调用 | 重试 | 失败原因 |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        lines.append(f"| {r['run_id']} | {r['task']} | {r['status']} | {'✅' if r['green'] else '❌'} | "
                     f"{r['steps']} | {r['tokens']} | {r['llm_calls']} | {r.get('retried', 0)} | {r['reason']} |")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=None,
                    help="模型名；不传用 config 默认(flash)。数字①建议 glm-4-air-250414")
    ap.add_argument("--repeat", type=int, default=1, help="每任务跑几次（预算基数建议 ≥2）")
    ap.add_argument("--retries", type=int, default=2,
                    help="单局未全绿时整局重试次数（自愈瞬时故障，默认 2）")
    ap.add_argument("--tasks", default=",".join(ALL_TASKS), help="逗号分隔任务列表")
    args = ap.parse_args()

    if args.model is None:
        from app.config import LLM_MODEL
        model = LLM_MODEL
        print(f"⚠️ 未指定 --model，用 config 默认 {model}（glm-4.5-flash 免费档实测 6/6）")
    else:
        model = args.model
    tasks = [t.strip() for t in args.tasks.split(",") if t.strip()]

    print(f"run_all 开始：{tasks} × {args.repeat} 次 | model={model} | 自愈重试≤{args.retries}")
    print("=" * 70)
    rows = []
    for task in tasks:
        for i in range(args.repeat):
            print(f"> {task} 第{i+1}/{args.repeat} 次…", flush=True)
            r = run_once_with_retry(task, model, max_retry=args.retries)
            rows.append(r)
            print(f"  -> {r['status']} | 全绿={r['green']} | "
                  f"steps={r['steps']} | token={r['tokens']} | "
                  f"重试={r.get('retried', 0)} | 原因={r['reason'][:60]}")

    md = fmt_md_table(rows, model, args.repeat)
    print("\n" + "=" * 70)
    print(md)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    (REPORT_DIR / f"run_all_report_{stamp}.md").write_text(md, encoding="utf-8")
    (REPORT_DIR / f"run_all_{stamp}.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告已存: data/run_all_report_{stamp}.md (+.json)")

    # 总判定
    greens = sum(1 for r in rows if r["green"])
    print(f"\n汇总: 全绿 {greens}/{len(rows)}")
    if len(rows) and greens == len(rows):
        print("🎉 3/3 全绿达成（数字① 最小证据）")
    else:
        print("未全绿——看明细定位失败任务（模型 or 任务包？）")


if __name__ == "__main__":
    main()
