"""Explicit crash probe on private workspaces; no task restore and no auto-resume."""

import argparse
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.runtime.demo import DemoClient
from app.runtime.run_service import RunService
from app.runtime.settings import Settings


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--task", default="t1_single_fix")
    ap.add_argument("--mode", choices=["offline_demo", "local"], default="offline_demo")
    ap.add_argument("--data-dir", type=Path, required=True)
    ap.add_argument("--run-id")
    ap.add_argument("--kill-before", type=int)
    args = ap.parse_args()
    settings = Settings.from_env().model_copy(
        update={"data_dir": args.data_dir.resolve(), "default_mode": args.mode}
    )
    service = RunService(settings)
    run_id = args.run_id or service.create(args.task, "", settings.options())["run_id"]
    print("RUN_ID=" + run_id, flush=True)
    # Kill injection happens after request intent: unknown cost is deliberately preserved.
    original = DemoClient.chat
    calls = 0

    def probe(self, messages):
        nonlocal calls
        calls += 1
        if args.kill_before and calls >= args.kill_before:
            os._exit(137)
        return original(self, messages)

    if args.kill_before:
        if args.mode != "offline_demo":
            ap.error("crash probe only supports offline_demo")
        DemoClient.chat = probe
    result = service.resume(run_id) if args.run_id else service.execute(run_id)
    print(result["status"], "verified=" + str(result["verified"]))
    return 0 if result["status"] == "done" and result["verified"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
