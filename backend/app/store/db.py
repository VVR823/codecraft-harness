"""SQLite 存储层 v2（Q9 六字段 + ctx_messages 完整消息 + runs.goal）。

表：
- runs:        一次任务运行总账（含 goal：resume 重建上下文需要）
- checkpoints: 断点存档（run_id, step, done_actions, ctx_messages, ctx_summary, ws_hash, tokens_used）
- traces:      行车记录仪（JSONL 同构事件，摘要）
- usage:       成本记账
- approvals:   审批记录（M2：超预算续跑 / HIGH 高危工具——"人工批准"证据落这里）
- plans:       任务计划（M4 planner：run 级 1 行，审计/回放 + 防 resume 重规划）
"""
import json
import sqlite3
import threading
from datetime import datetime, timezone

from ..config import DB_PATH

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id    TEXT PRIMARY KEY,
    task_id   TEXT NOT NULL,
    goal      TEXT NOT NULL DEFAULT '',
    status    TEXT NOT NULL DEFAULT 'pending',   -- pending|running|paused|done|failed
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS checkpoints (
    run_id     TEXT NOT NULL,
    step       INTEGER NOT NULL,
    done_actions TEXT NOT NULL DEFAULT '[]',      -- JSON：已完成动作，resume 不重放
    ctx_messages TEXT NOT NULL DEFAULT '[]',      -- JSON：完整对话消息（resume 重建上下文）
    ctx_summary  TEXT NOT NULL DEFAULT '',
    ws_hash      TEXT NOT NULL DEFAULT '',
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
CREATE TABLE IF NOT EXISTS approvals (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    ts    TEXT NOT NULL,
    kind  TEXT NOT NULL,        -- budget_continue | high_tool
    action TEXT NOT NULL,       -- requested | approved | denied
    note  TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS plans (
    run_id  TEXT PRIMARY KEY,
    plan_json TEXT NOT NULL DEFAULT '',      -- AgentPlan JSON（失败降级时为空串）
    planning_tokens INTEGER NOT NULL DEFAULT 0,  -- 规划调用耗的 token（meter 前后差值）
    status TEXT NOT NULL DEFAULT 'ok',       -- ok | failed（重试耗尽降级）
    created_at TEXT NOT NULL
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
def create_run(run_id: str, task_id: str, goal: str = "") -> None:
    with _lock, _conn() as conn:
        conn.execute(
            "INSERT INTO runs(run_id, task_id, goal, status, created_at, updated_at)"
            " VALUES(?,?,?,?,?,?)",
            (run_id, task_id, goal, "pending", _now(), _now()),
        )


def update_run_status(run_id: str, status: str) -> None:
    with _lock, _conn() as conn:
        conn.execute(
            "UPDATE runs SET status=?, updated_at=? WHERE run_id=?",
            (status, _now(), run_id),
        )


def mark_stale_runs_failed(status: str = "running") -> int:
    """把历史残留指定状态的 run 标 failed（进程被杀时 loop 来不及标；run_all 启动时清场）。

    正常流程的 run 最终都会落到 done/failed，残留 running 只可能是异常中断。
    """
    with _lock, _conn() as conn:
        cur = conn.execute(
            "UPDATE runs SET status='failed', updated_at=? WHERE status=?",
            (_now(), status),
        )
        return cur.rowcount


def get_run(run_id: str) -> sqlite3.Row | None:
    with _lock, _conn() as conn:
        return conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()


# ---------------- checkpoints（Q9 六字段 + ctx_messages） ----------------
def save_checkpoint(run_id: str, step: int, done_actions: list,
                    ctx_messages: list | None = None, ctx_summary: str = "",
                    ws_hash: str = "", tokens_used: int = 0) -> None:
    with _lock, _conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO checkpoints(run_id, step, done_actions, ctx_messages,"
            " ctx_summary, ws_hash, tokens_used, created_at) VALUES(?,?,?,?,?,?,?,?)",
            (run_id, step,
             json.dumps(done_actions, ensure_ascii=False),
             json.dumps(ctx_messages or [], ensure_ascii=False),
             ctx_summary, ws_hash, tokens_used, _now()),
        )


def last_checkpoint(run_id: str) -> sqlite3.Row | None:
    with _lock, _conn() as conn:
        return conn.execute(
            "SELECT * FROM checkpoints WHERE run_id=? ORDER BY step DESC LIMIT 1",
            (run_id,),
        ).fetchone()


# ---------------- traces（Q13） ----------------
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


# ---------------- approvals（M2：审批记录，底线 4 证据） ----------------
def append_approval(run_id: str, kind: str, action: str, note: str = "") -> None:
    """记一条审批事件：kind=budget_continue|high_tool；action=requested|approved|denied。"""
    with _lock, _conn() as conn:
        conn.execute(
            "INSERT INTO approvals(run_id, ts, kind, action, note) VALUES(?,?,?,?,?)",
            (run_id, _now(), kind, action, note),
        )


def last_approval(run_id: str, kind: str) -> sqlite3.Row | None:
    """该 run 某类审批的最后一条记录（判断是否已放行/拒绝）。"""
    with _lock, _conn() as conn:
        return conn.execute(
            "SELECT * FROM approvals WHERE run_id=? AND kind=? ORDER BY id DESC LIMIT 1",
            (run_id, kind),
        ).fetchone()


def list_approvals(run_id: str) -> list[sqlite3.Row]:
    with _lock, _conn() as conn:
        return conn.execute(
            "SELECT * FROM approvals WHERE run_id=? ORDER BY id", (run_id,)
        ).fetchall()


# ---------------- plans（M4 planner：计划存档，防 resume 重规划） ----------------
def save_plan(run_id: str, plan_json: str, planning_tokens: int = 0,
              status: str = "ok") -> None:
    """存一次规划结果（run 级 1 行）。plan_json 为空串 = 重试耗尽降级（status=failed）。

    产出即落库（原子）：进程若被杀在 plan phase，resume 时 get_plan 已有行 →
    不重规划、不重付规划 token。
    """
    with _lock, _conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO plans(run_id, plan_json, planning_tokens, status, created_at)"
            " VALUES(?,?,?,?,?)",
            (run_id, plan_json, planning_tokens, status, _now()),
        )


def get_plan(run_id: str) -> sqlite3.Row | None:
    with _lock, _conn() as conn:
        return conn.execute(
            "SELECT * FROM plans WHERE run_id=?", (run_id,)
        ).fetchone()
