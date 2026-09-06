"""M2 韧性三件套测试（MVP 底线 3/4）：
- 工作区漂移识别：checkpoint 的 ws_hash vs 当前工作区不一致 → resume 拒绝（底线3）
- 预算护栏：meter 超限 → budget_paused + 审批请求记录；批准后才可 resume（底线4）
- HIGH 工具审批：install_package 请求被拒 + 审计记录，run 不被拖垮
"""
import json
import sys
from pathlib import Path

import uuid

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.runtime.loop import HarnessLoop, LoopError  # noqa: E402
from app.store import db  # noqa: E402
from app.tools import approval  # noqa: E402

@pytest.fixture
def ws(tmp_path):
    """带一个可读 .py 的任务工作区（跑 Fake 决策脚本用）。"""
    (tmp_path / "utils.py").write_text(
        'def trim_whitespace(text: str) -> str:\n'
        '    return text.strip()\n',
        encoding="utf-8")
    return tmp_path


class FakeDecider:
    """按剧本出 JSON 决策：用完剧本就 done（防跑飞）。"""

    def __init__(self, script: list[str]):
        self.script = list(script)
        self.calls = 0

    def __call__(self, messages):
        self.calls += 1
        return self.script.pop(0) if self.script else '{"thought": "收尾", "done": true}'


def _read_step(path):
    return json.dumps({"thought": "读", "tool": "read_file", "args": {"path": path}}, ensure_ascii=False)


# ---------------- 底线 3：工作区漂移 ---------------

def test_resume_refuses_when_workspace_drifted(ws):
    """跑几步落 checkpoint → 改工作区文件 → resume 必须拒绝（ws_hash 不一致）。"""
    goal = "修好 utils.py"
    rid = "m2_" + uuid.uuid4().hex[:10]
    # 第一次 run：read_file 一步后 done
    d1 = FakeDecider([_read_step("utils.py")])
    HarnessLoop(ws, goal, d1, run_id=rid).run()
    cp = db.last_checkpoint(rid)
    assert cp is not None and cp["ws_hash"] != ""
    # 外部改动工作区（模拟 git restore / 手动编辑 → 上下文过期）
    (ws / "utils.py").write_text("def f():\n    return 2\n", encoding="utf-8")
    d2 = FakeDecider([])
    with pytest.raises(LoopError, match="漂移"):
        HarnessLoop(ws, goal, d2, run_id=rid).resume()
    # 漂移被拒后 run 状态标 failed（可审计）
    assert db.get_run(rid)["status"] == "failed"


def test_resume_ok_when_workspace_unchanged(ws):
    """工作区没动过 → resume 正常续跑（底线 2 的回归保护）。"""
    goal = "修好 utils.py"
    rid = "m2_" + uuid.uuid4().hex[:10]
    d1 = FakeDecider([_read_step("utils.py")])
    HarnessLoop(ws, goal, d1, run_id=rid).run()
    d2 = FakeDecider([])  # 没剧本 → 直接 done
    r = HarnessLoop(ws, goal, d2, run_id=rid).resume()
    assert r["status"] == "done"


# ---------------- 底线 4：预算护栏 ---------------

def _meter_decider(total: int, script: list[str]):
    """decider 每次调用把 meter 涨到 total（模拟 LLM 消耗），剧本走完即 done。"""
    class MeterDecider(FakeDecider):
        def __call__(self, messages):
            meter["tokens"] = total
            return super().__call__(messages)
    meter = {"tokens": 0}
    return MeterDecider(script), meter


def test_budget_overrun_pauses_and_needs_approval(ws):
    """meter 超预算 → 转 budget_paused（记 requested）；未批准 resume 被拒；批准后续跑成功。"""
    goal = "修好 utils.py"
    rid = "m2_" + uuid.uuid4().hex[:10]
    script = [_read_step("utils.py"), '{"thought": "收尾", "done": true}']
    decider, meter = _meter_decider(total=5_000, script=script)
    loop = HarnessLoop(ws, goal, decider, run_id=rid, token_budget=1_000, meter=meter)
    r = loop.run()
    assert r["status"] == "budget_paused"
    assert db.get_run(rid)["status"] == "budget_paused"
    # 审批记录：requested 落了，尚未 approved
    last = db.last_approval(rid, "budget_continue")
    assert last is not None and last["action"] == "requested"
    assert approval.is_budget_approved(rid) is False
    # 未批准 resume → 拒绝
    d2 = FakeDecider([])
    with pytest.raises(LoopError, match="批准"):
        HarnessLoop(ws, goal, d2, run_id=rid, token_budget=1_000, meter={"tokens": 5_000}).resume()
    # 人工批准 → 续跑成功（批准后预算不再拦截本轮）
    d3 = FakeDecider([])  # 无剧本 → 直接 done（没有写动作，放行）
    r3 = HarnessLoop(ws, goal, d3, run_id=rid, token_budget=1_000,
                     meter={"tokens": 5_000}).resume(approve_budget=True)
    assert r3["status"] == "done"
    assert approval.is_budget_approved(rid) is True


def test_budget_not_triggered_within_limit(ws):
    """预算内正常跑到 done，不产生审批请求。"""
    rid = "m2_" + uuid.uuid4().hex[:10]
    decider, meter = _meter_decider(total=500, script=[_read_step("utils.py")])
    r = HarnessLoop(ws, "goal", decider, run_id=rid, token_budget=1_000, meter=meter).run()
    assert r["status"] == "done"
    assert db.last_approval(rid, "budget_continue") is None


# ---------------- HIGH 工具审批 ----------------

def test_high_tool_request_denied_with_audit(ws):
    """模型请求 install_package（HIGH）→ 拒绝 + 审计，且不拖垮 run（可继续到 done）。"""
    rid = "m2_" + uuid.uuid4().hex[:10]
    script = [
        json.dumps({"thought": "装个包", "tool": "install_package",
                    "args": {"package": "numpy"}}, ensure_ascii=False),
        '{"thought": "收尾", "done": true}',
    ]
    r = HarnessLoop(ws, "goal", FakeDecider(script), run_id=rid).run()
    assert r["status"] == "done"          # 被拒后模型收尾，run 没 failed
    kinds = [(a["kind"], a["action"]) for a in db.list_approvals(rid)]
    assert ("high_tool", "requested") in kinds
    assert ("high_tool", "denied") in kinds
