"""Agent 决策协议：模型每步输出一段经过校验的 JSON。

协议：{"thought": str, "tool": "read_file|edit_file|write_file|run_tests", "args": {...}, "done": bool}
- done=True 时本轮结束（tool/args 可空）。
- 允许模型包裹 ```json 代码块，剥离后提取最外层对象。
- 解析或字段校验失败时抛 StepParseError，由执行循环做有限重试。
- edit_file 使用旧片段到新片段的精确替换，避免为局部修改传输整个文件。
"""

from __future__ import annotations

import json
import re
from copy import deepcopy
from enum import Enum

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator


class Tool(str, Enum):
    read_file = "read_file"
    edit_file = "edit_file"
    write_file = "write_file"
    run_tests = "run_tests"
    search_file = "search_file"  # 在大仓库中先定位匹配行，再按区间读取源码
    install_package = "install_package"  # HIGH 权限占位，请求会进入审批拒绝与审计流程
    # GitHub 交付工具由注册表和 HARNESS_GITHUB 门闩共同限制。
    git_branch = "git_branch"  # 新建/切换分支（交付隔离）
    git_commit = "git_commit"  # 提交工作区全部改动
    git_push = "git_push"  # 推送当前分支到 origin
    gh_create_pr = "gh_create_pr"  # 开 PR 到 main


MCP_PREFIX = "mcp_"  # MCP 动态工具命名空间前缀（mcp_<server>__<tool>）


class AgentStep(BaseModel):
    thought: str = Field(description="这步在想什么")
    tool: Tool | str | None = Field(default=None, description="要调的工具；done=True 时可为空")
    args: dict = Field(default_factory=dict, description="工具参数")
    done: bool = Field(default=False, description="True=认为任务已完成")

    @model_validator(mode="after")
    def _unfinished_step_needs_tool(self):
        if not self.done and self.tool is None:
            raise ValueError("未完成的决策必须指定工具；只有done=true时tool才可为空")
        return self

    @field_validator("tool")
    @classmethod
    def _tool_must_be_known_or_mcp(cls, v):
        """工具白名单：内置枚举或带 MCP 前缀的动态工具。

        内置工具仍走 Tool 枚举强校验（防模型幻觉乱拼名字）；
        MCP 工具是运行时从 server 拉取注册的，枚举无法静态穷举，
        放行 `mcp_` 前缀（执行时 registry.get_tool 才是最终白名单，
        拉不到/没注册照样报"未知工具"）。
        """
        if v is None:
            return v
        if isinstance(v, Tool):
            v = v.value
        if isinstance(v, str) and v.startswith(MCP_PREFIX):
            return v
        from ..tools.registry import get_tool

        if get_tool(str(v)) is None:
            raise ValueError(f"未知工具: {v!r}")
        try:
            return Tool(v)  # legacy enum representation, not a second allowlist
        except ValueError:
            return str(v)

    @property
    def tool_name(self) -> str | None:
        """工具名的统一字符串形态（枚举 .value 或 mcp_ 原串）——loop 层用它分发。"""
        t = self.tool
        if t is None:
            return None
        return t.value if isinstance(t, Tool) else str(t)


def decision_schema(registry=None) -> dict:
    """Local grammar: each tool owns its complete input contract; runtime rechecks it."""
    from ..tools.registry import Perm, ToolRegistry

    registry = registry if registry is not None else ToolRegistry()

    def has_ref(value):
        if isinstance(value, dict):
            return "$ref" in value or any(has_ref(v) for v in value.values())
        return isinstance(value, list) and any(has_ref(v) for v in value)

    def branch(tool, args, done):
        return {
            "type": "object",
            "properties": {
                "thought": {"type": "string"},
                "tool": {"const": tool},
                "args": args,
                "done": {"const": done},
            },
            "required": ["thought", "tool", "args", "done"],
            "additionalProperties": False,
        }

    branches = [branch(None, {"type": "object", "properties": {}, "additionalProperties": False}, True)]
    for spec in registry.tools.values():
        if spec.perm >= Perm.HIGH:
            continue
        if registry.github is False and spec.name in ("git_branch", "git_commit", "git_push", "gh_create_pr"):
            continue
        if has_ref(spec.input_schema):
            raise ValueError("工具 schema 不支持外部引用")
        branches.append(branch(spec.name, deepcopy(spec.input_schema), False))
    # llama.cpp cannot combine object properties with a union at the same level.
    return {"oneOf": branches}


class StepParseError(Exception):
    """模型输出不是合法/可校验的 JSON 决策。"""


_JSON_BLOCK = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)
_FIRST_OBJECT = re.compile(r"(\{.*\})", re.S)  # 与 _JSON_BLOCK 一致带捕获组


def extract_json_object(text: str) -> str:
    """从模型输出里剥出最外层 {...} 的 JSON 文本；找不到抛 StepParseError。

    供 parse_step（每步决策）与 parse_plan（planner 计划）共用——两种解析
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
