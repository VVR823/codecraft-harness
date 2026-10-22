"""CLI for the shared service. No automatic resumption or source reset."""

import argparse
from contextlib import redirect_stdout
import json
import sqlite3
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.runtime.run_service import RunService
from app.runtime.settings import Settings
from app.store.migrations import MigrationRequired


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("task_name", nargs="?", default="t1_single_fix")
    ap.add_argument("--mode", choices=["offline_demo", "local", "remote"])
    ap.add_argument("--data-dir", type=Path)
    ap.add_argument("--model")
    ap.add_argument("--budget", type=int)
    ap.add_argument("--new-budget", type=int)
    ap.add_argument("--approve-budget", action="store_true")
    ap.add_argument("--compress", action="store_true")
    ap.add_argument("--structured-output", action="store_true", help="Explicit local execution JSON Schema")
    for flag in ["plan", "skills", "mcp", "memory", "github"]:
        ap.add_argument("--" + flag, action="store_true")
    ap.add_argument("--workspace", type=Path)
    ap.add_argument("--goal", default="")
    ap.add_argument("--resume")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--env-file", type=Path, help="Only explicitly selected dotenv files are read")
    args = ap.parse_args(argv)
    if args.env_file:
        from dotenv import load_dotenv

        load_dotenv(args.env_file, override=False)
    try:
        settings = Settings.from_env()
        if args.data_dir:
            settings = settings.model_copy(update={"data_dir": args.data_dir.resolve()})
        service = RunService(settings)
        with redirect_stdout(sys.stderr):
            if args.resume:
                if (
                    args.mode
                    or args.model
                    or args.budget
                    or args.workspace
                    or args.goal
                    or args.compress
                    or args.structured_output
                    or any(getattr(args, f) for f in ["plan", "skills", "mcp", "memory", "github"])
                ):
                    raise ValueError("恢复复用已存配置；仅可通过--new-budget扩额")
                new_budget = args.new_budget
                if args.approve_budget and new_budget is None:
                    new_budget = service.get(args.resume)["options"]["token_budget"] + 30000
                result = service.resume(args.resume, new_budget=new_budget)
            else:
                overrides = {
                    k: v
                    for k, v in {"mode": args.mode, "model": args.model, "token_budget": args.budget}.items()
                    if v is not None
                }
                options = settings.options(
                    **overrides,
                    context_compress=args.compress,
                    structured_output=args.structured_output,
                    use_plan=args.plan,
                    use_skills=args.skills,
                    use_mcp=args.mcp,
                    use_memory=args.memory,
                    use_github=args.github,
                )
                created = service.create(args.task_name, args.goal, options, source=args.workspace)
                result = service.execute(created["run_id"])
        print(
            json.dumps(result, ensure_ascii=False)
            if args.json
            else f"run_id={result['run_id']} mode={result['mode']} status={result['status']} verified={result['verified']} tokens={result['known_tokens']} unknown_calls={result['unknown_calls']}\nworkspace={result['workspace_path']}"
        )
        return (
            0
            if result["status"] == "done" and result["verified"]
            else 2
            if result["status"] in ("paused", "budget_paused", "interrupted")
            else 1
        )
    except (MigrationRequired, sqlite3.Error):
        print(
            json.dumps(
                {
                    "error_code": "storage_unavailable",
                    "error_message": "存储不可用，请检查数据目录；旧库需显式备份迁移，损坏库请保留核查，也可使用新的数据目录",
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1
    except (ValueError, KeyError, OSError) as exc:
        print(
            json.dumps({"error_code": "cli_rejected", "error_message": str(exc)}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
