"""agent 决策协议（Q11）：LLM 每步输出一段 JSON，我们校验后执行。

协议：{"thought": str, "tool": "read_file|edit_file|write_file|run_tests", "args": {...}, "done": bool}
- done=True 时本轮结束（tool/args 可空）。
- 解析策略：允许模型包 ```json 代码块，剥掉后取最外层 {...}；解析/校验失败抛 StepParseError，
  由 loop 触发重试（最多 LLM_RETRY 次，Q11）。
- Day5+ 演进：新增 edit_file（旧→新片段替换）——改已有代码时模型只需输出改动片段，
  不用整文件 JSON 转义（免费模型写长 content 的翻车点）。
"""
from __future__ import annotations

import json
import re
from enum import Enum

from pydantic import BaseModel, Field, ValidationError, field_validator


class Tool(str, Enum):
    read_file = "read_file"
    edit_file = "edit_file"
    write_file = "write_file"
    run_tests = "run_tests"
    install_package = "install_package"   # M2：HIGH 权限占位，请求即审批拒绝+审计


MCP_PREFIX = "mcp_"   # M5-B2：MCP 动态工具命名空间前缀（mcp_<server>__<tool>）


class AgentStep(BaseModel):
    thought: str = Field(description="这步在想什么")
    tool: Tool | str | None = Field(default=None, description="要调的工具；done=True 时可为空")
    args: dict = Field(default_factory=dict, description="工具参数")
    done: bool = Field(default=False, description="True=认为任务已完成")

    @field_validator("tool")
    @classmethod
    def _tool_must_be_known_or_mcp(cls, v):
        """工具白名单：内置枚举 或 MCP 前缀动态工具（M5-B2）。

        内置工具仍走 Tool 枚举强校验（防模型幻觉乱拼名字）；
        MCP 工具是运行时从 server 拉取注册的，枚举无法静态穷举，
        放行 `mcp_` 前缀（执行时 registry.get_tool 才是最终白名单，
        拉不到/没注册照样报"未知工具"）。
        """
        if v is None:
            return v
        if isinstance(v, str) and v.startswith(MCP_PREFIX):
            return v
        try:
            return Tool(v)
        except ValueError:
            raise ValueError(
                f"未知工具: {v!r}。可用: {[t.value for t in Tool]} 或 MCP 工具(mcp_ 前缀)") from None

    @property
    def tool_name(self) -> str | None:
        """工具名的统一字符串形态（枚举 .value 或 mcp_ 原串）——loop 层用它分发。"""
        t = self.tool
        if t is None:
            return None
        return t.value if isinstance(t, Tool) else str(t)


class StepParseError(Exception):
    """模型输出不是合法/可校验的 JSON 决策。"""


_JSON_BLOCK = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)
_FIRST_OBJECT = re.compile(r"(\{.*\})", re.S)   # 与 _JSON_BLOCK 一致带捕获组


def extract_json_object(text: str) -> str:
    """从模型输出里剥出最外层 {...} 的 JSON 文本；找不到抛 StepParseError。

    供 parse_step（每步决策）与 parse_plan（M4 planner 计划）共用——两种解析
    都容忍模型包 ```json 代码块 / 前后夹带自然语言。
    """
    if not text or not text.strip():
        raise StepParseError("模型输出为空")
    m = _JSON_BLOCK.search(text) or _FIRST_OBJECT.search(text)
    if not m:
        raise StepParseError(f"输出里找不到 JSON 对象: {text[:120]!r}")
    return m.group(1)


def parse_step(text: str) -> AgentStep:
    """把模型输出解析成 AgentStep；失败抛 StepParseError。"""
    raw = extract_json_object(text)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise StepParseError(f"JSON 解析失败: {e}\n原文: {raw[:200]}") from e
    if not isinstance(data, dict):
        raise StepParseError(f"JSON 不是对象: {type(data).__name__}")
    try:
        return AgentStep(**data)
    except ValidationError as e:
        raise StepParseError(f"字段校验失败: {e}") from e
