import importlib
import sqlite3
from contextlib import closing

import pytest


def test_legacy_requires_explicit_migration_and_backup(tmp_path):
    from app.store import db

    migration = importlib.import_module("app.store.migrations")
    path = tmp_path / "legacy.db"
    with closing(sqlite3.connect(path)) as con, con:
        con.executescript(db._SCHEMA)
        con.execute("INSERT INTO runs VALUES('old','task','goal','paused','t','t')")
    with pytest.raises(migration.MigrationRequired):
        db.init_db(path)
    backup = migration.migrate_database(path, tmp_path / "backups")
    with closing(sqlite3.connect(backup)) as con:
        assert con.execute("SELECT status FROM runs").fetchone()[0] == "paused"
    assert migration.inspect_schema(path) == 2
    assert migration.migrate_database(path, tmp_path / "backups") is None
    with closing(sqlite3.connect(path)) as con:
        assert con.execute("SELECT goal, options_json FROM runs").fetchone() == ("goal", "")


def test_upgrade_failure_rolls_back_new_columns_and_version(tmp_path):
    from app.store import db, migrations

    class FailingConnection(sqlite3.Connection):
        def execute(self, sql, *args):
            if "CREATE TABLE IF NOT EXISTS actions" in sql:
                raise sqlite3.OperationalError("injected")
            return super().execute(sql, *args)

    path = tmp_path / "failure.db"
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript(db._SCHEMA)
    conn = sqlite3.connect(path, factory=FailingConnection)
    try:
        with pytest.raises(sqlite3.OperationalError):
            migrations.upgrade(conn)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
        assert "options_json" not in {r[1] for r in conn.execute("PRAGMA table_info(runs)")}
        assert not conn.execute("SELECT name FROM sqlite_master WHERE name='model_calls'").fetchall()
    finally:
        conn.close()


def test_failure_also_rolls_back_missing_legacy_tables(tmp_path):
    from app.store import db, migrations

    class Failing(sqlite3.Connection):
        def execute(self, sql, *args):
            if "CREATE TABLE IF NOT EXISTS actions" in sql:
                raise sqlite3.OperationalError("injected")
            return super().execute(sql, *args)

    path = tmp_path / "partial.db"
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript(db._SCHEMA.split(";")[0] + ";")
    with closing(sqlite3.connect(path, factory=Failing)) as conn:
        before = conn.execute("SELECT name,sql FROM sqlite_master ORDER BY name").fetchall()
        with pytest.raises(sqlite3.OperationalError):
            migrations.upgrade(conn)
        assert conn.execute("SELECT name,sql FROM sqlite_master ORDER BY name").fetchall() == before
