"""M3 数字③：上下文压缩率实测（开关对比）。

同一任务真 LLM 跑两次：关压缩（全量视图）vs 开压缩（近 3 步全文+旧步一行摘要），
用 chat_with_usage 记录每次决策的真实 prompt_tokens，对比：
    压缩率 = 1 - median(开)/median(关)
并记录两档是否都全绿——数字③ 的意义是"压了还绿"，不只是"省了多少"。

用法（backend/ 下）：python scripts/measure_compress.py [--task t2_missing_fn]
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


def run_one(task: str, model: str, compress: bool) -> dict:
    """真 LLM 跑一次，返回 {green, steps, calls, prompt_tokens:[...], total_tokens}。"""
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
            "median_prompt": statistics.median(prompt_tokens) if prompt_tokens else 0,
            "max_prompt": max(prompt_tokens) if prompt_tokens else 0,
            "duration_s": dt, "run_id": loop.run_id}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", default="t2_missing_fn", help="默认 T2：读多文件+长内容，压缩空间最大")
    ap.add_argument("--model", default="glm-4.5-flash")
    args = ap.parse_args()

    print(f"measure_compress: task={args.task} | model={args.model} | "
          f"压缩档=近3步全文+旧步摘要 | 对照档=全量")
    print("=" * 70)
    base = run_one(args.task, args.model, compress=False)
    print(f"[对照-关] run={base['run_id'][:8]} | green={base['green']} | steps={base['steps']} "
          f"| calls={base['calls']} | prompt中位={base['median_prompt']} | 最大={base['max_prompt']}")
    comp = run_one(args.task, args.model, compress=True)
    print(f"[压缩-开] run={comp['run_id'][:8]} | green={comp['green']} | steps={comp['steps']} "
          f"| calls={comp['calls']} | prompt中位={comp['median_prompt']} | 最大={comp['max_prompt']}")

    rate = 1 - comp["median_prompt"] / base["median_prompt"] if base["median_prompt"] else 0
    print("\n" + "=" * 70)
    print(f"压缩率 = 1 - 开/关 单步prompt中位 = {rate*100:.1f}%")
    print(f"  关: {base['median_prompt']} token/决策 → 开: {comp['median_prompt']} token/决策")
    print(f"全绿: 关={base['green']} | 开={comp['green']}  →  "
          f"{'✅ 压缩且绿（数字③成立）' if (base['green'] and comp['green']) else '⚠️ 有档未绿，需调保留步数'}")

    lines = [
        f"# 压缩率实测（{time.strftime('%Y-%m-%d %H:%M')} | task={args.task} | model={args.model}）",
        "",
        "| 档 | run_id | 全绿 | 步数 | LLM调用 | 单步prompt中位 | 单步prompt最大 |",
        "|---|---|---|---|---|---|---|",
        f"| 关压缩(全量) | {base['run_id']} | {'✅' if base['green'] else '❌'} | "
        f"{base['steps']} | {base['calls']} | {base['median_prompt']} | {base['max_prompt']} |",
        f"| 开压缩(近3步) | {comp['run_id']} | {'✅' if comp['green'] else '❌'} | "
        f"{comp['steps']} | {comp['calls']} | {comp['median_prompt']} | {comp['max_prompt']} |",
        "",
        f"**压缩率 = {rate*100:.1f}%**（1 - 开/关 单步 prompt token 中位比）",
    ]
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    (REPORT_DIR / f"measure_compress_{stamp}.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"\n报告已存: data/measure_compress_{stamp}.md")
    return 0 if (base["green"] and comp["green"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
