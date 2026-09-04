"""M3 数字②探针（子进程版）：真 LLM 跑任务，可模拟中途被 kill -9。

两种模式：
- fresh（无 --run-id）：新建 run；带 --kill-before D 时，在第 D 次决策前
  os._exit(137) 模拟 kill -9（此前 D-1 步已执行并落 checkpoint）。
- resume（带 --run-id）：从该 run 最后 checkpoint 续跑至 done，打印是否全绿。

父进程 run_resume_test.py 串起"跑一段→杀→resume→绿"的循环，统计恢复率。
用法（backend/ 下）：
    python scripts/drive_resume_probe.py --task t1_single_fix --kill-before 3   # fresh+杀
    python scripts/drive_resume_probe.py --task t1_single_fix --run-id XXXXX    # resume
"""
import argparse
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.runtime.llm import chat  # noqa: E402
from app.runtime.loop import HarnessLoop, LoopError  # noqa: E402
from app.store import db  # noqa: E402

TASKS = Path(__file__).resolve().parent.parent / "tasks"
REPO_ROOT = Path(__file__).resolve().parent.parent.parent


class KillSwitchDecider:
    """包一层 decider：决策次数到 kill_before 前一刻自杀（模拟进程被杀）。"""

    def __init__(self, base, kill_before: int | None):
        self.base = base
        self.kill_before = kill_before
        self.calls = 0

    def __call__(self, messages):
        self.calls += 1
        if self.kill_before and self.calls >= self.kill_before:
            print(f"[kill] 第 {self.calls} 次决策前自杀（模拟 kill -9），此前已完成 "
                  f"{self.calls - 1} 个决策并落 checkpoint", flush=True)
            os._exit(137)  # SIGKILL 语义：无 finally、无状态更新，模拟最狠的中断
        return self.base(messages)


def _restore(task: str):
    try:
        subprocess.run(["git", "-C", str(REPO_ROOT), "restore", "--",
                        f"backend/tasks/{task}"], check=True, capture_output=True)
    except subprocess.CalledProcessError:
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", default="t1_single_fix")
    ap.add_argument("--model", default="glm-4.5-flash")
    ap.add_argument("--run-id", default=None, help="给定时 = resume 模式")
    ap.add_argument("--kill-before", type=int, default=None,
                    help="fresh 模式：第 N 次决策前自杀（N-1 步已完成）")
    args = ap.parse_args()

    task_dir = TASKS / args.task
    goal = (task_dir / "README.md").read_text(encoding="utf-8")
    db.init_db()

    if args.run_id:  # ---------- resume 模式 ----------
        try:
            loop = HarnessLoop(task_dir, goal, decider=chat, run_id=args.run_id)
            result = loop.resume()
        except LoopError as e:
            # 机制性失败（工作区漂移/预算未批/run 不存在）：重试无意义，父进程判 FAIL
            print(f"[MECH-FAIL] resume 机制拒绝: {e}", flush=True)
            _restore(args.task)
            return 2
        except Exception as e:  # noqa: BLE001 - LLM 网络/限流等偶发
            # 偶发失败：checkpoint 仍在、工作区未动（决策前被杀/异常），父进程可自愈重试
            print(f"[LLM-FAIL] resume 途中偶发异常: {type(e).__name__}: {e}", flush=True)
            return 3  # 不 restore：保留现场供重试 resume（restore 会触发 ws_hash 漂移）
        tests = [a for a in result["actions"] if a["tool"] == "run_tests"]
        green = result["status"] == "done" and tests and "全绿" in tests[-1]["result_tail"]
        print(f"[resume] status={result['status']} | steps={result['steps']} | 全绿={green}")
        _restore(args.task)
        return 0 if green else 1

    # ---------- fresh 模式（可带 kill） ----------
    decider = KillSwitchDecider(chat, args.kill_before)
    loop = HarnessLoop(task_dir, goal, decider=decider)
    print(f"RUN_ID={loop.run_id}", flush=True)
    result = loop.run()
    tests = [a for a in result["actions"] if a["tool"] == "run_tests"]
    green = result["status"] == "done" and tests and "全绿" in tests[-1]["result_tail"]
    print(f"[fresh] status={result['status']} | steps={result['steps']} | 全绿={green}")
    _restore(args.task)
    return 0 if green else 1


if __name__ == "__main__":
    raise SystemExit(main())
