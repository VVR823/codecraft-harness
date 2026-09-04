"""人工审批层（M2，底线 4 证据：审批记录落 approvals 表）。

MVP 无 UI、无人值守（CLI/批处理），审批策略 = "记录即审计，默认拒绝/默认要批准"：
- budget_continue：run 累计 token 超预算 → loop 暂停（status=budget_paused）并记 requested；
  只有 approve_budget()（模拟人工批准，真实系统里是 UI 按钮/API 调用）后才允许 resume。
- high_tool：registry 里 Perm.HIGH 的高危工具（工作区外 / 装包等不可逆操作），
  模型一请求就记 requested + denied，拒绝执行——不留任何可绕过口子，只留审计痕迹。

本模块是纯"记录 + 判定"，不持有状态；由 loop 在暂停/拦截点调用。
"""
from __future__ import annotations

from ..store import db

KIND_BUDGET = "budget_continue"
KIND_HIGH_TOOL = "high_tool"


# ---------------- budget_continue ----------------
def request_budget_continue(run_id: str, used_tokens: int, budget: int) -> None:
    """预算超限暂停时记录"请求放行"（人还没批）。"""
    db.append_approval(run_id, KIND_BUDGET, "requested",
                       f"累计 {used_tokens} token 超预算 {budget}")


def approve_budget_continue(run_id: str, note: str = "CLI 人工批准") -> None:
    """批准续跑。真实系统里这是 UI 按钮 / 审批 API 调用的落点；MVP demo 直接调。"""
    db.append_approval(run_id, KIND_BUDGET, "approved", note)


def is_budget_approved(run_id: str) -> bool:
    """该 run 最近一条预算审批是否为 approved（requested/denied 都算未批准）。"""
    row = db.last_approval(run_id, KIND_BUDGET)
    return bool(row and row["action"] == "approved")


# ---------------- high_tool ----------------
def deny_high_tool(run_id: str, tool: str, note: str = "") -> str:
    """HIGH 工具请求：记 requested + denied，返回喂回 loop 的拦截消息（不执行）。"""
    db.append_approval(run_id, KIND_HIGH_TOOL, "requested", f"工具 {tool} {note}")
    db.append_approval(run_id, KIND_HIGH_TOOL, "denied", f"工具 {tool} {note}")
    return (f"工具 {tool} 属高危操作（工作区外/不可逆），已拒绝执行并记录审计。"
            "请在任务包允许的动作范围内完成任务（read_file/edit_file/write_file/run_tests）。")
