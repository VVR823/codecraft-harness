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
