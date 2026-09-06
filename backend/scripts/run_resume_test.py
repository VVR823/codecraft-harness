"""M3 数字②：杀 N 次测恢复率（resume 成功率）。

对同一任务（默认 T1），在 N 个不同决策点把子进程 kill -9（os._exit(137)），
每次都用独立子进程 resume 续跑，统计"恢复至全绿"的成功率 + "不重放"证据。

resume 进程退出码语义（drive_resume_probe.py）：
    0 = 全绿 | 1 = 正常返回但未绿 | 2 = 机制性失败（漂移/预算，重试无意义）
    3 = LLM 偶发异常（网络/限流，checkpoint 还在，可自愈重试）
对退出码 3 做 --retries 次自愈重试（与 run_all 吸收免费模型抖动同口径）；
退出码 2 是机制真缺陷，立即判 FAIL——数字② 度量的是"resume 机制可靠性"。

用法（backend/ 下）：
    python scripts/run_resume_test.py [--task t1_single_fix] [--kill-points 3,4] [--retries 2]
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PY = sys.executable
SCRIPTS = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS.parent.parent
PROBE = SCRIPTS / "drive_resume_probe.py"
REPORT_DIR = SCRIPTS.parent / "data"

# probe resume 退出码常量（与 drive_resume_probe.py 保持同步）
RC_GREEN = 0
RC_NOT_GREEN = 1
RC_MECH_FAIL = 2
RC_LLM_FAIL = 3


def _run_child(args: list[str], timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run([PY, str(PROBE)] + args, capture_output=True,
                          text=True, encoding="utf-8", timeout=timeout,
                          cwd=SCRIPTS.parent)


def _diagnose(tag: str, p: subprocess.CompletedProcess) -> None:
    """失败时打印 stdout 尾部 + stderr 尾部——traceback 走 stderr，不打印等于不可查。"""
    print(f"  ⚠️ {tag} 失败诊断（退出码={p.returncode}）:")
    lines = [l for l in p.stderr.splitlines() if l.strip()][-12:]
    if lines:
        for l in lines:
            print("   stderr |", l)
    for l in [l for l in p.stdout.splitlines() if l.strip()][-12:]:
        print("   stdout |", l)


def _log_result(jsonl: Path, rec: dict) -> None:
    """每试次即时落盘一行（O2：崩了不丢已跑试次）。"""
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    with jsonl.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", default="t1_single_fix")
    ap.add_argument("--model", default="glm-4.5-flash")
    ap.add_argument("--kill-points", default="3,4",
                    help="逗号分隔的杀点（第 N 次决策前杀；N-1 步已完成）")
    ap.add_argument("--retries", type=int, default=2,
                    help="LLM 偶发失败（码3）的自愈重试次数，默认 2（与 run_all 同口径）")
    args = ap.parse_args()
    kill_points = [int(x) for x in args.kill_points.split(",") if x.strip()]

    stamp = time.strftime("%Y%m%d_%H%M%S")
    jsonl = REPORT_DIR / f"run_resume_test_{stamp}.jsonl"

    print(f"run_resume_test 开始：task={args.task} | 杀点={kill_points} | "
          f"model={args.model} | LLM自愈重试={args.retries}")
    print("=" * 70)
    passed = 0
    for kp in kill_points:
        # 试前清场（幂等）：任务包回 bug 态（含删 AI 新建的 untracked 文件，
        # 防上一试次 resume 异常残留污染本试次——与 run_all 的 _restore_task 同标准）
        subprocess.run(["git", "-C", str(REPO_ROOT), "restore", "--",
                        f"backend/tasks/{args.task}"], check=True)
        subprocess.run(["git", "-C", str(REPO_ROOT), "clean", "-fd", "--",
                        f"backend/tasks/{args.task}"], check=True)
        print(f"\n>>> 试次：杀点 = 第 {kp} 次决策前（应已完成 {kp-1} 步）…")

        # 1) fresh 跑一段 → 中途自杀（LLM 偶发码 3 自愈重试，与阶段2同口径）
        p1 = None
        for attempt in range(args.retries + 1):
            p1 = _run_child(["--task", args.task, "--model", args.model,
                             "--kill-before", str(kp)])
            if p1.returncode == RC_LLM_FAIL and attempt < args.retries:
                print(f"  ⚠️ fresh 阶段 LLM 偶发异常，清场后自愈重试 {attempt + 1}/{args.retries}…")
                subprocess.run(["git", "-C", str(REPO_ROOT), "restore", "--",
                                f"backend/tasks/{args.task}"], check=True)
                subprocess.run(["git", "-C", str(REPO_ROOT), "clean", "-fd", "--",
                                f"backend/tasks/{args.task}"], check=True)
                continue
            break
        run_id = ""
        for line in (p1.stdout if p1 else "").splitlines():
            if line.startswith("RUN_ID="):
                run_id = line.split("=", 1)[1].strip()
        killed = p1 is not None and p1.returncode == 137
        print(f"  阶段1 退出码={p1.returncode if p1 else '?'} | 被 kill: {killed} | run_id={run_id[:8] if run_id else '?'}")
        if not (killed and run_id):
            print("  ❌ 阶段1未按预期被杀，跳过该试次")
            _diagnose("阶段1", p1) if p1 is not None else None
            _log_result(jsonl, {"kp": kp, "ok": False, "reason": "阶段1未按预期被杀",
                                "rc1": p1.returncode if p1 else None, "run_id": run_id[:8]})
            continue

        # 2) resume 续跑 → 全绿（LLM 偶发码 3 自愈重试，机制码 2 直接 FAIL）
        p2 = None
        for attempt in range(args.retries + 1):
            p2 = _run_child(["--task", args.task, "--model", args.model,
                             "--run-id", run_id])
            rc = p2.returncode
            if rc == RC_LLM_FAIL and attempt < args.retries:
                print(f"  ⚠️ resume LLM 偶发异常，自愈重试 {attempt + 1}/{args.retries}…")
                continue
            break
        no_replay = "[resume] 从 checkpoint" in (p2.stdout if p2 else "")
        tail = [l for l in (p2.stdout if p2 else "").splitlines()
                if "status=" in l or "全绿" in l or "FAIL" in l]
        print(f"  阶段2 退出码={p2.returncode if p2 else '?'}")
        for l in tail:
            print("   ", l)
        print(f"  resume 日志(不重放证据): {'✅' if no_replay else '❌'}")
        if p2 is None or p2.returncode not in (RC_GREEN, RC_NOT_GREEN):
            _diagnose("阶段2", p2) if p2 is not None else None

        ok = p2 is not None and p2.returncode == RC_GREEN and no_replay
        passed += 1 if ok else 0
        reason = ("机制失败(码2)" if p2 and p2.returncode == RC_MECH_FAIL else
                  "LLM自愈重试后仍失败" if p2 and p2.returncode == RC_LLM_FAIL else
                  "resume 正常但未全绿" if p2 and p2.returncode == RC_NOT_GREEN else "")
        print(f"  该试次恢复至绿: {'✅ PASS' if ok else f'❌ FAIL（{reason}）'}")
        _log_result(jsonl, {"kp": kp, "ok": ok, "reason": reason or "PASS",
                            "rc1": p1.returncode, "killed": killed,
                            "rc2": p2.returncode if p2 else None,
                            "no_replay": no_replay, "run_id": run_id[:8]})

    print("\n" + "=" * 70)
    print(f"汇总: 恢复率 {passed}/{len(kill_points)}")
    print(f"逐试次明细已存: {jsonl}")
    if len(kill_points) and passed == len(kill_points):
        print(f"🎉 resume 恢复率 100%（杀 {len(kill_points)} 次全部续跑至全绿，且不重放）")
    return 0 if passed == len(kill_points) else 1


if __name__ == "__main__":
    raise SystemExit(main())
