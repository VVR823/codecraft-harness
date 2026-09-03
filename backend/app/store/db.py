"""SQLite 存储层（Q9：六字段 checkpoint 表 + runs/traces/usage，WAL）。

表：
- runs:        一次任务运行的总账
- checkpoints: 断点存档（run_id, step, done_actions, ctx_summary, ws_hash, tokens_used）
- traces:      行车记录仪（JSONL 同构事件，摘要不进全文）
- usage:       成本记账
"""
import sqlite3
import threading
from datetime import datetime, timezone

from ..config import DB_PATH

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id    TEXT PRIMARY KEY,
    task_id   TEXT NOT NULL,
    status    TEXT NOT NULL DEFAULT 'pending',   -- pending|running|paused|done|failed
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS checkpoints (
    run_id     TEXT NOT NULL,
    step       INTEGER NOT NULL,
    done_actions TEXT NOT NULL DEFAULT '[]',      -- JSON 数组：已完成动作，resume 不重放
    ctx_summary  TEXT NOT NULL DEFAULT '',        -- 上下文摘要，续跑时喂 LLM
    ws_hash      TEXT NOT NULL DEFAULT '',        -- 工作区文件哈希快照（漂移识别，M2）
    tokens_used  INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (run_id, step)
);
CREATE TABLE IF NOT EXISTS traces (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    step  INTEGER NOT NULL,
    ts    TEXT NOT NULL,
    tool  TEXT NOT NULL DEFAULT '',
    args_summary   TEXT NOT NULL DEFAULT '',
    output_summary TEXT NOT NULL DEFAULT '',
    tokens INTEGER NOT NULL DEFAULT 0,
    verdict TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS usage (
    run_id TEXT PRIMARY KEY,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    cost REAL NOT NULL DEFAULT 0
);
"""

_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def init_db() -> None:
    DB_PATH.parent.mkdir(exist_ok=True, parents=True)
    with _lock, sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA)
        conn.commit()


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------- runs ----------------
def create_run(run_id: str, task_id: str) -> None:
    with _lock, _conn() as conn:
        conn.execute(
            "INSERT INTO runs(run_id, task_id, status, created_at, updated_at) VALUES(?,?,?,?,?)",
            (run_id, task_id, "pending", _now(), _now()),
        )


def update_run_status(run_id: str, status: str) -> None:
    with _lock, _conn() as conn:
        conn.execute(
            "UPDATE runs SET status=?, updated_at=? WHERE run_id=?",
            (status, _now(), run_id),
        )


def get_run(run_id: str) -> sqlite3.Row | None:
    with _lock, _conn() as conn:
        return conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()


# ---------------- checkpoints（Q9 六字段） ----------------
def save_checkpoint(run_id: str, step: int, done_actions: list, ctx_summary: str = "",
                    ws_hash: str = "", tokens_used: int = 0) -> None:
    import json
    with _lock, _conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO checkpoints(run_id, step, done_actions, ctx_summary, ws_hash, tokens_used, created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (run_id, step, json.dumps(done_actions, ensure_ascii=False),
             ctx_summary, ws_hash, tokens_used, _now()),
        )


def last_checkpoint(run_id: str) -> sqlite3.Row | None:
    with _lock, _conn() as conn:
        return conn.execute(
            "SELECT * FROM checkpoints WHERE run_id=? ORDER BY step DESC LIMIT 1",
            (run_id,),
        ).fetchone()


# ---------------- traces（Q13：一行一事件，只存摘要） ----------------
def append_trace(run_id: str, step: int, tool: str, args_summary: str,
                 output_summary: str, tokens: int = 0, verdict: str = "") -> None:
    with _lock, _conn() as conn:
        conn.execute(
            "INSERT INTO traces(run_id, step, ts, tool, args_summary, output_summary, tokens, verdict)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (run_id, step, _now(), tool, args_summary, output_summary, tokens, verdict),
        )


def list_traces(run_id: str) -> list[sqlite3.Row]:
    with _lock, _conn() as conn:
        return conn.execute(
            "SELECT * FROM traces WHERE run_id=? ORDER BY id", (run_id,)
        ).fetchall()


# ---------------- usage ----------------
def bump_usage(run_id: str, tokens: int, cost: float = 0.0) -> None:
    with _lock, _conn() as conn:
        conn.execute(
            "INSERT INTO usage(run_id, total_tokens, cost) VALUES(?,?,?)"
            " ON CONFLICT(run_id) DO UPDATE SET total_tokens=total_tokens+?, cost=cost+?",
            (run_id, tokens, cost, tokens, cost),
        )


def get_usage(run_id: str) -> sqlite3.Row | None:
    with _lock, _conn() as conn:
        return conn.execute("SELECT * FROM usage WHERE run_id=?", (run_id,)).fetchone()
