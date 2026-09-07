"""drive_task 决策退化自动续跑判定（T4 run7/11 实证，2026-09-07）。

只测纯函数 _is_degraded_failure（不跑真 LLM）：解析/校验类失败才触发
run 级重试；工作区漂移等硬失败不触发；非 failed 不触发。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from scripts.drive_task import _is_degraded_failure  # noqa: E402


def test_degraded_validation_error_triggers():
    r = {"status": "failed", "reason": "字段校验失败: 1 validation error for AgentStep\nthought\n  Field required"}
    assert _is_degraded_failure(r) is True


def test_degraded_json_parse_triggers():
    assert _is_degraded_failure({"status": "failed", "reason": "JSON 解析失败: Expecting value"}) is True
    assert _is_degraded_failure({"status": "failed", "reason": "输出里找不到 JSON 对象"}) is True
    assert _is_degraded_failure({"status": "failed", "reason": "模型输出为空"}) is True


def test_hard_failures_do_not_trigger():
    # 工作区漂移等非解析类失败：续跑会拒绝或没意义，不触发
    assert _is_degraded_failure(
        {"status": "failed", "reason": "[工作区漂移] 拒绝续跑"}) is False
    # 非 failed 状态不触发
    assert _is_degraded_failure({"status": "done"}) is False
    assert _is_degraded_failure({"status": "paused", "reason": ""}) is False
    assert _is_degraded_failure({"status": "budget_paused"}) is False


# ---------- paused 自动续跑判定 + MAX_STEPS 段语义（T4 run12 实证，2026-09-07） ----------

def test_auto_resumable_paused_and_degraded():
    """_auto_resumable：决策退化 failed 与 MAX_STEPS 触顶 paused 都值得自动续跑。"""
    from scripts.drive_task import _auto_resumable
    assert _auto_resumable({"status": "failed", "reason": "字段校验失败: thought"}) is True
    assert _auto_resumable({"status": "paused", "steps": 30}) is True
    # 终态与预算暂停不自动续（预算需人工批准，resume 有审批门）
    assert _auto_resumable({"status": "done"}) is False
    assert _auto_resumable({"status": "budget_paused"}) is False
    assert _auto_resumable({"status": "failed", "reason": "[工作区漂移] 拒绝续跑"}) is False


def test_max_steps_is_per_segment(monkeypatch, tmp_path):
    """段语义：run() 一段触顶 paused 后，resume() 获得全新配额继续跑。

    T4 run12 实证（旧绝对语义 bug）：run 在 30 步 paused 后 resume，step 从 30 起算，
    `while step < 30` 立即退出 → 一步都续不了。改段语义后每段（run/每次 resume）
    最多 MAX_STEPS 步。
    """
    import app.runtime.loop as loop_mod
    from app.runtime.loop import HarnessLoop
    from app.store import db
    monkeypatch.setattr(loop_mod, "MAX_STEPS", 3)
    # 600 行文件：乱序 offset 精读不会撞 dup/翻页护栏（护栏会让步不落 trace）
    (tmp_path / "x.py").write_text(
        "\n".join(f"line{i:03d} = {i}" for i in range(1, 601)), encoding="utf-8")

    state = {"n": 0}

    def decider(messages):
        # decider 契约：返回 JSON 文本，loop 内 parse_step 解析
        state["n"] += 1
        off = (state["n"] * 37) % 500 + 1   # 伪随机 offset：异区间不重复、非接续不翻页
        return ('{"thought": "侦察", "tool": "read_file",'
                f' "args": {{"path": "x.py", "offset": {off}, "limit": 5}},'
                ' "done": false}')

    loop = HarnessLoop(tmp_path, "测试目标", decider=decider)
    r1 = loop.run()
    assert r1["status"] == "paused" and r1["steps"] == 3   # 段1：step 1-3
    r2 = loop.resume()
    assert r2["status"] == "paused" and r2["steps"] == 6   # 段2：step 4-6 真跑了
    # 过滤空转雷达的观测 trace（tool 为空，非真实动作）后，6 步全执行
    traced = sorted(t["step"] for t in db.list_traces(loop.run_id) if t["tool"])
    assert traced == [1, 2, 3, 4, 5, 6]                     # 6 个动作都执行了

