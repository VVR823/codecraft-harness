"""M3 数字③：上下文压缩率实测（开关对比，O1~O3 加固版 2026-09-05）。

同一任务真 LLM 跑两档：关压缩（全量视图）vs 开压缩（近 3 步全文+旧步一行摘要），
用 chat_with_usage 记录每次决策的真实 prompt_tokens，对比：
    压缩率 = 1 - median(开)/median(关)
并记录两档是否都全绿——数字③ 的意义是"压了还绿"，不只是"省了多少"。

加固（B6a 补测暴露的问题）：
- 自愈重试：429 账户级限流（code 1302）/空内容是服务端偶发，整局冷却后重跑 ≤2，
  正常返回的 failed 不重试（真实信号）
- status 字段：区分 done/failed/llm_failed
- --repeat N：多样本，prompt_tokens 合并取中位（防单次样本运气）
- 即时落盘：每局跑完立即写盘，崩了不丢已跑局

用法（backend/ 下）：
    python scripts/measure_compress.py [--task t2_missing_fn] [--repeat 2]
"""
import argparse
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
REPORT_DIR = Path(__file__).resolve().parent.parent / "data"
REPO_ROOT = TASKS.parent.parent


def _restore(task: str):
    """A/B 公平前提：每档起跑前把任务包 git 还原回 bug 态，杜绝上一档的修复污染下一档。"""
    try:
        subprocess.run(["git", "-C", str(REPO_ROOT), "restore", "--",
                        f"backend/tasks/{task}"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(REPO_ROOT), "clean", "-fd",
                        "--", f"backend/tasks/{task}"], check=True, capture_output=True)
    except subprocess.CalledProcessError:
        pass


def _run_once(task: str, model: str, compress: bool) -> dict:
    """单次真 LLM 跑（内部被 run_one 重试调用）。"""
    _restore(task)  # 起跑前必还原：两档起点一致（同 bug 态）
    task_dir = TASKS / task
    goal = (task_dir / "README.md").read_text(encoding="utf-8")
    prompt_tokens: list[int] = []

    def decider(messages):
        text, usage = chat_with_usage(messages, model=model)
        prompt_tokens.append(usage.get("prompt_tokens", 0))
        return text

    db.init_db()
    loop = HarnessLoop(task_dir, goal, decider=decider,
                       context_compress=compress, keep_recent_steps=3)
    t0 = time.monotonic()
    try:
        result = loop.run()
    finally:
        _restore(task)  # 跑完必还原任务包回 bug 态——同 run_all 标准，防残留污染后续测量/回归
    dt = round(time.monotonic() - t0, 1)
    tests = [a for a in result["actions"] if a["tool"] == "run_tests"]
    green = result["status"] == "done" and tests and "全绿" in tests[-1]["result_tail"]
    return {"compress": compress, "green": green, "steps": result["steps"],
            "calls": len(prompt_tokens), "prompt_tokens": prompt_tokens,
            "status": result["status"],  # done|failed|budget_paused——护栏止损可区分
            "duration_s": dt, "run_id": loop.run_id}


def run_one(task: str, model: str, compress: bool, max_retry: int = 2) -> dict:
    """真 LLM 跑一次（带自愈重试，与 run_all 同口径）。

    异常抛出的 RuntimeError = 基础设施故障（429/1302/空内容，免费模型服务端高压常见），
    整局冷却后重跑；正常返回的 failed/budget_paused = 真实结果，不重试。
    """
    for attempt in range(max_retry + 1):
        if attempt:
            print(f"    ↻ 第{attempt}次整局重试（冷却 90s）…", flush=True)
            time.sleep(90)
        try:
            return _run_once(task, model, compress)
        except RuntimeError as e:
            msg = str(e)
            if "429" in msg or "速率限制" in msg or "1302" in msg:
                print(f"    ⚠️ LLM 限流（{msg[:70]}…）", flush=True)
                continue
            if "空内容" in msg or "LLM 返回空" in msg:
                print(f"    ⚠️ LLM 返回空内容（服务端高压偶发，{msg[:50]}…）", flush=True)
                continue
            raise
    return {"compress": compress, "green": False, "steps": 0, "calls": 0,
            "prompt_tokens": [], "status": "llm_failed", "duration_s": 0,
            "run_id": "", "error": "LLM 故障重试耗尽"}


# ---------- 多样本统计与即时落盘 ----------

def _render(rows: list[dict], task: str, model: str, repeat: int) -> str:
    """把当前全部样本渲染成 md（明细表 + 合并中位汇总）。"""
    lines = [f"# 压缩率实测（{time.strftime('%Y-%m-%d %H:%M')} | task={task} "
             f"| model={model} | repeat={repeat}）", "",
             "| 档 | run_id | 全绿 | 步数 | LLM调用 | 状态 | 单步prompt中位 | 耗时(s) |",
             "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        med = statistics.median(r["prompt_tokens"]) if r["prompt_tokens"] else 0
        lines.append(
            f"| {'开压缩' if r['compress'] else '关压缩'} | {r['run_id'][:8]} | "
            f"{'✅' if r['green'] else '❌'} | {r['steps']} | {r['calls']} | "
            f"{r.get('status', r.get('error', ''))} | {med} | {r['duration_s']} |")
    # 合并中位：把同档所有局的所有单步 prompt_tokens 汇成一个池取中位（样本更多更稳）
    off_toks = [t for r in rows if not r["compress"] for t in r["prompt_tokens"]]
    on_toks = [t for r in rows if r["compress"] for t in r["prompt_tokens"]]
    off_med = statistics.median(off_toks) if off_toks else 0
    on_med = statistics.median(on_toks) if on_toks else 0
    rate = 1 - on_med / off_med if off_med else 0
    off_green = all(r["green"] for r in rows if not r["compress"]) if any(
        not r["compress"] for r in rows) else False
    on_green = all(r["green"] for r in rows if r["compress"]) if any(
        r["compress"] for r in rows) else False
    lines.append("")
    lines.append("## 汇总（合并全部样本）")
    lines.append(f"- 关压缩: 单步 prompt 中位 **{off_med}** token/决策（{len(off_toks)} 个决策样本）")
    lines.append(f"- 开压缩: 单步 prompt 中位 **{on_med}** token/决策（{len(on_toks)} 个决策样本）")
    lines.append(f"- **压缩率 = {rate * 100:.1f}%**")
    lines.append(f"- 全绿: 关={'✅' if off_green else '❌'} | 开={'✅' if on_green else '❌'}  →  "
                 f"{'✅ 压缩且绿（数字③成立）' if (off_green and on_green) else '⚠️ 有档未绿，看明细'}")
    return "\n".join(lines)


def _save(rows: list[dict], task: str, model: str, repeat: int, stamp: str) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / f"measure_compress_{stamp}.md"
    path.write_text(_render(rows, task, model, repeat), encoding="utf-8")
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", default="t2_missing_fn", help="默认 T2：读多文件+长内容，压缩空间最大")
    ap.add_argument("--model", default="glm-4.5-flash")
    ap.add_argument("--repeat", type=int, default=1,
                    help="每档重复次数（prompt_tokens 合并取中位，防单次样本运气；默认 1）")
    args = ap.parse_args()

    print(f"measure_compress: task={args.task} | model={args.model} | repeat={args.repeat} | "
          f"压缩档=近3步全文+旧步摘要 | 对照档=全量")
    print("=" * 70)
    rows: list[dict] = []
    stamp = time.strftime("%Y%m%d_%H%M%S")
    report = None
    for rep in range(args.repeat):
        label = f"（第{rep + 1}/{args.repeat}次）" if args.repeat > 1 else ""
        print(f"[{label} 对照-关压缩]", flush=True)
        rows.append(run_one(args.task, args.model, compress=False))
        r = rows[-1]
        med = statistics.median(r["prompt_tokens"]) if r["prompt_tokens"] else 0
        print(f"  run={r['run_id'][:8]} | green={r['green']} | steps={r['steps']} "
              f"| calls={r['calls']} | prompt中位={med} | status={r.get('status', '')}", flush=True)
        report = _save(rows, args.task, args.model, args.repeat, stamp)  # 即时落盘

        print(f"[{label} 压缩-开]", flush=True)
        rows.append(run_one(args.task, args.model, compress=True))
        r = rows[-1]
        med = statistics.median(r["prompt_tokens"]) if r["prompt_tokens"] else 0
        print(f"  run={r['run_id'][:8]} | green={r['green']} | steps={r['steps']} "
              f"| calls={r['calls']} | prompt中位={med} | status={r.get('status', '')}", flush=True)
        report = _save(rows, args.task, args.model, args.repeat, stamp)  # 即时落盘

    print("\n" + "=" * 70)
    md = _render(rows, args.task, args.model, args.repeat)
    print(md)
    if report is None:
        report = _save(rows, args.task, args.model, args.repeat, stamp)
    print(f"\n报告已存: {report}")
    off_ok = all(r["green"] for r in rows if not r["compress"]) and any(
        not r["compress"] for r in rows)
    on_ok = all(r["green"] for r in rows if r["compress"]) and any(
        r["compress"] for r in rows)
    return 0 if (off_ok and on_ok) else 1


if __name__ == "__main__":
    raise SystemExit(main())
