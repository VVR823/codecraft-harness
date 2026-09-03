"""trace 回放查看器：按步打印一次 run 的执行轨迹（行车记录仪回放）。

用法（backend/ 下）：python scripts/replay_run.py <run_id>
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.store import db  # noqa: E402


def main():
    if len(sys.argv) < 2:
        print("用法: python scripts/replay_run.py <run_id>")
        sys.exit(1)
    run_id = sys.argv[1]
    run = db.get_run(run_id)
    if run is None:
        print("run 不存在:", run_id)
        sys.exit(1)
    print("=== run", run_id, "| task:", run["task_id"], "| status:", run["status"], "===")
    for t in db.list_traces(run_id):
        print("step %-3d %-10s | verdict=%-8s | args: %s" % (
            t["step"], t["tool"] or "(decide)", t["verdict"] or "-",
            t["args_summary"][:80]))
        out = t["output_summary"]
        if out:
            print("        output:", out[:200].replace("\n", " "), "...")
    usage = db.get_usage(run_id)
    cp = db.last_checkpoint(run_id)
    if cp:
        print("checkpoint: step", cp["step"], "| ws_hash:", cp["ws_hash"],
              "| tokens:", cp["tokens_used"])
    if usage:
        print("usage: total_tokens =", usage["total_tokens"])


if __name__ == "__main__":
    main()
