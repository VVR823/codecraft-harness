"""M4 planner 测试（B1 骨架 + B2 韧性）：
① plan phase 先于 execute：规划只调一次、计划注入执行上下文、执行剧本照跑
② 坏 plan JSON → 重试 ≤PLAN_RETRY → 成功；重试耗尽 → 降级无计划执行（不崩）
③ resume 不重规划：plans 表仅 1 行（正常 resume / 杀在 plan phase 两个场景）
④ planning tokens 计入预算护栏：规划耗的 token 参与超限判定（护栏不被绕过）
"""
import json
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.runtime.loop import HarnessLoop  # noqa: E402
from app.runtime.plan import PLAN_PROMPT, PLAN_RETRY  # noqa: E402
from app.store import db  # noqa: E402

pytestmark = pytest.mark.usefixtures("db_ready")

GOOD_PLAN = json.dumps(
    {"objective": "修好 utils.py 的 trim 函数",
     "steps": [{"id": "1", "intent": "读文件看现状", "files": ["utils.py"],
                "verification": "确认 bug 位置"},
               {"id": "2", "intent": "修复并跑测试", "files": ["utils.py"],
                "verification": "pytest 全绿"}]},
    ensure_ascii=False)


@pytest.fixture(autouse=True)
def db_ready():
    db.init_db()


@pytest.fixture
def ws(tmp_path):
    """带一个可读 .py 的任务工作区（跑 Fake 决策脚本用）。"""
    (tmp_path / "utils.py").write_text(
        'def trim_whitespace(text: str) -> str:\n'
        '    return text.strip()\n',
        encoding="utf-8")
    return tmp_path


class PlannerFake:
    """区分规划/执行调用的 Fake decider。

    - 规划调用（system == PLAN_PROMPT）：从 plan_outputs 弹一段文本返回
    - 执行调用：从 script 弹 JSON 决策；剧本用尽 → done（防跑飞）
    meter_plan_add / meter_exec_add：每次对应调用把 meter["tokens"] 加上该值
    （模拟 LLM 真实消耗，预算用例用；None=不动 meter）。
    """

    def __init__(self, plan_outputs: list[str], script: list[str],
                 meter: dict | None = None,
                 meter_plan_add: int = 0, meter_exec_add: int = 0):
        self.plan_outputs = list(plan_outputs)
        self.script = list(script)
        self.meter = meter
        self.meter_plan_add = meter_plan_add
        self.meter_exec_add = meter_exec_add
        self.plan_calls = 0
        self.exec_calls = 0
        self.seen_exec_messages: list[list[dict]] = []

    def __call__(self, messages):
        is_plan = bool(messages) and messages[0].get("role") == "system" \
            and messages[0].get("content") == PLAN_PROMPT
        if is_plan:
            self.plan_calls += 1
            if self.meter is not None and self.meter_plan_add:
                self.meter["tokens"] = self.meter.get("tokens", 0) + self.meter_plan_add
            return self.plan_outputs.pop(0) if self.plan_outputs else "不是合法 JSON"
        self.exec_calls += 1
        self.seen_exec_messages.append(messages)
        if self.meter is not None and self.meter_exec_add:
            self.meter["tokens"] = self.meter.get("tokens", 0) + self.meter_exec_add
        return self.script.pop(0) if self.script \
            else '{"thought": "收尾", "done": true}'


def _read_step(path="utils.py"):
    return json.dumps({"thought": "读", "tool": "read_file",
                       "args": {"path": path}}, ensure_ascii=False)


def _plan_row(run_id):
    row = db.get_plan(run_id)
    return row


# ---------------- ① 正常规划：plan 先于 execute ----------------

def test_plan_runs_once_before_execute(ws):
    rid = "m4p1_" + uuid.uuid4().hex[:8]
    fake = PlannerFake(plan_outputs=[GOOD_PLAN],
                       script=[_read_step(), '{"thought": "收尾", "done": true}'])
    r = HarnessLoop(ws, "修好 utils.py", fake, run_id=rid, use_plan=True).run()
    assert r["status"] == "done"
    # planner 只调 1 次；执行剧本照跑 2 次
    assert fake.plan_calls == 1
    assert fake.exec_calls == 2
    # 计划落库（审计/回放）：status=ok，plan_json 可解析出 objective
    row = _plan_row(rid)
    assert row is not None and row["status"] == "ok"
    plan = json.loads(row["plan_json"])
    assert plan["objective"] == "修好 utils.py 的 trim 函数"
    # 计划注入执行上下文：decider 收到的第 2 条（goal user）含计划文本
    injected = fake.seen_exec_messages[0][1]["content"]
    assert "执行计划" in injected and "读文件看现状" in injected
    # checkpoint 持久化：ctx_messages 里也有计划（resume 不丢）
    cp = db.last_checkpoint(rid)
    assert "执行计划" in json.loads(cp["ctx_messages"])[1]["content"]


def test_plan_off_by_default_keeps_old_behavior(ws):
    """use_plan 默认 False：不开规划，跟 M3 及以前行为一致（回归保护）。"""
    rid = "m4p0_" + uuid.uuid4().hex[:8]
    fake = PlannerFake(plan_outputs=[GOOD_PLAN],
                       script=['{"thought": "收尾", "done": true}'])
    r = HarnessLoop(ws, "修好 utils.py", fake, run_id=rid).run()
    assert r["status"] == "done"
    assert fake.plan_calls == 0          # 没开规划
    assert db.get_plan(rid) is None      # plans 表无行


# ---------------- ② 坏 plan：重试 → 成功；耗尽 → 降级 ----------------

def test_bad_plan_retries_then_succeeds(ws):
    rid = "m4p2a_" + uuid.uuid4().hex[:8]
    good_late = json.dumps({"objective": "迟到的好计划", "steps": []},
                           ensure_ascii=False)
    fake = PlannerFake(plan_outputs=["完全不是 JSON", "也坏", good_late],
                       script=['{"thought": "收尾", "done": true}'])
    r = HarnessLoop(ws, "修好 utils.py", fake, run_id=rid, use_plan=True).run()
    assert r["status"] == "done"
    # 初始 1 次 + 坏 JSON 重试 ≤PLAN_RETRY 次 → 共尝试 3 次后成功
    assert fake.plan_calls == PLAN_RETRY + 1
    assert _plan_row(rid)["status"] == "ok"


def test_bad_plan_exhausted_falls_back_without_crash(ws):
    """规划 JSON 一直坏 → 重试耗尽 → 降级无计划执行，run 照常 done 不崩。"""
    rid = "m4p2b_" + uuid.uuid4().hex[:8]
    fake = PlannerFake(plan_outputs=["坏"] * 10,
                       script=[_read_step(), '{"thought": "收尾", "done": true}'])
    r = HarnessLoop(ws, "修好 utils.py", fake, run_id=rid, use_plan=True).run()
    assert r["status"] == "done"
    assert fake.plan_calls == PLAN_RETRY + 1   # 3 次尝试后放弃
    assert fake.exec_calls == 2                # 执行不受影响
    row = _plan_row(rid)
    assert row is not None and row["status"] == "failed"  # 审计：降级有记录
    # 执行上下文里没有计划注入
    assert "执行计划" not in fake.seen_exec_messages[0][1]["content"]


# ---------------- ③ resume 不重规划 ----------------

def test_resume_does_not_replan_normal_path(ws):
    """正常 resume（有 checkpoint）：plan 已在 ctx_messages，不重规划。"""
    rid = "m4p3a_" + uuid.uuid4().hex[:8]
    fake1 = PlannerFake(plan_outputs=[GOOD_PLAN],
                        script=[_read_step(), '{"thought": "收尾", "done": true}'])
    assert HarnessLoop(ws, "修好 utils.py", fake1, run_id=rid,
                       use_plan=True).run()["status"] == "done"
    # resume：新 decider 不给 plan（若重规划会因取不到而返回坏 JSON → 也可观察 plan_calls）
    fake2 = PlannerFake(plan_outputs=[], script=['{"thought": "收尾", "done": true}'])
    r2 = HarnessLoop(ws, "修好 utils.py", fake2, run_id=rid, use_plan=True).resume()
    assert r2["status"] == "done"
    assert fake2.plan_calls == 0                 # 不重规划
    # plans 表仍是初始那次（status=ok、plan_json 原样）——若 resume 重规划会
    # save_plan 覆盖该行（此 fake 给坏 JSON → 覆盖成 failed），断言原样即证明没重写
    row = _plan_row(rid)
    assert row is not None and row["status"] == "ok"
    assert json.loads(row["plan_json"]) == json.loads(GOOD_PLAN)


def test_resume_after_kill_in_plan_phase_does_not_replan(ws):
    """杀在 plan phase（plan 已落库、0 步执行、无 checkpoint）→ resume 不重规划续跑。

    依赖 run() 幂等（run 行已存在时复用不重建）——否则 resume 走 cp=None→run()
    会 INSERT 主键冲突。这是 planner 引入前就潜伏、由"防杀在 plan phase"暴露的修复。
    """
    rid = "m4p3b_" + uuid.uuid4().hex[:8]
    db.create_run(rid, "utils", "修好 utils.py")
    db.save_plan(rid, GOOD_PLAN, planning_tokens=120, status="ok")  # 模拟被杀前已规划
    fake = PlannerFake(plan_outputs=[],  # 若重规划会返回坏 JSON
                       script=[_read_step(), '{"thought": "收尾", "done": true}'])
    r = HarnessLoop(ws, "修好 utils.py", fake, run_id=rid, use_plan=True).resume()
    assert r["status"] == "done"
    assert fake.plan_calls == 0          # 没重规划
    row = _plan_row(rid)
    assert json.loads(row["plan_json"]) == json.loads(GOOD_PLAN)  # 原计划原样保留
    assert db.get_run(rid)["status"] == "done"


# ---------------- ④ planning tokens 计入预算护栏 ----------------

def test_planning_tokens_count_toward_budget(ws):
    """规划耗 900 + 执行耗 600 > 预算 1000 → budget_paused（若规划不计入则不会超限）。"""
    rid = "m4p4_" + uuid.uuid4().hex[:8]
    meter = {"tokens": 0}
    fake = PlannerFake(plan_outputs=[GOOD_PLAN],
                       script=[_read_step(), '{"thought": "收尾", "done": true}'],
                       meter=meter, meter_plan_add=900, meter_exec_add=600)
    r = HarnessLoop(ws, "修好 utils.py", fake, run_id=rid, use_plan=True,
                    token_budget=1_000, meter=meter).run()
    assert r["status"] == "budget_paused"
    assert db.get_run(rid)["status"] == "budget_paused"
    # 规划 token 已计入 meter 且落 plans 表（审计：这次规划花了多少）
    assert meter["tokens"] >= 1_000
    assert _plan_row(rid)["planning_tokens"] == 900
    last = db.last_approval(rid, "budget_continue")
    assert last is not None and last["action"] == "requested"
