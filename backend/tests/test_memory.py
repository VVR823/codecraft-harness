"""M5-B3 长期记忆单测：沉淀（成功路径/失败教训）/ 检索 / run 注入 / 默认关口径。"""
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.runtime import memory  # noqa: E402
from app.runtime.loop import HarnessLoop  # noqa: E402
from app.store import db  # noqa: E402

pytestmark = pytest.mark.usefixtures("db_ready")


@pytest.fixture()
def db_ready():
    db.init_db()


@pytest.fixture()
def task_id() -> str:
    """唯一任务名：真库测试不污染 t1~t3 的真实记忆。"""
    return f"mem_test_{uuid.uuid4().hex[:8]}"


def _cleanup(task_id: str):
    """清理该测试任务写进真库的记忆行。"""
    import sqlite3
    conn = sqlite3.connect(db.DB_PATH)
    try:
        conn.execute("DELETE FROM memories WHERE task_id=?", (task_id,))
        conn.commit()
    finally:
        conn.close()


# ---------- 沉淀 ----------

def test_distill_success_path(task_id):
    actions = [
        {"tool": "read_file", "args": {"path": "utils.py"}},
        {"tool": "edit_file", "args": {"path": "utils.py",
                                       "old": "return text.strip(' ')",
                                       "new": "return text.strip()"}},
        {"tool": "run_tests", "args": {}},
    ]
    got = memory.distill("run_a", task_id, actions, "done")
    assert got and "成功路径" in got
    assert "strip(' ')" in got and "strip()" in got and "utils.py" in got
    mems = db.get_memories_for_task(task_id)
    assert len(mems) == 1 and mems[0]["kind"] == "success_path"
    _cleanup(task_id)


def test_distill_failure_lesson(task_id):
    got = memory.distill("run_b", task_id, [], "failed", reason="LLM 连续失败: 429 限流")
    assert got and "失败教训" in got and "429" in got
    mems = db.get_memories_for_task(task_id)
    assert mems[0]["kind"] == "failure_lesson"
    _cleanup(task_id)


def test_distill_skips_paused_and_empty():
    # paused（可续跑）不沉淀；failed 但无 reason 不沉淀
    assert memory.distill("r1", "x", [], "paused") is None
    assert memory.distill("r2", "x", [], "failed") is None
    # done 但没写过文件（没实质改动）不沉淀
    assert memory.distill("r3", "x", [{"tool": "read_file", "args": {}}],
                          "done") is None


# ---------- 防膨胀（写入端去重 + 上限） ----------

def test_distill_dedupes_same_kind_keeps_latest(task_id):
    """同任务连跑两次 done → success_path 只留最新一条（旧的下沉）。"""
    actions = [{"tool": "edit_file", "args": {"path": "utils.py",
                                              "old": "a", "new": "b"}}]
    memory.distill("run_1", task_id, actions, "done")
    memory.distill("run_2", task_id, actions, "done")
    mems = db.get_memories_for_task(task_id, limit=10)
    sp = [m for m in mems if m["kind"] == "success_path"]
    assert len(sp) == 1 and sp[0]["run_id"] == "run_2"
    _cleanup(task_id)


def test_save_memory_caps_per_task(task_id):
    """每任务总量封顶 MAX_MEMORIES_PER_TASK：超出删最旧（多 kind 场景兜底）。"""
    n = memory.MAX_MEMORIES_PER_TASK
    for i in range(n + 3):
        db.save_memory(task_id, f"r{i}", f"kind_{i}", f"内容{i}",
                       dedupe_kind=False, max_per_task=n)
    mems = db.get_memories_for_task(task_id, limit=100)
    assert len(mems) == n
    run_ids = [m["run_id"] for m in mems]
    assert "r0" not in run_ids and "r2" not in run_ids   # 最旧 3 条被裁
    assert f"r{n + 2}" in run_ids                        # 最新保留
    _cleanup(task_id)


# ---------- 检索渲染 ----------

def test_render_for_goal_empty_when_no_memory(task_id):
    assert memory.render_for_goal(task_id) == ""
    _cleanup(task_id)


def test_render_for_goal_returns_recent(task_id):
    memory.distill("run_1", task_id, [], "failed", reason="测试一直红：x 没初始化")
    memory.distill("run_2", task_id, [
        {"tool": "edit_file", "args": {"path": "m.py", "old": "a", "new": "b"}}],
        "done")
    text = memory.render_for_goal(task_id)
    assert "经验" in text and "教训" in text
    assert "历史 run 的记忆" in text
    _cleanup(task_id)


# ---------- loop 注入与沉淀 ----------

def _ws(tmp_path):
    (tmp_path / "utils.py").write_text(
        'def trim_whitespace(text):\n    return text.strip()\n', encoding="utf-8")
    return tmp_path


def _mk_loop(ws, task_id: str, use_memory: bool) -> HarnessLoop:
    return HarnessLoop(ws, "修好 trim_whitespace 让测试全绿",
                       decider=lambda m: None,
                       run_id=uuid.uuid4().hex[:12], task_id=task_id,
                       use_memory=use_memory)


def test_loop_initial_message_injects_memory_when_enabled(tmp_path, task_id):
    """use_memory=True 且有历史记忆 → 初始 user 消息注入；False → 不注入（口径）。"""
    # 先造一条该任务的历史记忆
    db.save_memory(task_id, "past_run", "failure_lesson", "上次栽在没初始化 x")
    ws = _ws(tmp_path)
    on = _mk_loop(ws, task_id, use_memory=True)
    msgs = on._initial_messages(on.goal, fresh=True)
    assert "历史 run 的记忆" in msgs[1]["content"]
    assert "上次栽在没初始化 x" in msgs[1]["content"]
    off = _mk_loop(ws, task_id, use_memory=False)
    msgs_off = off._initial_messages(off.goal, fresh=True)
    assert "历史 run 的记忆" not in msgs_off[1]["content"]
    _cleanup(task_id)


def test_loop_maybe_distill_writes_on_done(tmp_path, task_id):
    """_maybe_distill：done 且 done_actions 有 edit_file → 沉淀 success_path。"""
    ws = _ws(tmp_path)
    loop = _mk_loop(ws, task_id, use_memory=True)
    db.init_db()
    # 模拟 run 已产生的动作（与真实 done run 的 done_actions 形态一致）
    loop.done_actions = [
        {"step": 1, "tool": "edit_file",
         "args": {"path": "utils.py", "old": "strip(' ')", "new": "strip()"}},
    ]
    loop._maybe_distill({"status": "done"})
    mems = db.get_memories_for_task(task_id)
    assert any(m["kind"] == "success_path" for m in mems)
    # 内容里应含文件与改动（验证不再只出空 '?: →'）
    content = [m["content"] for m in mems if m["kind"] == "success_path"][0]
    assert "utils.py" in content and "strip()" in content
    _cleanup(task_id)


def test_loop_maybe_distill_paused_skips(tmp_path, task_id):
    """paused（可续跑）不沉淀。"""
    ws = _ws(tmp_path)
    loop = _mk_loop(ws, task_id, use_memory=True)
    loop._maybe_distill({"status": "paused", "reason": "步数上限"})
    assert db.get_memories_for_task(task_id) == []
    _cleanup(task_id)
