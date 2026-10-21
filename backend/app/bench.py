"""Explicit offline/local evaluation; every attempt remains in the denominator."""

import argparse
from contextlib import redirect_stdout
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

from .config import BASE_DIR
from .runtime.run_service import RunService
from .runtime.settings import Settings


def code_identity():
    try:
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=BASE_DIR, capture_output=True, text=True, check=True, timeout=5
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        revision = "source-distribution"
    digest = hashlib.sha256()
    for path in sorted((BASE_DIR / "app").rglob("*.py")):
        digest.update(path.relative_to(BASE_DIR).as_posix().encode())
        digest.update(path.read_bytes())
    return revision, digest.hexdigest()


def summarize(attempts, *, evidence_kind, options):
    if evidence_kind not in ("offline_demo", "local"):
        raise ValueError("本轮评测仅支持offline_demo或显式local")
    revision, digest = code_identity()
    samples: dict[tuple[str, int], list[dict]] = {}
    for item in attempts:
        samples.setdefault((item["task_id"], item["sample"]), []).append(item)

    def success(item):
        return item["status"] == "done" and item["verified"]

    return {
        "schema_version": 1,
        "git_revision": revision,
        "source_sha256": digest,
        "evidence_kind": evidence_kind,
        "options": options,
        "attempts": attempts,
        "samples": len(samples),
        "first_pass_successes": sum(success(group[0]) for group in samples.values()),
        "verified_successes": sum(any(success(item) for item in group) for group in samples.values()),
        "within_budget_successes": sum(
            any(
                success(item)
                and item["known_tokens"] <= options.get("token_budget", 30000)
                and not item["unknown_calls"]
                for item in group
            )
            for group in samples.values()
        ),
        "known_tokens": sum(item["known_tokens"] for item in attempts),
        "unknown_calls": sum(item["unknown_calls"] for item in attempts),
        "duration_s": round(sum(item["duration_s"] for item in attempts), 3),
        "tool_calls": sum(item["tool_calls"] for item in attempts),
    }


def write_report(report, output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# CodeCraft评测记录",
        "",
        f"证据类型：{report['evidence_kind']}；离线固定脚本不能代表真实模型效果。",
        f"代码：{report['git_revision']}；源码摘要：{report['source_sha256']}",
        "",
        f"首次成功 {report['first_pass_successes']}/{report['samples']}；重试后成功 {report['verified_successes']}/{report['samples']}；总尝试 {len(report['attempts'])}",
        f"已知token {report['known_tokens']}；未知调用 {report['unknown_calls']}；总耗时 {report['duration_s']}s；工具调用 {report['tool_calls']}",
        "",
        "|任务|样本|尝试|状态|verified|token|未知调用|耗时s|",
        "|---|---|---|---|---|---|---|---|",
    ]
    lines += [
        f"|{a['task_id']}|{a['sample']}|{a['attempt']}|{a['status']}|{a['verified']}|{a['known_tokens']}|{a['unknown_calls']}|{a['duration_s']}|"
        for a in report["attempts"]
    ]
    (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_benchmark(settings, tasks, output, *, repeat=1, retries=0, options=None, service=None):
    options = options or settings.options()
    if options.mode not in ("offline_demo", "local") or not 1 <= repeat <= 20 or not 0 <= retries <= 2:
        raise ValueError("评测参数无效；repeat1..20、显式retries0..2")
    settings.validate_mode(options)
    service = service or RunService(settings)
    attempts = []
    for task in tasks:
        for sample in range(repeat):
            for attempt in range(retries + 1):
                started = time.monotonic()
                created = service.create(task, "", options)
                result = service.execute(created["run_id"])
                with service.database():
                    from .store import db

                    tools = len(db.committed_actions(created["run_id"]))
                attempts.append(
                    {
                        k: result[k]
                        for k in (
                            "run_id",
                            "task_id",
                            "status",
                            "verified",
                            "known_tokens",
                            "unknown_calls",
                            "error_code",
                        )
                    }
                )
                attempts[-1].update(
                    sample=sample,
                    attempt=attempt,
                    duration_s=round(time.monotonic() - started, 3),
                    tool_calls=tools,
                )
                report = summarize(attempts, evidence_kind=options.mode, options=options.model_dump())
                write_report(report, output)
                if result["status"] == "done" and result["verified"]:
                    break
    return summarize(attempts, evidence_kind=options.mode, options=options.model_dump())


def main(argv=None, *, default_variant=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mode", choices=["offline_demo", "local"], default="offline_demo")
    ap.add_argument("--tasks", default="t1_single_fix,t2_missing_fn,t3_cross_file")
    ap.add_argument("--task", help="Compatibility single-task selector")
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--data-dir", type=Path, required=True)
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--retries", type=int, default=0)
    ap.add_argument("--model")
    ap.add_argument("--budget", type=int, default=30000)
    ap.add_argument("--compress", action="store_true")
    ap.add_argument("--structured-output", action="store_true")
    ap.add_argument("--plan", action="store_true")
    args = ap.parse_args(argv)
    settings = Settings.from_env().model_copy(
        update={"data_dir": args.data_dir.resolve(), "default_mode": args.mode}
    )
    options = settings.options(
        token_budget=args.budget,
        context_compress=args.compress or default_variant == "compress",
        structured_output=args.structured_output,
        use_plan=args.plan or default_variant == "plan",
        **({"model": args.model} if args.model else {}),
    )
    if args.mode == "local" and not settings.base_url:
        print(json.dumps({"skipped": True, "reason": "local端点未配置；未运行真实模型评测"}))
        return 2
    try:
        with redirect_stdout(sys.stderr):
            report = run_benchmark(
                settings,
                [args.task] if args.task else args.tasks.split(","),
                args.output,
                repeat=args.repeat,
                retries=args.retries,
                options=options,
            )
        print(json.dumps(report, ensure_ascii=False))
        return 0 if report["verified_successes"] == report["samples"] else 1
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
