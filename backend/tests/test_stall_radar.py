"""O4 空转雷达测试（2026-09-05）。

直接驱动 _stall_radar 方法（不跑真 LLM）：连续 STALL_LIMIT 个零进展动作 → 触发；
有进展动作（edit_file/run_tests）→ 不触发；同一段空转只提醒一次；stall_warning=True
时把提醒注入 messages（默认 False 纯观测不改执行流）。
"""
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.runtime.loop import HarnessLoop  # noqa: E402
from app.store import db  # noqa: E402


def _mk_loop(tmp_path, stall_warning: bool = False) -> HarnessLoop:
    loop = HarnessLoop(tmp_path, "测试目标", decider=lambda m: None,
                       run_id=uuid.uuid4().hex[:12], stall_warning=stall_warning)
    loop.messages = []  # 未跑 run，手动初始化
    return loop


def _stall_traces(loop) -> list:
    return [t for t in db.list_traces(loop.run_id) if t["verdict"] == "stall"]


def _reads(n: int) -> list[dict]:
    return [{"tool": "read_file", "args": {"path": "x.py"}, "step": i}
            for i in range(1, n + 1)]


def test_no_stall_below_limit(tmp_path):
    """2 个零进展动作 < STALL_LIMIT(3) → 不触发。"""
    loop = _mk_loop(tmp_path)
    loop.done_actions = _reads(2)
    loop._stall_radar()
    assert _stall_traces(loop) == []
    assert loop._stall_armed is False


def test_stall_triggers_after_3_reads(tmp_path):
    """连续 3 个 read_file → trace 记录 stall。"""
    loop = _mk_loop(tmp_path)
    loop.done_actions = _reads(3)
    loop._stall_radar()
    stalls = _stall_traces(loop)
    assert len(stalls) == 1
    assert "零进展" in stalls[0]["output_summary"] or "stall" in stalls[0]["verdict"]


def test_progress_action_resets(tmp_path):
    """第 3 个动作是 edit_file（进展）→ 不触发且复位。"""
    loop = _mk_loop(tmp_path)
    loop.done_actions = _reads(2) + [{"tool": "edit_file", "args": {}, "step": 3}]
    loop._stall_radar()
    assert _stall_traces(loop) == []
    assert loop._stall_armed is False


def test_same_stall_warns_only_once(tmp_path):
    """连续 5 个 read：只在第 3 个后提醒一次，之后不刷屏；出现进展后复位可再提醒。"""
    loop = _mk_loop(tmp_path)
    loop.done_actions = _reads(3)
    loop._stall_radar()
    assert len(_stall_traces(loop)) == 1

    loop.done_actions = _reads(4)
    loop._stall_radar()
    assert len(_stall_traces(loop)) == 1  # 同一段空转不重复提醒

    loop.done_actions = _reads(5)
    loop._stall_radar()
    assert len(_stall_traces(loop)) == 1

    # 出现进展 → 复位
    loop.done_actions = _reads(5) + [{"tool": "run_tests", "args": {}, "step": 6}]
    loop._stall_radar()
    assert len(_stall_traces(loop)) == 1

    # 又连续 3 个 read → 可再次提醒
    loop.done_actions = (_reads(5) + [{"tool": "run_tests", "args": {}, "step": 6}]
                         + [{"tool": "read_file", "args": {}, "step": 7},
                            {"tool": "read_file", "args": {}, "step": 8},
                            {"tool": "read_file", "args": {}, "step": 9}])
    loop._stall_radar()
    assert len(_stall_traces(loop)) == 2


def test_warning_injects_message_when_enabled(tmp_path):
    """stall_warning=True：提醒注入 messages（默认 False 不注入）。"""
    loop = _mk_loop(tmp_path, stall_warning=True)
    loop.done_actions = _reads(3)
    loop._stall_radar()
    assert any("系统提醒" in (m.get("content") or "") for m in loop.messages)
    assert loop._stall_armed is True


def test_warning_not_injected_by_default(tmp_path):
    """默认（stall_warning=False）：纯观测，messages 不动。"""
    loop = _mk_loop(tmp_path)
    loop.done_actions = _reads(3)
    loop._stall_radar()
    assert loop.messages == []
    assert len(_stall_traces(loop)) == 1  # trace 仍记录（观测不缺席）


# ---------- read_file 重复拦截（T4 大文件死循环实证补的防复发） ----------

def test_dup_read_same_range_blocked(tmp_path):
    """连续两次 read_file 同一文件同一区间 → 拦截并给转向提示。"""
    loop = _mk_loop(tmp_path)
    step = {"thought": "再读一次", "tool": "read_file",
            "args": {"path": "big.py", "offset": 590, "limit": 15}, "done": False}
    from app.runtime.protocol import AgentStep
    act = AgentStep(**step)
    # 上一步是同一文件同一区间
    loop._prev_action = {"tool": "read_file", "args": {"path": "big.py", "offset": 590, "limit": 15}}
    msg = loop._dup_action_block(act)
    assert msg and "同一文件同一区间" in msg


def test_dup_read_diff_range_allowed(tmp_path):
    """同一文件但区间不同（offset 变了）→ 放行（分页前进是合法的）。"""
    loop = _mk_loop(tmp_path)
    from app.runtime.protocol import AgentStep
    act = AgentStep(thought="翻页看后面", tool="read_file",
                    args={"path": "big.py", "offset": 600, "limit": 15}, done=False)
    loop._prev_action = {"tool": "read_file", "args": {"path": "big.py", "offset": 590, "limit": 15}}
    assert loop._dup_action_block(act) == ""


def test_dup_read_diff_file_allowed(tmp_path):
    """换了个文件读 → 放行。"""
    loop = _mk_loop(tmp_path)
    from app.runtime.protocol import AgentStep
    act = AgentStep(thought="读另一个文件", tool="read_file",
                    args={"path": "other.py"}, done=False)
    loop._prev_action = {"tool": "read_file", "args": {"path": "big.py", "offset": 590, "limit": 15}}
    assert loop._dup_action_block(act) == ""


# ---------- read_file 重复拦截（T4 大文件死循环实证补的防复发） ----------

def test_dup_read_same_range_blocked(tmp_path):
    """连续两次 read_file 同一文件同一区间 → 拦截并给转向提示。"""
    loop = _mk_loop(tmp_path)
    step = {"thought": "再读一次", "tool": "read_file",
            "args": {"path": "big.py", "offset": 590, "limit": 15}, "done": False}
    from app.runtime.protocol import AgentStep
    act = AgentStep(**step)
    # 上一步是同一文件同一区间
    loop._prev_action = {"tool": "read_file", "args": {"path": "big.py", "offset": 590, "limit": 15}}
    msg = loop._dup_action_block(act)
    assert msg and "同一文件同一区间" in msg


def test_dup_read_diff_range_allowed(tmp_path):
    """同一文件但区间不同（offset 变了）→ 放行（分页前进是合法的）。"""
    loop = _mk_loop(tmp_path)
    from app.runtime.protocol import AgentStep
    act = AgentStep(thought="翻页看后面", tool="read_file",
                    args={"path": "big.py", "offset": 600, "limit": 15}, done=False)
    loop._prev_action = {"tool": "read_file", "args": {"path": "big.py", "offset": 590, "limit": 15}}
    assert loop._dup_action_block(act) == ""


def test_dup_read_diff_file_allowed(tmp_path):
    """换了个文件读 → 放行。"""
    loop = _mk_loop(tmp_path)
    from app.runtime.protocol import AgentStep
    act = AgentStep(thought="读另一个文件", tool="read_file",
                    args={"path": "other.py"}, done=False)
    loop._prev_action = {"tool": "read_file", "args": {"path": "big.py", "offset": 590, "limit": 15}}
    assert loop._dup_action_block(act) == ""


# ---------- read_file 分页（T4 第二次失败补的边界处理） ----------

def test_read_file_pagination_bounds(tmp_path):
    """分页边界：offset=0 clamp、越界提示、非整数提示、末尾锚点——都不抛错。"""
    from app.tools import registry
    (tmp_path / "big.py").write_text("\n".join(f"line{i}" for i in range(1, 601)))
    ws = tmp_path
    # offset=0 → clamp 到 1
    r = registry._read_file(ws, {"path": "big.py", "offset": 0, "limit": 3})
    assert r.ok and "[行 1-3" in r.output
    # 越界 → 提示不崩
    r = registry._read_file(ws, {"path": "big.py", "offset": 9999, "limit": 3})
    assert r.ok and "超出文件范围" in r.output
    # 非整数 → 提示不崩
    r = registry._read_file(ws, {"path": "big.py", "offset": "abc"})
    assert r.ok and "整数行号" in r.output
    # 末尾锚点
    r = registry._read_file(ws, {"path": "big.py", "offset": 598, "limit": 100})
    assert r.ok and "已到文件末尾" in r.output and "共 600 行" in r.output
    # 小文件全文直读不受影响（无截断提示）
    r = registry._read_file(ws, {"path": "big.py"})
    assert r.ok and "[行" not in r.output


# ---------- read_file 分页（T4 第二次失败补的边界处理） ----------

def test_read_file_pagination_bounds(tmp_path):
    """分页边界：offset=0 clamp、越界提示、非整数提示、末尾锚点——都不抛错。"""
    from app.tools import registry
    (tmp_path / "big.py").write_text("\n".join(f"line{i}" for i in range(1, 601)))
    ws = tmp_path
    # offset=0 → clamp 到 1
    r = registry._read_file(ws, {"path": "big.py", "offset": 0, "limit": 3})
    assert r.ok and "[行 1-3" in r.output
    # 越界 → 提示不崩
    r = registry._read_file(ws, {"path": "big.py", "offset": 9999, "limit": 3})
    assert r.ok and "超出文件范围" in r.output
    # 非整数 → 提示不崩
    r = registry._read_file(ws, {"path": "big.py", "offset": "abc"})
    assert r.ok and "整数行号" in r.output
    # 末尾锚点
    r = registry._read_file(ws, {"path": "big.py", "offset": 598, "limit": 100})
    assert r.ok and "已到文件末尾" in r.output and "共 600 行" in r.output
    # 小文件全文直读不受影响（无截断提示）
    r = registry._read_file(ws, {"path": "big.py"})
    assert r.ok and "[行" not in r.output


# ---------- search_file（T4 第四次失败补：真实库定位必须有搜索） ----------

def test_search_file_locates_definitions(tmp_path):
    """search_file 返回 文件:行号:内容，可定位大文件里的定义。"""
    from app.tools import registry
    (tmp_path / "big.py").write_text(
        "import os\n\nGITHUB_ESCAPE_RULES = {r\"|\": r\"\\|\"}\n\n"
        "def _pipe_line():\n    pass\n")
    r = registry._search_file(tmp_path, {"pattern": "GITHUB_ESCAPE_RULES", "path": "big.py"})
    assert r.ok and "big.py:3" in r.output and "GITHUB_ESCAPE_RULES" in r.output
    # 正则多匹配
    r = registry._search_file(tmp_path, {"pattern": "def |GITHUB"})
    assert r.ok and "big.py:3" in r.output and "big.py:5" in r.output
    # 无 path 全目录搜
    r = registry._search_file(tmp_path, {"pattern": "import os"})
    assert r.ok and "big.py:1" in r.output
    # 无匹配
    r = registry._search_file(tmp_path, {"pattern": "zzz_nope"})
    assert r.ok and "未找到匹配" in r.output
    # 空 pattern
    r = registry._search_file(tmp_path, {"pattern": ""})
    assert "不能为空" in r.output
