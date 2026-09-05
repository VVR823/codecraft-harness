"""Planner（M4 B1~B2）：任务规划器——advisory plan + agentic execution。

背景：v2.2 §4 承诺了 runtime/plan.py（任务拆 DAG…），但 M0~M3 没落地，README
架构树也悄悄删了这行——JD 关键词"任务规划器"接不上。M4 补上，设计定稿（选项 A，
控制在一个"不加戏"的度）：

- 两段式：plan phase（一次规划调用）+ execute phase（现有 agentic loop 原样复用）。
- 计划是**咨询性脚手架，不是硬状态机**——执行仍让 LLM 每步自选工具；发现计划与
  文件实际不符时可偏离（保住已打通的 self-repair 闭环，不引 LangGraph）。
- 计划注入初始 user 消息（goal 之后）→ 随 checkpoint 自动持久化 → resume 不重规划、
  不重付规划 token（与"压缩不损可恢复性"同理）。
- 计划单独落 plans 表（审计/回放口径）；planning tokens 由 loop 按 meter 前后差值
  记录，计入预算护栏（护栏不被规划调用绕过）。

P2 解析策略与 Q11 同构：Pydantic 强校验 + 重试 ≤PLAN_RETRY，仍失败 → 降级无计划
执行（advisory 语义：规划失败不能拖垮执行）。
"""
from __future__ import annotations

import json

from pydantic import BaseModel, Field, ValidationError

from .protocol import StepParseError, extract_json_object

# 坏 plan JSON 最多重试次数（P2；比执行决策少——规划是前置 0 成本调用，不值得反复烧）
PLAN_RETRY = 2


class PlanStep(BaseModel):
    id: str = Field(description="步骤编号（如 '1'）")
    intent: str = Field(description="这步要做什么（一句话）")
    files: list[str] = Field(default_factory=list,
                             description="涉及的文件（相对任务目录路径）")
    verification: str = Field(default="", description="怎么验证这步做对了")


class AgentPlan(BaseModel):
    objective: str = Field(description="对任务目标的理解/重述（一句话）")
    steps: list[PlanStep] = Field(description="执行步骤清单（按顺序）")


PLAN_PROMPT = """你是一个软件工程师 Agent 的任务规划器。先读任务目标，输出一段【计划 JSON】（不要多余文字），格式：
{"objective": "对任务的一句话理解", "steps": [{"id": "1", "intent": "这步做什么", "files": ["涉及的文件，相对任务目录路径"], "verification": "怎么验证这步做对了"}]}
- 步骤给 3~6 步即可，按"先侦察现状 → 再改代码 → 后跑测试验证"的顺序
- files 只列确实要读/改的文件；不知道确切路径就先写你猜的，执行阶段以实际为准
- 计划是参考不是硬性规定：执行时若发现与文件实际不符，可以偏离计划
- 只输出 JSON 对象本身，不要用 ``` 代码块包裹"""


def _coerce(raw: dict) -> dict:
    """轻量容错：LLM 常把空列表写成 null/漏字段，先归一化再交给 Pydantic。

    省一次重试比严格校验更划算——免费模型"files": null 是高频小毛病。
    """
    steps = raw.get("steps")
    if not isinstance(steps, list):
        raw["steps"] = []
    for i, s in enumerate(raw.get("steps") or [], 1):
        if not isinstance(s, dict):
            continue
        if s.get("files") is None:
            s["files"] = []
        if not isinstance(s.get("files"), list):
            s["files"] = []
        if s.get("verification") is None:
            s["verification"] = ""
        if s.get("id") is None:
            s["id"] = str(i)
    if raw.get("objective") is None:
        raw["objective"] = ""
    return raw


def parse_plan(text: str) -> AgentPlan:
    """把 planner 输出解析成 AgentPlan；失败抛 StepParseError（loop 触发重试）。

    容错两件套：① 容忍整体包一层 {"plan": {...}}；② _coerce 归一化 null 字段。
    """
    raw = extract_json_object(text)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise StepParseError(f"计划 JSON 解析失败: {e}\n原文: {raw[:200]}") from e
    if not isinstance(data, dict):
        raise StepParseError(f"计划 JSON 不是对象: {type(data).__name__}")
    if "plan" in data and isinstance(data["plan"], dict) and "objective" not in data:
        data = data["plan"]
    try:
        return AgentPlan(**_coerce(data))
    except ValidationError as e:
        raise StepParseError(f"计划字段校验失败: {e}") from e


def plan_to_text(plan: AgentPlan) -> str:
    """把计划序列化成注入消息的文本（放在任务目标之后）。"""
    lines = [f"【执行计划（参考；若与文件实际不符，按实际调整）】目标: {plan.objective}"]
    for i, s in enumerate(plan.steps, 1):
        files = (f"；涉及文件: {', '.join(s.files)}") if s.files else ""
        ver = f"；验证: {s.verification}" if s.verification else ""
        lines.append(f"{i}. {s.intent}{files}{ver}")
    return "\n".join(lines)
