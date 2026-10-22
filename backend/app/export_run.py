"""Portable allowlisted result and redacted relative patch; never raw prompts/DB."""

import argparse
import difflib
import json
from pathlib import Path

from .logging_config import redact
from .runtime.run_service import RunService
from .runtime.settings import Settings
from .runtime.workspace import EXCLUDED, linked, protected, load_snapshot, verify_baseline, workspace_hash
from .runtime.execution_lock import execution_lock
from .store import db


def _file_patch(rel, old, new):
    old_exists, new_exists = old.is_file(), new.is_file()
    before = old.read_bytes().decode("utf-8") if old_exists else ""
    after = new.read_bytes().decode("utf-8") if new_exists else ""
    if old_exists == new_exists and before == after:
        return []
    old_name = json.dumps("a/" + rel.as_posix(), ensure_ascii=False)
    new_name = json.dumps("b/" + rel.as_posix(), ensure_ascii=False)
    patch = [f"diff --git {old_name} {new_name}\n"]
    if not old_exists:
        patch.append("new file mode 100644\n")
    elif not new_exists:
        patch.append("deleted file mode 100644\n")
    for line in difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=old_name if old_exists else "/dev/null",
        tofile=new_name if new_exists else "/dev/null",
    ):
        patch.append(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n")
    return patch


def result_bundle(service, run_id):
    with execution_lock(service.settings.data_dir, run_id), service.database():
        run = service.get(run_id)
        root = Path(run["workspace_path"]) if run["workspace_path"] else None
        before = None
        if root is not None:
            try:
                snapshot = load_snapshot(root)
                verify_baseline(snapshot)
                before = workspace_hash(root)
                cp = db.last_checkpoint(run_id)
                if cp and cp["ws_hash"] != before:
                    raise ValueError("工作区漂移")
            except (OSError, ValueError):
                raise ValueError("工作区或测试基线发生漂移，当前补丁未验证，拒绝导出") from None
        result = _result_bundle_locked(service, run_id)
        if root is not None:
            verify_baseline(snapshot)
            if workspace_hash(root) != before:
                raise ValueError("导出期间工作区漂移，当前补丁未验证")
        return result


def _result_bundle_locked(service, run_id):
    run = service.get(run_id)
    if run["active"] or run["status"] in ("pending", "running"):
        raise ValueError("请等待运行暂停或结束后导出")
    root = Path(run["workspace_path"]) if run["workspace_path"] else None
    original = root.parent / "original" if root else None
    patches: list[str] = []
    if root is not None and original is not None and original.is_dir():
        paths: set[Path] = set()
        for directory in (root, original):
            paths.update(p.relative_to(directory) for p in directory.rglob("*") if p.is_file())
        for rel in sorted(paths):
            if protected(rel) or any(p.casefold() in EXCLUDED for p in rel.parts):
                continue
            old, new = original / rel, root / rel
            if any(linked(p) for p in (old, new)) or rel.suffix.lower() not in {
                ".py",
                ".md",
                ".txt",
                ".json",
                ".toml",
                ".js",
                ".ts",
                ".yml",
                ".yaml",
            }:
                continue
            if max((p.stat().st_size if p.is_file() else 0 for p in (old, new))) > 262144:
                continue
            try:
                patches.extend(_file_patch(rel, old, new))
            except UnicodeError:
                continue
    patch = redact("".join(patches))
    if len(patch.encode("utf-8")) > 2 * 1024 * 1024:
        raise ValueError("补丁超过2MiB，请在私有工作区自行取回")
    traces = service.traces(run_id, limit=200)["traces"]
    bundle = {
        "schema_version": 1,
        **{
            k: run[k]
            for k in (
                "run_id",
                "task_id",
                "status",
                "verified",
                "evidence_kind",
                "known_tokens",
                "unknown_calls",
                "error_code",
            )
        },
        "options": run["options"],
        "patch": patch,
        "patch_available": bool(original and original.is_dir()),
        "trace_summary": [{k: t[k] for k in ("step", "tool", "verdict", "tokens")} for t in traces],
        "note": "仅允许列表摘要与脱敏补丁；不含原始prompt/数据库。敏感内容仍需操作者公开前人工检查。",
    }

    def sanitize(value):
        if isinstance(value, str):
            return redact(value)
        if isinstance(value, list):
            return [sanitize(item) for item in value]
        if isinstance(value, dict):
            return {key: sanitize(item) for key, item in value.items()}
        return value

    return sanitize(bundle)


def export_run(service, run_id, output):
    run = service.get(run_id)
    output = Path(output).resolve()
    if run["workspace_path"] and output.is_relative_to(Path(run["workspace_path"]).resolve()):
        raise ValueError("导出目录不能位于运行工作区内")
    bundle = result_bundle(service, run_id)
    output.mkdir(parents=True, exist_ok=True)
    (output / "result.json").write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "changes.patch").write_bytes(bundle["patch"].encode("utf-8"))
    return bundle


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("run_id")
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--data-dir", type=Path, required=True)
    args = ap.parse_args()
    service = RunService(Settings(data_dir=args.data_dir.resolve()))
    export_run(service, args.run_id, args.output)
    print("导出完成：result.json、changes.patch")


if __name__ == "__main__":
    main()
