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
import uuid
from contextlib import contextmanager, closing
from contextvars import ContextVar
from pathlib import Path
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
CREATE TABLE IF NOT EXISTS memories (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,       -- 归属任务（按任务检索复用）
    run_id TEXT NOT NULL,        -- 来源 run（审计可溯）
    kind  TEXT NOT NULL,         -- success_path | failure_lesson | fact
    content TEXT NOT NULL,       -- 记忆正文（给后续 run 注入的提示）
    created_at TEXT NOT NULL
);
"""

_lock = threading.Lock()
_database: ContextVar[Path | None] = ContextVar("codecraft_database", default=None)


@contextmanager
def use_database(path):
    token = _database.set(Path(path))
    try:
        yield
    finally:
        _database.reset(token)


def database_path():
    return _database.get() or DB_PATH


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def init_db(path=None) -> None:
    from .migrations import inspect_schema, upgrade, MigrationRequired

    path = Path(path) if path else database_path()
    path.parent.mkdir(exist_ok=True, parents=True)
    if path.exists() and path.stat().st_size and inspect_schema(path) != 2:
        raise MigrationRequired("旧数据库需显式备份迁移；请使用迁移命令或新的数据目录")
    with _lock, closing(sqlite3.connect(path)) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        if inspect_schema(path) != 2:
            upgrade(conn)


@contextmanager
def _conn():
    conn = sqlite3.connect(database_path(), timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            yield conn
    finally:
        conn.close()


# ---------------- runs ----------------
def create_run(run_id: str, task_id: str, goal: str = "") -> None:
    with _lock, _conn() as conn:
        conn.execute(
            "INSERT INTO runs(run_id, task_id, goal, status, created_at, updated_at) VALUES(?,?,?,?,?,?)",
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


def list_runs(limit: int = 30) -> list[sqlite3.Row]:
    """run 列表（UI/审计用）：主表 + 最新 checkpoint 步数 + 累计 token。"""
    with _lock, _conn() as conn:
        rows = conn.execute(
            "SELECT r.run_id, r.task_id, r.status, r.created_at, r.updated_at,"
            "       (SELECT MAX(step) FROM checkpoints c WHERE c.run_id = r.run_id) AS step,"
            "       (SELECT total_tokens FROM usage u WHERE u.run_id = r.run_id) AS tokens"
            "  FROM runs r ORDER BY r.created_at DESC, r.rowid DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return rows


# ---------------- checkpoints（Q9 六字段 + ctx_messages） ----------------
def save_checkpoint(
    run_id: str,
    step: int,
    done_actions: list,
    ctx_messages: list | None = None,
    ctx_summary: str = "",
    ws_hash: str = "",
    tokens_used: int = 0,
) -> None:
    with _lock, _conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO checkpoints(run_id, step, done_actions, ctx_messages,"
            " ctx_summary, ws_hash, tokens_used, created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                run_id,
                step,
                json.dumps(done_actions, ensure_ascii=False),
                json.dumps(ctx_messages or [], ensure_ascii=False),
                ctx_summary,
                ws_hash,
                tokens_used,
                _now(),
            ),
        )


def last_checkpoint(run_id: str) -> sqlite3.Row | None:
    with _lock, _conn() as conn:
        return conn.execute(
            "SELECT * FROM checkpoints WHERE run_id=? ORDER BY step DESC LIMIT 1",
            (run_id,),
        ).fetchone()


# ---------------- traces（Q13） ----------------
def append_trace(
    run_id: str,
    step: int,
    tool: str,
    args_summary: str,
    output_summary: str,
    tokens: int = 0,
    verdict: str = "",
) -> None:
    with _lock, _conn() as conn:
        conn.execute(
            "INSERT INTO traces(run_id, step, ts, tool, args_summary, output_summary, tokens, verdict)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (run_id, step, _now(), tool, args_summary, output_summary, tokens, verdict),
        )


def list_traces(run_id: str) -> list[sqlite3.Row]:
    with _lock, _conn() as conn:
        return conn.execute("SELECT * FROM traces WHERE run_id=? ORDER BY id", (run_id,)).fetchall()


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
        return conn.execute("SELECT * FROM approvals WHERE run_id=? ORDER BY id", (run_id,)).fetchall()


# ---------------- plans（M4 planner：计划存档，防 resume 重规划） ----------------
def save_plan(run_id: str, plan_json: str, planning_tokens: int = 0, status: str = "ok") -> None:
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
        return conn.execute("SELECT * FROM plans WHERE run_id=?", (run_id,)).fetchone()


# ---------------- memories（M5-B3 长期记忆） ----------------


def save_memory(
    task_id: str,
    run_id: str,
    kind: str,
    content: str,
    dedupe_kind: bool = True,
    max_per_task: int | None = None,
) -> None:
    """跨 run 长期记忆：run 结束沉淀一条经验（成功后路径 / 失败教训）。

    写入端防膨胀（与 memory.py docstring 对齐，面试可指代码）：
    - dedupe_kind=True：同 task 同 kind 只留最新一条——先 INSERT 再删同
      kind 旧记录（旧的下沉，防止每跑一次 done 就叠一条 success_path）
    - max_per_task=N：该 task 总条数封顶，超出删最旧（id 最小）
    两者同事务原子完成；读取端另有限额控注入（get_memories_for_task LIMIT）。
    """
    if not content.strip():
        return
    with _lock, _conn() as conn:
        cur = conn.execute(
            "INSERT INTO memories(task_id, run_id, kind, content, created_at) VALUES(?,?,?,?,?)",
            (task_id, run_id, kind, content.strip(), _now()),
        )
        new_id = cur.lastrowid
        if dedupe_kind:
            conn.execute(
                "DELETE FROM memories WHERE task_id=? AND kind=? AND id<>?",
                (task_id, kind, new_id),
            )
        if max_per_task:
            conn.execute(
                "DELETE FROM memories WHERE task_id=? AND id NOT IN"
                " (SELECT id FROM memories WHERE task_id=?"
                "  ORDER BY id DESC LIMIT ?)",
                (task_id, task_id, max_per_task),
            )


def get_memories_for_task(task_id: str, limit: int = 3) -> list[dict]:
    """按任务检索历史记忆（新 run 开始时注入；同任务跨 run 复用经验）。"""
    with _lock, _conn() as conn:
        rows = conn.execute(
            "SELECT task_id, run_id, kind, content, created_at FROM memories"
            " WHERE task_id=? ORDER BY id DESC LIMIT ?",
            (task_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def configure_run(run_id, options, workspace_path, baseline_path):
    with _lock, _conn() as conn:
        conn.execute(
            "UPDATE runs SET options_json=?,workspace_path=?,baseline_path=? WHERE run_id=?",
            (json.dumps(options), str(workspace_path), str(baseline_path), run_id),
        )


def set_error(run_id, code, message, status="failed"):
    with _lock, _conn() as conn:
        conn.execute(
            "UPDATE runs SET error_code=?,error_message=?,status=?,updated_at=? WHERE run_id=?",
            (code, message, status, _now(), run_id),
        )


def record_model_call(run_id, request_id, phase, reply, *, outcome):
    values = [getattr(reply, key, None) for key in ("prompt_tokens", "completion_tokens", "total_tokens")]
    known = all(type(v) is int and v >= 0 for v in values)
    if known and sum(v for v in values[:2] if isinstance(v, int)) != values[2]:
        known = False
    if not known:
        values = [None, None, None]
    with _lock, _conn() as conn:
        inserted = conn.execute(
            "INSERT OR IGNORE INTO model_calls VALUES(?,?,?,?,?,?,?,?)",
            (request_id, run_id, phase, *values, outcome, _now()),
        ).rowcount
        if inserted and known:
            conn.execute(
                "INSERT INTO usage(run_id,total_tokens,cost) VALUES(?,?,0) ON CONFLICT(run_id) DO UPDATE SET total_tokens=total_tokens+excluded.total_tokens",
                (run_id, values[2]),
            )


def call_totals(run_id):
    with _lock, _conn() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(total_tokens),0) AS tokens,COUNT(*) AS calls,SUM(CASE WHEN total_tokens IS NULL THEN 1 ELSE 0 END) AS unknown_calls FROM model_calls WHERE run_id=?",
            (run_id,),
        ).fetchone()
        return {"tokens": row["tokens"], "calls": row["calls"], "unknown_calls": row["unknown_calls"] or 0}


def begin_model_call(run_id, request_id, phase):
    with _lock, _conn() as conn:
        conn.execute(
            "INSERT INTO model_calls VALUES(?,?,?,?,?,?,?,?)",
            (request_id, run_id, phase, None, None, None, "started", _now()),
        )


def finish_model_call(request_id, reply):
    values = [getattr(reply, key, None) for key in ("prompt_tokens", "completion_tokens", "total_tokens")]
    known = all(type(v) is int and v >= 0 for v in values)
    if known and sum(v for v in values[:2] if isinstance(v, int)) != values[2]:
        known = False
    if not known:
        values = [None, None, None]
    with _lock, _conn() as conn:
        row = conn.execute(
            "SELECT run_id,outcome FROM model_calls WHERE request_id=?", (request_id,)
        ).fetchone()
        if row is None or row["outcome"] != "started":
            raise ValueError("调用不存在或已结算")
        conn.execute(
            "UPDATE model_calls SET prompt_tokens=?,completion_tokens=?,total_tokens=?,outcome=? WHERE request_id=?",
            (*values, "received" if known else "usage_unknown", request_id),
        )
        if known:
            conn.execute(
                "INSERT INTO usage(run_id,total_tokens,cost) VALUES(?,?,0) ON CONFLICT(run_id) DO UPDATE SET total_tokens=total_tokens+excluded.total_tokens",
                (row["run_id"], values[2]),
            )


def save_budget(run_id, new_limit):
    with _lock, _conn() as conn:
        row = conn.execute("SELECT options_json FROM runs WHERE run_id=?", (run_id,)).fetchone()
        options = json.loads(row[0] or "{}")
        before = options.get("token_budget", 30000)
        options["token_budget"] = new_limit
        conn.execute("UPDATE runs SET options_json=? WHERE run_id=?", (json.dumps(options), run_id))
        conn.execute(
            "INSERT INTO approvals(run_id,ts,kind,action,note) VALUES(?,?,?,?,?)",
            (run_id, _now(), "budget_continue", "approved", f"{before} -> {new_limit}"),
        )


def begin_action(run_id: str, step: int, tool: str, args: dict) -> str:
    action_id = uuid.uuid4().hex
    with _lock, _conn() as conn:
        conn.execute(
            "INSERT INTO actions VALUES(?,?,?,?,?,'started','')",
            (action_id, run_id, step, tool, json.dumps(args, ensure_ascii=False)),
        )
    return action_id


def commit_action_checkpoint(action_id: str, result: dict, checkpoint: dict) -> None:
    with _lock, _conn() as conn:
        action = conn.execute(
            "SELECT * FROM actions WHERE action_id=? AND state='started'", (action_id,)
        ).fetchone()
        if action is None:
            raise ValueError("动作不存在或已提交")
        if action["run_id"] != checkpoint["run_id"] or action["step"] != checkpoint["step"]:
            raise ValueError("动作与断点身份不一致")
        conn.execute(
            "UPDATE actions SET state='completed',result_json=? WHERE action_id=?",
            (json.dumps(result, ensure_ascii=False), action_id),
        )
        conn.execute(
            "INSERT INTO traces(run_id,step,ts,tool,args_summary,output_summary,tokens,verdict) VALUES(?,?,?,?,?,?,?,?)",
            (
                action["run_id"],
                action["step"],
                _now(),
                action["tool"],
                action["args_json"][:500],
                result.get("output", "")[:3000],
                result.get("tokens", 0),
                "ok" if result.get("ok") else "fail",
            ),
        )
        cp = checkpoint
        conn.execute(
            "INSERT OR REPLACE INTO checkpoints VALUES(?,?,?,?,?,?,?,?)",
            (
                cp["run_id"],
                cp["step"],
                json.dumps(cp["done_actions"], ensure_ascii=False),
                json.dumps(cp["ctx_messages"], ensure_ascii=False),
                cp["ctx_summary"],
                cp["ws_hash"],
                cp["tokens_used"],
                _now(),
            ),
        )


def inspect_recovery(run_id: str) -> dict:
    with _lock, _conn() as conn:
        pending = conn.execute(
            "SELECT action_id,tool,step FROM actions WHERE run_id=? AND state='started'", (run_id,)
        ).fetchone()
    if pending:
        return {
            "status": "interrupted",
            "can_auto_resume": False,
            "reason": f"动作{pending['step']} {pending['tool']}结果未确定；不自动重放",
        }
    return {"status": "safe", "can_auto_resume": True, "reason": ""}


def committed_actions(run_id):
    with _lock, _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM actions WHERE run_id=? AND state='completed' ORDER BY step", (run_id,)
        ).fetchall()
    return [
        {**dict(r), "args": json.loads(r["args_json"]), "result": json.loads(r["result_json"])} for r in rows
    ]


def claim_run(run_id, allowed):
    with _lock, _conn() as conn:
        placeholders = ",".join("?" for _ in allowed)
        updated = conn.execute(
            f"UPDATE runs SET status='running',error_code='',error_message='',updated_at=? WHERE run_id=? AND status IN ({placeholders})",
            (_now(), run_id, *allowed),
        ).rowcount
        if not updated:
            raise ValueError("run状态不允许执行或已有执行者")
