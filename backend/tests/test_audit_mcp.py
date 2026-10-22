"""Real UTF-8 stdio and bounded read-only query regressions."""

import sqlite3
import sys
from contextlib import closing
from pathlib import Path

from app.mcp import demo_server
from app.mcp.mcp_client import MCPClient


def test_mcp_python_server_uses_utf8_independent_of_parent_encoding(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHONIOENCODING", "ascii")
    path = tmp_path / "harness.db"
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("CREATE TABLE words(value TEXT)")
        conn.execute("INSERT INTO words VALUES (?)", ("中文记录",))
    server = Path(demo_server.__file__)
    client = MCPClient("utf8", [sys.executable, str(server), "--db-dir", str(tmp_path)])
    try:
        client.start()
        assert "只读" in client.list_tools()[0].description
        assert "中文记录" in client.call_tool(
            "sqlite_query", {"database": "harness.db", "sql": "SELECT value FROM words"}
        )
    finally:
        client.stop()


def test_mcp_database_directory_can_contain_uri_fragment_characters(tmp_path):
    directory = tmp_path / "data#snapshot"
    directory.mkdir()
    with closing(sqlite3.connect(directory / "harness.db")) as conn, conn:
        conn.execute("CREATE TABLE example(value TEXT)")
        conn.execute("INSERT INTO example VALUES ('expected')")
    result = demo_server._handle_call(
        {"name": "sqlite_query", "arguments": {"database": "harness.db", "sql": "SELECT value FROM example"}},
        directory,
    )
    assert not result["isError"], result
    assert "expected" in result["content"][0]["text"]
    assert not (tmp_path / "data").exists()


def test_mcp_only_materializes_the_preview_rows(tmp_path, monkeypatch):
    path = tmp_path / "harness.db"
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("CREATE TABLE numbers(value INTEGER)")
        conn.executemany("INSERT INTO numbers VALUES (?)", [(i,) for i in range(100)])
    original_connect = sqlite3.connect
    visited = []

    def connect(*args, **kwargs):
        conn = original_connect(*args, **kwargs)
        conn.create_function("audit_tick", 1, lambda value: visited.append(value) or value)
        return conn

    monkeypatch.setattr(demo_server.sqlite3, "connect", connect)
    result = demo_server._handle_call(
        {
            "name": "sqlite_query",
            "arguments": {"database": "harness.db", "sql": "SELECT audit_tick(value) FROM numbers"},
        },
        tmp_path,
    )
    assert not result["isError"]
    assert len(visited) <= 22, "A 20-row preview should not evaluate the entire result set"


def test_mcp_stop_reaps_process_and_closes_both_pipes(tmp_path):
    client = MCPClient(
        "cleanup", [sys.executable, str(Path(demo_server.__file__)), "--db-dir", str(tmp_path)]
    )
    client.start()
    proc = client.proc
    try:
        client.stop()
        assert proc.poll() is not None
        assert proc.stdin.closed
        assert proc.stdout.closed
        client.stop()  # Repeated cleanup remains safe.
    finally:
        proc.stdin.close()
        proc.stdout.close()
