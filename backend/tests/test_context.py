"""M3 上下文分层压缩测试（Q14 / 数字③）。

覆盖：近 N 步全文保留、旧步压成一行摘要、开关视图等价性、压缩率>0。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.runtime.context import (  # noqa: E402
    build_view,
    estimate_tokens,
    view_stats,
)


def _sys_and_goal():
    return [{"role": "system", "content": "你是一个工程师 Agent（长提示，占位占位占位）"},
            {"role": "user", "content": "任务目标：修好 utils.py（目标描述占位）"}]


def _step(n: int, body: str = "内容占位" * 40):
    """第 n 步的 assistant(调用，含决策说明) + user(长工具结果) 两条消息——贴近真实形态。"""
    return [{"role": "assistant",
             "content": f"调用 read_file utils.py 看现状（第{n}步的思考与决策说明占位，"
                        f"模拟真实里模型输出的完整 thought 与参数摘要，较长以便压缩生效 {body[:100]}）"},
            {"role": "user", "content": f"[工具结果 read_file] {body}"}]


def _history(n_steps: int):
    msgs = _sys_and_goal()
    for i in range(1, n_steps + 1):
        msgs += _step(i)
    return msgs


def test_short_history_unchanged():
    """步数 <= 保留上限 → 视图与原始一致（压缩不该动短会话）。"""
    msgs = _sys_and_goal() + _step(1)
    assert build_view(msgs) == msgs


def test_recent_steps_kept_full():
    """近 3 步（assistant+user 对）必须全文在视图里，原文一字不差。"""
    msgs = _history(5)  # system + goal + 5 步
    view = build_view(msgs, keep_recent_steps=3)
    # 最后 6 条（近 3 步）逐字保留
    assert view[-6:] == msgs[-6:]
    # system 与 goal 在头部全文保留
    assert view[0] == msgs[0] and view[1] == msgs[1]


def test_old_steps_summarized_into_one_message():
    """第 1~2 步（旧）被压成摘要，不再以原始长消息出现。"""
    msgs = _history(5)
    view = build_view(msgs, keep_recent_steps=3)
    # 旧步原始消息（msgs[2:4] 即第 1 步两条）不应原样出现在视图里
    raw = msgs[2]  # 第 1 步 assistant
    assert all(m != raw for m in view)
    # 视图结构 = head(2) + 摘要(1) + recent(6) = 9 条
    assert len(view) == 9
    summary = view[2]
    assert summary["role"] == "user" and "已压缩" in summary["content"]
    assert "工具结果 read_file" in summary["content"]  # 摘要里保留了动作信息
    # 旧步两条消息总字符数 > 摘要总字符数（真实压缩；覆盖表头开销）
    old_total = len(msgs[2]["content"]) + len(msgs[3]["content"])
    assert len(summary["content"]) < old_total


def test_compression_rate_positive_and_toggle_eq():
    """压缩率 >0（本地估算）；keep_recent_steps 巨大 ≈ 全量视图。"""
    msgs = _history(8)  # 让旧步骤足够多，压缩才有意义
    stats = view_stats(msgs, keep_recent_steps=3)
    assert stats["rate"] > 0.15
    assert stats["n_view"] < stats["n_full"]
    # 开关等价性：全量保留档的输出 == 原始消息
    assert build_view(msgs, keep_recent_steps=10**9) == msgs
    # 0 = 全压缩（旧步全变摘要），但 system/goal 仍在
    v0 = build_view(msgs, keep_recent_steps=0)
    assert v0[0] == msgs[0] and v0[1] == msgs[1]
    assert len(v0) < len(msgs)


def test_estimate_tokens_basic():
    assert estimate_tokens("") == 1
    assert estimate_tokens("abc") == 2   # ceil(3/2)
    assert estimate_tokens("abcdef") == 3
