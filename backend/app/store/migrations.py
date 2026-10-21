"""Explicit backup + versioned migration. Never migrate a nonempty DB on startup."""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import closing
from pathlib import Path


class MigrationRequired(RuntimeError):
    pass


def inspect_schema(path: Path) -> int:
    if not path.exists() or path.stat().st_size == 0:
        return 0
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        return conn.execute("PRAGMA user_version").fetchone()[0]


def upgrade(conn) -> None:
    from .db import _SCHEMA

    # The controlled schema contains no semicolons in strings. Execute its
    # statements individually: executescript would commit outside this txn.
    conn.execute("BEGIN IMMEDIATE")
    try:
        for statement in _SCHEMA.split(";"):
            if statement.strip():
                conn.execute(statement)
        columns = {r[1] for r in conn.execute("PRAGMA table_info(runs)")}
        for name in ("options_json", "workspace_path", "baseline_path", "error_code", "error_message"):
            if name not in columns:
                conn.execute(f"ALTER TABLE runs ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS model_calls (request_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, phase TEXT NOT NULL, prompt_tokens INTEGER, completion_tokens INTEGER, total_tokens INTEGER, outcome TEXT NOT NULL, created_at TEXT NOT NULL)"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS model_calls_run ON model_calls(run_id)")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS actions (action_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, step INTEGER NOT NULL, tool TEXT NOT NULL, args_json TEXT NOT NULL, state TEXT NOT NULL, result_json TEXT NOT NULL DEFAULT '', UNIQUE(run_id, step))"
        )
        conn.execute("PRAGMA user_version=2")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def migrate_database(path: Path, backup_dir: Path) -> Path | None:
    version = inspect_schema(path)
    if version == 2:
        return None
    if version != 0 or not path.is_file():
        raise MigrationRequired("不支持该schema版本或数据库不存在")
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / f"{path.stem}-{uuid.uuid4().hex}.sqlite3"
    with closing(sqlite3.connect(path)) as source, closing(sqlite3.connect(backup)) as target:
        source.backup(target)
        if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("备份完整性失败；未迁移")
        upgrade(source)
    return backup


def main():
    import argparse
    import json

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument("--backup-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        backup = migrate_database(args.database.resolve(), args.backup_dir.resolve())
    except (MigrationRequired, OSError, sqlite3.Error) as exc:
        parser.exit(1, f"迁移未完成：{type(exc).__name__}；请保留原库并检查备份目录\n")
    print(json.dumps({"schema_version": 2, "backup": str(backup) if backup else None}))


if __name__ == "__main__":
    main()
