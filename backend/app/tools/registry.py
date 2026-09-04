"""工具注册表 + 权限分级（Day5 / 执行计划 §4 tools/registry.py）。

把 read_file / write_file / run_tests 从 loop 的 if/elif 搬进注册表统一管理：
- 权限分级：LOW=只读/沙箱内可随便跑；MED=写工作区（git 可还原）；HIGH=工作区外/
  装包等（需人工审批，M2 接 approval.py，现阶段直接拦截抛错）。
- loop 查表分发，handler 返回 ToolResult(output, ok)，不再散落 if/elif。
- describe_tools() 生成"喂给 LLM 的工具说明书"，与运行时校验表永远同源，
  避免 LLM 调出注册表里不存在的工具。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path

from ..verifier.pytest_runner import format_for_llm, run_pytest

# 工具输出截断上限（防上下文爆炸 v0 防线，正式分层压缩在 M3 context.py）
TOOL_OUTPUT_CAP = 3000
READ_FILE_CAP = 6000


class Perm(IntEnum):
    """权限级别：数值越大越危险。"""
    LOW = 1    # 只读 / 沙箱内执行，无副作用
    MED = 2    # 写工作区文件（git 管理，可还原）
    HIGH = 3   # 工作区外 / 装包等不可逆操作（需审批，M2 接线）


class ToolError(Exception):
    """工具执行期错误（文件不存在、路径越界等），由 loop 转成整局失败原因。"""


class NeedsApprovalError(ToolError):
    """高危工具未获人工审批（M2 接 approval.py 后不再直接失败）。"""


@dataclass
class ToolResult:
    output: str
    ok: bool = True


@dataclass
class ToolSpec:
    name: str
    description: str        # 给 LLM 看的一句话
    args_hint: str          # 给 LLM 看的参数示例（JSON）
    perm: Perm
    handler: object         # (workspace: Path, args: dict) -> ToolResult


def _truncate(text: str, cap: int = TOOL_OUTPUT_CAP) -> str:
    text = text or ""
    return text if len(text) <= cap else text[:cap] + f"\n...[截断 {len(text)-cap} 字符]"


def _path_in_workspace(workspace: Path, rel: str) -> Path:
    """解析并校验路径必须落在 workspace 内（路径穿越拦截）。"""
    p = (workspace / rel).resolve()
    if not p.is_relative_to(workspace.resolve()):
        raise ToolError(f"路径越界，拒绝: {rel}")
    return p


# ---------- handlers ----------

def _read_file(workspace: Path, args: dict) -> ToolResult:
    path = _path_in_workspace(workspace, str(args.get("path", "")))
    if not path.is_file():
        raise ToolError(f"文件不存在: {path}")
    content = path.read_text(encoding="utf-8", errors="replace")
    return ToolResult(_truncate(content, READ_FILE_CAP))


def _write_file(workspace: Path, args: dict) -> ToolResult:
    path = _path_in_workspace(workspace, str(args.get("path", "")))
    content = str(args.get("content", ""))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    out = f"已写入 {path.relative_to(workspace)}（{len(content)} 字符）"
    # 即时语法体检：.py 文件写坏（截断/拼错）立刻暴露，别等 run_tests 收集期才炸
    if path.suffix == ".py" and content.strip():
        try:
            compile(content, str(path), "exec")
        except SyntaxError as e:
            out += (f"\n[语法检查失败] {path.name}:{e.lineno}: {e.msg}"
                    "\n你写入的 Python 有语法错误，先 read_file 看当前内容，修正后再 write_file。")
    return ToolResult(out)


def _run_tests(workspace: Path, args: dict) -> ToolResult:
    """沙箱跑 pytest，输出结构化病灶（红 N 条/文件:行/断言消息）。"""
    parsed = run_pytest(workspace)
    return ToolResult(format_for_llm(parsed), ok=parsed["ok"])


# ---------- 注册表 ----------

TOOLS: dict[str, ToolSpec] = {
    "read_file": ToolSpec(
        name="read_file",
        description="读取任务目录内的文件全文",
        args_hint='{"path": "utils.py"}',
        perm=Perm.LOW,
        handler=_read_file,
    ),
    "write_file": ToolSpec(
        name="write_file",
        description="把文件全文写入任务目录（覆盖同名文件；content 须为完整新内容）",
        args_hint='{"path": "utils.py", "content": "…完整文件内容…"}',
        perm=Perm.MED,
        handler=_write_file,
    ),
    "run_tests": ToolSpec(
        name="run_tests",
        description="在沙箱里跑任务目录的 pytest，返回结构化失败（文件:行号+断言消息）",
        args_hint="{}",
        perm=Perm.LOW,
        handler=_run_tests,
    ),
}


def get_tool(name: str) -> ToolSpec | None:
    return TOOLS.get(name)


def list_tool_names() -> list[str]:
    return list(TOOLS)


def describe_tools() -> str:
    """生成 system prompt 里的工具说明书（与注册表同源，不会说一套做一套）。"""
    lines = []
    for spec in TOOLS.values():
        perm_name = spec.perm.name
        lines.append(f"- {spec.name}(权限{perm_name}): {spec.description}。参数示例: {spec.args_hint}")
    return "\n".join(lines)
