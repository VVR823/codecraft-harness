"""Private run snapshots and a frozen, source-bound pytest baseline."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

EXCLUDED = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "node_modules",
    "data",
}
CONFIGS = {"conftest.py", "pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini"}


def protected(path: Path) -> bool:
    name = path.name.casefold()
    return (
        any(p.casefold() in {"test", "tests", ".git"} for p in path.parts)
        or name in CONFIGS
        or name.startswith(("test_", ".env"))
        or name.endswith("_test.py")
        or path.suffix.casefold() in {".pem", ".key"}
    )


def linked(path: Path) -> bool:
    return path.is_symlink() or path.is_junction()


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def workspace_hash(root: Path) -> str:
    h = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if any(p.casefold() in EXCLUDED for p in rel.parts):
            continue
        if linked(path):
            raise ValueError("工作区含链接")
        if path.is_file():
            h.update(rel.as_posix().encode())
            h.update(b"\0")
            h.update(bytes.fromhex(digest(path)))
    return h.hexdigest()


@dataclass(frozen=True)
class WorkspaceSnapshot:
    run_id: str
    root: Path
    baseline_dir: Path
    manifest: dict


def resolve_pack(task_id: str, packs_root: Path) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", task_id):
        raise ValueError("非法任务包名称")
    path = packs_root / task_id
    if linked(path) or not path.is_dir() or not (path / "README.md").is_file():
        raise ValueError("任务包不存在或为链接")
    if path.resolve().parent != packs_root.resolve():
        raise ValueError("任务包越界")
    return path


def prepare_workspace(source: Path, run_id: str, data_dir: Path, *, github=False) -> WorkspaceSnapshot:
    source = source.absolute()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", run_id) or linked(source) or not source.is_dir():
        raise ValueError("非法来源或运行ID（不允许链接）")
    if github and (not (source / ".git").is_dir() or linked(source / ".git")):
        raise ValueError("GitHub来源须为含独立.git目录的仓库，不接受worktree/gitfile")
    if github:
        import subprocess

        def git(*args, cwd=source):
            return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)

        if git("status", "--porcelain").stdout.strip():
            raise ValueError("GitHub来源须为干净独立仓库")
        origin = git("remote", "get-url", "origin").stdout.strip()
        from urllib.parse import urlsplit

        origin_url = urlsplit(origin)
        if origin_url.password is not None or (
            origin_url.scheme in {"http", "https"} and origin_url.username is not None
        ):
            raise ValueError("origin不得嵌入凭证；请使用独立的Git/gh凭证配置")
    run_root = data_dir.resolve() / "runs" / run_id
    if run_root.is_relative_to(source.resolve()):
        raise ValueError("数据目录不能位于源码目录内")
    files, size = [], 0
    for parent_name, dirs, names in os.walk(source, followlinks=False):
        parent = Path(parent_name)
        for name in dirs + names:
            if linked(parent / name):
                raise ValueError("源码中不允许符号链接或junction")
        dirs[:] = [n for n in dirs if n.casefold() not in EXCLUDED]
        for name in names:
            p = parent / name
            if (
                name.casefold() in EXCLUDED
                or name.casefold().startswith(".env")
                or p.suffix.casefold() in {".key", ".pem", ".pyc"}
            ):
                continue
            size += p.stat().st_size
            files.append(p)
            if len(files) > 2000 or size > 50 * 1024 * 1024:
                raise ValueError("源码超出2000文件/50MiB快照上限")
    root, baseline = run_root / "workspace", run_root / "baseline"
    run_root.mkdir(parents=True, exist_ok=False)
    if github:
        # Local upload-pack copies the complete ancestry without hardlinks or
        # shared Git metadata; no network or source refs are modified.
        git("clone", "--no-local", "--no-checkout", "--", str(source), str(root))
        git("remote", "set-url", "origin", origin, cwd=root)
        git("reset", "--mixed", "HEAD", cwd=root)
    else:
        root.mkdir()
    baseline.mkdir()
    original = run_root / "original"
    original.mkdir()
    manifest = {}
    for path in files:
        rel = path.relative_to(source)
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        archived = original / rel
        archived.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, archived)
        if protected(rel):
            frozen = baseline / rel
            frozen.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, frozen)
            manifest[rel.as_posix()] = digest(frozen)
    (run_root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return WorkspaceSnapshot(run_id, root, baseline, manifest)


def load_snapshot(root: Path) -> WorkspaceSnapshot:
    parent = root.parent
    manifest = json.loads((parent / "manifest.json").read_text(encoding="utf-8"))
    if not root.is_dir() or linked(root) or not (parent / "baseline").is_dir() or linked(parent / "baseline"):
        raise ValueError("工作区或测试基线缺失")
    if not isinstance(manifest, dict):
        raise ValueError("测试清单格式错误")
    for name, expected in manifest.items():
        if not isinstance(name, str) or not isinstance(expected, str):
            raise ValueError("测试清单字段类型错误")
        rel = Path(name)
        if (
            rel.is_absolute()
            or ".." in rel.parts
            or "\\" in name
            or not protected(rel)
            or not re.fullmatch("[0-9a-f]{64}", expected)
        ):
            raise ValueError("测试清单路径或摘要错误")
    return WorkspaceSnapshot(parent.name, root, parent / "baseline", manifest)


def verify_baseline(snapshot: WorkspaceSnapshot) -> None:
    for rel, expected in snapshot.manifest.items():
        for root in (snapshot.root, snapshot.baseline_dir):
            path = root / rel
            if linked(path) or not path.is_file() or digest(path) != expected:
                raise ValueError(f"原始测试/配置发生漂移: {rel}")
    actual = {
        p.relative_to(snapshot.root).as_posix()
        for p in snapshot.root.rglob("*")
        if p.is_file()
        and protected(p.relative_to(snapshot.root))
        and not any(n.casefold() in EXCLUDED for n in p.relative_to(snapshot.root).parts)
    }
    if actual != set(snapshot.manifest):
        raise ValueError("测试/配置集合发生变化")


def assert_writable(snapshot: WorkspaceSnapshot, relative_path: str) -> None:
    from ..tools.registry import ToolError

    path = (snapshot.root / relative_path).resolve()
    if not path.is_relative_to(snapshot.root.resolve()) or protected(
        path.relative_to(snapshot.root.resolve())
    ):
        raise ToolError("禁止写入原测试、配置或工作区外路径")
