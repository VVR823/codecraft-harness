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
    assert loop._stall_warned_at == 0


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
    assert loop._stall_warned_at == 3


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
    assert loop._stall_warned_at == 3


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


# ---------- 顺序翻页护栏（T4 run7 实证补：逐页通读大文件 → 上下文臃肿 → JSON 崩坏） ----------

def _read_act(path: str, offset: int, limit: int = 100):
    from app.runtime.protocol import AgentStep
    return AgentStep(thought="翻页", tool="read_file",
                     args={"path": path, "offset": offset, "limit": limit}, done=False)


def test_page_walk_blocked_after_3_sequential(tmp_path):
    """同文件连续顺序翻 3 页仍只读 → 第 3 页拦截，提示 search_file。"""
    loop = _mk_loop(tmp_path)
    assert loop._page_walk_block(_read_act("big.py", 1)) == ""     # 页1
    assert loop._page_walk_block(_read_act("big.py", 101)) == ""   # 页2
    msg = loop._page_walk_block(_read_act("big.py", 201))          # 页3
    assert msg and "search_file" in msg and "big.py" in msg
    assert loop._page_walk["big.py"]["consec"] == 0  # 软墙：拦后清零可续翻


def test_page_walk_soft_wall_allows_continue(tmp_path):
    """软墙语义：拦一次后模型若仍坚持通读，下一页放行（非死墙）。"""
    loop = _mk_loop(tmp_path)
    loop._page_walk_block(_read_act("big.py", 1))
    loop._page_walk_block(_read_act("big.py", 101))
    loop._page_walk_block(_read_act("big.py", 201))  # 拦
    assert loop._page_walk_block(_read_act("big.py", 301)) == ""   # 续翻放行
    assert loop._page_walk_block(_read_act("big.py", 401)) == ""   # 再翻也放行（从0重计）
    assert loop._page_walk_block(_read_act("big.py", 501)) != ""   # 又满3页 → 再拦


def test_page_walk_non_sequential_resets(tmp_path):
    """非接续翻页（跳读/折返）不累计：offset 不接上页末尾就重新计段。"""
    loop = _mk_loop(tmp_path)
    loop._page_walk_block(_read_act("big.py", 1))
    loop._page_walk_block(_read_act("big.py", 101))
    loop._page_walk_block(_read_act("big.py", 50))   # 折返 → 新段（末行 149）
    assert loop._page_walk["big.py"]["consec"] == 1
    assert loop._page_walk_block(_read_act("big.py", 150)) == ""   # 新段第2页（接 149）
    msg = loop._page_walk_block(_read_act("big.py", 250))          # 新段第3页 → 拦
    assert msg and "search_file" in msg


def test_page_walk_full_read_and_other_tools_ignored(tmp_path):
    """无 limit 的全文读、非 read_file 动作 → 不计数不拦。"""
    loop = _mk_loop(tmp_path)
    from app.runtime.protocol import AgentStep
    full = AgentStep(thought="读全文", tool="read_file",
                     args={"path": "big.py"}, done=False)
    assert loop._page_walk_block(full) == ""          # 全文读不管
    assert "big.py" not in loop._page_walk
    edit = AgentStep(thought="改代码", tool="edit_file",
                     args={"path": "big.py", "old": "a", "new": "b"}, done=False)
    assert loop._page_walk_block(edit) == ""          # 非 read_file 不管
    done = AgentStep(thought="完成", done=True)
    assert loop._page_walk_block(done) == ""


# ---------- 工具结果消息不二次截断（T4 run8 实证：2000 字符消息层砍掉页尾目标测试） ----------

def test_read_result_message_not_double_truncated(tmp_path):
    """消息层截断上限 = registry 输出上限：工具给了多少内容，模型就能看到多少。

    T4 run8 实证：页读 500-599 行输出 3682 字符（含第 596 行的目标测试），消息层
    2000 字符二次截断把它砍半——目标测试永远看不到，模型只能反复换读法空转 15 步。
    """
    from app.runtime.protocol import AgentStep
    # ~4200 字符文件：超过旧 2000 上限、低于 registry 6000 上限 → 工具全量给出
    body = "\n".join(f"line{i:04d} " + "x" * 40 for i in range(1, 90))
    (tmp_path / "medium.py").write_text(body, encoding="utf-8")
    loop = _mk_loop(tmp_path)
    act = AgentStep(thought="读文件", tool="read_file",
                    args={"path": "medium.py"}, done=False)
    loop._execute(act, 1)
    msg = loop.messages[-1]["content"]
    assert "line0089" in msg    # 文件尾部内容可见（未被砍）
    assert "截断" not in msg     # 消息层没有二次截断标记


# ---------- 工具结果消息不二次截断（T4 run8 实证：2000 字符消息层砍掉页尾目标测试） ----------

def test_read_result_message_not_double_truncated(tmp_path):
    """消息层截断上限 = registry 输出上限：工具给了多少内容，模型就能看到多少。

    T4 run8 实证：页读 500-599 行输出 3682 字符（含第 596 行的目标测试），消息层
    2000 字符二次截断把它砍半——目标测试永远看不到，模型只能反复换读法空转 15 步。
    """
    from app.runtime.protocol import AgentStep
    # ~4200 字符文件：超过旧 2000 上限、低于 registry 6000 上限 → 工具全量给出
    body = "\n".join(f"line{i:04d} " + "x" * 40 for i in range(1, 90))
    (tmp_path / "medium.py").write_text(body, encoding="utf-8")
    loop = _mk_loop(tmp_path)
    act = AgentStep(thought="读文件", tool="read_file",
                    args={"path": "medium.py"}, done=False)
    loop._execute(act, 1)
    msg = loop.messages[-1]["content"]
    assert "line0089" in msg    # 文件尾部内容可见（未被砍）
    assert "截断" not in msg     # 消息层没有二次截断标记


# ---------- 分页自动收缩（T4 run10 实证：页尾拦腰截断 → 模型折返重读/乱序跳读） ----------

def test_page_read_auto_shrinks_long_pages(tmp_path):
    """请求 100 行但超 6000 字符 → 只返回放得下的完整行 + 明确的下页 offset，不拦腰截断。"""
    from app.tools import registry
    body = "\n".join(f"longline{i:04d} " + "x" * 90 for i in range(1, 101))  # 100 行 × ~100 字符 ≈ 10KB
    (tmp_path / "big.py").write_text(body, encoding="utf-8")
    r = registry._read_file(tmp_path, {"path": "big.py", "offset": 1, "limit": 100})
    assert r.ok
    out = r.output
    assert "已自动收缩" in out                      # 有收缩说明
    assert "offset=" in out                         # 给了下页起点
    assert "行 1-" in out and "共 100 行" in out
    # 输出里不该有旧式拦腰截断标记；页尾是完整行
    assert "\n...[截断" not in out
    assert out.rstrip().endswith("longline" + "x" * 90) or "（注" in out
    # 收缩后的行数与说明一致：页尾完整行 = 该页最后一行本身
    shown_end = int(out.split("[行 1-")[1].split(" ")[0])
    assert 1 <= shown_end < 100
    assert f"longline{shown_end:04d} " in out       # 显示的最后一整行内容在
    assert f"longline{shown_end + 1:04d}" not in out  # 未显示的下一行不在（无残行）


def test_page_read_small_pages_unchanged(tmp_path):
    """小页不受影响：无收缩说明，行为与旧实现一致。"""
    from app.tools import registry
    body = "\n".join(f"line{i}" for i in range(1, 601))
    (tmp_path / "big.py").write_text(body, encoding="utf-8")
    r = registry._read_file(tmp_path, {"path": "big.py", "offset": 1, "limit": 100})
    assert r.ok and "[行 1-100" in r.output and "已自动收缩" not in r.output
    # 末尾锚点语义保留
    r = registry._read_file(tmp_path, {"path": "big.py", "offset": 598, "limit": 100})
    assert r.ok and "已到文件末尾" in r.output and "共 600 行" in r.output


# ---------- 分页自动收缩（T4 run10 实证：页尾拦腰截断 → 模型折返重读/乱序跳读） ----------

def test_page_read_auto_shrinks_long_pages(tmp_path):
    """请求 100 行但超 6000 字符 → 只返回放得下的完整行 + 明确的下页 offset，不拦腰截断。"""
    from app.tools import registry
    body = "\n".join(f"longline{i:04d} " + "x" * 90 for i in range(1, 101))  # 100 行 × ~100 字符 ≈ 10KB
    (tmp_path / "big.py").write_text(body, encoding="utf-8")
    r = registry._read_file(tmp_path, {"path": "big.py", "offset": 1, "limit": 100})
    assert r.ok
    out = r.output
    assert "已自动收缩" in out                      # 有收缩说明
    assert "offset=" in out                         # 给了下页起点
    assert "行 1-" in out and "共 100 行" in out
    # 输出里不该有旧式拦腰截断标记；页尾是完整行
    assert "\n...[截断" not in out
    assert out.rstrip().endswith("longline" + "x" * 90) or "（注" in out
    # 收缩后的行数与说明一致：页尾完整行 = 该页最后一行本身
    shown_end = int(out.split("[行 1-")[1].split(" ")[0])
    assert 1 <= shown_end < 100
    assert f"longline{shown_end:04d} " in out       # 显示的最后一整行内容在
    assert f"longline{shown_end + 1:04d}" not in out  # 未显示的下一行不在（无残行）


def test_page_read_small_pages_unchanged(tmp_path):
    """小页不受影响：无收缩说明，行为与旧实现一致。"""
    from app.tools import registry
    body = "\n".join(f"line{i}" for i in range(1, 601))
    (tmp_path / "big.py").write_text(body, encoding="utf-8")
    r = registry._read_file(tmp_path, {"path": "big.py", "offset": 1, "limit": 100})
    assert r.ok and "[行 1-100" in r.output and "已自动收缩" not in r.output
    # 末尾锚点语义保留
    r = registry._read_file(tmp_path, {"path": "big.py", "offset": 598, "limit": 100})
    assert r.ok and "已到文件末尾" in r.output and "共 600 行" in r.output


# ---------- search_file 协议白名单（T4 run7~10 真根因：registry 注册了但协议枚举漏加） ----------

def test_protocol_allows_search_file():
    """AgentStep 校验放行 search_file——registry 有 handler、prompt 在教，协议必须开门。

    T4 run7~10 四连实证：search_file 自 2026-09-06 注册进 registry + system prompt
    教了用法 + 截断提示/翻页护栏都在喊'用 search_file'，但 protocol.Tool 枚举漏加
    → 模型每次调 search_file 都被'未知工具'打回，被迫退回逐页 read_file 通读大文件。
    """
    from app.runtime.protocol import AgentStep
    act = AgentStep(thought="定位目标测试", tool="search_file",
                    args={"pattern": "github_escape", "path": "test/test_regression.py"},
                    done=False)
    assert act.tool_name == "search_file"
    # 未知工具仍被拒（白名单没被放宽成自由串）
    import pytest
    with pytest.raises(Exception):
        AgentStep(thought="幻觉工具", tool="delete_all_files", done=False)


def test_protocol_parse_step_with_search_file():
    """parse_step 全链路：模型输出 search_file JSON 决策 → 解析成功。"""
    from app.runtime.protocol import parse_step
    text = '{"thought": "搜一下", "tool": "search_file", "args": {"pattern": "def _pipe"}, "done": false}'
    act = parse_step(text)
    assert act.tool_name == "search_file" and act.args["pattern"] == "def _pipe"


# ---------- search_file 协议白名单（T4 run7~10 真根因：registry 注册了但协议枚举漏加） ----------

def test_protocol_allows_search_file():
    """AgentStep 校验放行 search_file——registry 有 handler、prompt 在教，协议必须开门。

    T4 run7~10 四连实证：search_file 自 2026-09-06 注册进 registry + system prompt
    教了用法 + 截断提示/翻页护栏都在喊'用 search_file'，但 protocol.Tool 枚举漏加
    → 模型每次调 search_file 都被'未知工具'打回，被迫退回逐页 read_file 通读大文件。
    """
    from app.runtime.protocol import AgentStep
    act = AgentStep(thought="定位目标测试", tool="search_file",
                    args={"pattern": "github_escape", "path": "test/test_regression.py"},
                    done=False)
    assert act.tool_name == "search_file"
    # 未知工具仍被拒（白名单没被放宽成自由串）
    import pytest
    with pytest.raises(Exception):
        AgentStep(thought="幻觉工具", tool="delete_all_files", done=False)


def test_protocol_parse_step_with_search_file():
    """parse_step 全链路：模型输出 search_file JSON 决策 → 解析成功。"""
    from app.runtime.protocol import parse_step
    text = '{"thought": "搜一下", "tool": "search_file", "args": {"pattern": "def _pipe"}, "done": false}'
    act = parse_step(text)
    assert act.tool_name == "search_file" and act.args["pattern"] == "def _pipe"


# ---------- 周期式空转提醒 + search_file 重复拦截（T4 run12/14 实证补，2026-09-07） ----------

def test_stall_repeats_periodically(tmp_path):
    """长空转周期提醒：连续 8 个 read，第 3/6 个后各提醒一次（不再整段只一次）。"""
    loop = _mk_loop(tmp_path)
    loop.done_actions = _reads(3)
    loop._stall_radar()
    assert len(_stall_traces(loop)) == 1   # 第 3 个后首次提醒
    loop.done_actions = _reads(6)
    loop._stall_radar()
    assert len(_stall_traces(loop)) == 2   # 再满 3 步 → 再次提醒（run14 14 步空搜场景）
    loop.done_actions = _reads(8)
    loop._stall_radar()
    assert len(_stall_traces(loop)) == 2   # 8-6=2 < 3，还不到下次提醒
    assert loop._stall_warned_at == 6


def test_dup_search_same_args_blocked(tmp_path):
    """连续两次 search_file 完全同参数 → 拦截（run14 空 search {} 连发 16 步实证）。"""
    loop = _mk_loop(tmp_path)
    from app.runtime.protocol import AgentStep
    act = AgentStep(thought="再搜一次", tool="search_file",
                    args={"pattern": ""}, done=False)
    loop._prev_action = {"tool": "search_file", "args": {"pattern": ""}}
    msg = loop._dup_action_block(act)
    assert msg and "search_file" in msg and "pattern" in msg


def test_dup_search_diff_pattern_allowed(tmp_path):
    """异 pattern search 放行（搜索是有效的定位手段，只拦原地重复）。"""
    loop = _mk_loop(tmp_path)
    from app.runtime.protocol import AgentStep
    loop._prev_action = {"tool": "search_file", "args": {"pattern": "def _build_row"}}
    act = AgentStep(thought="换词搜", tool="search_file",
                    args={"pattern": "DataRow"}, done=False)
    assert loop._dup_action_block(act) == ""
