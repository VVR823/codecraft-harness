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
    lines = content.splitlines()
    total = len(lines)

    # 分页读取：offset=起始行(1-based，默认 1)，limit=最多读多少行。
    # 参数宽容处理：非法值 clamp 到合法范围（模型传错不整局崩，只提示）。
    offset = args.get("offset", 1)
    limit = args.get("limit", 0)
    try:
        offset = int(offset) if offset not in (None, "") else 1
        limit = int(limit) if limit not in (None, "") else 0
    except (TypeError, ValueError):
        return ToolResult("offset/limit 必须是整数行号。示例: {\"offset\": 100, \"limit\": 50}")
    if offset < 1:
        offset = 1  # clamp：模型传 0/负数时从第 1 行读，不崩
    if limit < 0:
        limit = 0
    if offset > total:
        return ToolResult(f"offset={offset} 超出文件范围：该文件共 {total} 行（最后一行是第 {total} 行）。"
                          "往回读，或先确认你要找的内容在哪个区间。")

    if limit > 0:
        end = min(offset + limit - 1, total)
        seg = "\n".join(lines[offset - 1: offset - 1 + limit])
        header = f"[行 {offset}-{end} / 共 {total} 行]"
        if end >= total:
            header += "（已到文件末尾）"
        return ToolResult(_truncate(header + "\n" + seg, READ_FILE_CAP))

    # 全文（自动截断防上下文爆炸）
    if len(content) <= READ_FILE_CAP:
        return ToolResult(content)
    cap_note = (f"\n...[文件共 {total} 行 / {len(content)} 字符，超过单次读取上限，"
                f"当前仅显示前 {READ_FILE_CAP} 字符]...\n"
                f"需要看后面的内容请用 read_file 分页：{{\"path\": \"{str(args.get('path',''))}\", "
                f"\"offset\": 行号, \"limit\": 行数}}（offset 从 1 开始）")
    return ToolResult(_truncate(content, READ_FILE_CAP) + cap_note)


def _syntax_check(path: Path, content: str) -> str | None:
    """.py 文件语法体检：有问题返回错误提示，没问题返回 None。"""
    if path.suffix == ".py" and content.strip():
        try:
            compile(content, str(path), "exec")
        except SyntaxError as e:
            return (f"[语法检查失败] {path.name}:{e.lineno}: {e.msg}"
                    "\n你写入的 Python 有语法错误，先 read_file 看当前内容，修正后再写入。")
    return None


def _write_file(workspace: Path, args: dict) -> ToolResult:
    path = _path_in_workspace(workspace, str(args.get("path", "")))
    content = str(args.get("content", ""))
    existed = path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    out = f"已写入 {path.relative_to(workspace)}（{len(content)} 字符）"
    if existed:
        out += "\n[提示] 该文件原本已存在。如你只是改其中几行，用 edit_file(给 old→new 片段) 更稳，别整文件重写。"
    # 即时语法体检：.py 文件写坏（截断/拼错）立刻暴露，别等 run_tests 收集期才炸
    err = _syntax_check(path, content)
    if err:
        out += "\n" + err
    return ToolResult(out)


def _edit_file(workspace: Path, args: dict) -> ToolResult:
    """精准编辑：把文件里唯一的 old 片段替换成 new 片段。

    - 设计动机：改已有代码时模型只需输出"改动片段"，content 短、几乎没有
      JSON 转义问题（write_file 整文件长 content 是免费模型的翻车点）。
    - 安全：old 必须存在且唯一（不唯一=模型没找准，引导先 read_file）。
    """
    path = _path_in_workspace(workspace, str(args.get("path", "")))
    if not path.is_file():
        raise ToolError(f"文件不存在: {path}")
    old = str(args.get("old", ""))
    new = str(args.get("new", ""))
    if not old.strip():
        raise ToolError("edit_file 的 old 参数不能为空（要先 read_file 拿准确原文片段）")
    content = path.read_text(encoding="utf-8")
    n = content.count(old)
    if n == 0:
        raise ToolError(f"文件里找不到要替换的片段: {old[:100]!r}。先用 read_file 看当前内容，复制准确原文。")
    if n > 1:
        raise ToolError(f"片段在文件里出现 {n} 次不唯一，请扩大 old 范围（带上下一行）再试")
    content = content.replace(old, new, 1)
    path.write_text(content, encoding="utf-8")
    out = f"已替换 {path.relative_to(workspace)} 中 1 处（old {len(old)} 字符 → new {len(new)} 字符）"
    err = _syntax_check(path, content)
    if err:
        out += "\n" + err
    return ToolResult(out)


def _run_tests(workspace: Path, args: dict) -> ToolResult:
    """沙箱跑 pytest，输出结构化病灶（红 N 条/文件:行/断言消息）。"""
    parsed = run_pytest(workspace)
    return ToolResult(format_for_llm(parsed), ok=parsed["ok"])


# ---------- 注册表 ----------

TOOLS: dict[str, ToolSpec] = {
    "read_file": ToolSpec(
        name="read_file",
        description="读取任务目录内的文件。小文件直接读全文；大文件超过 6000 字符会自动截断并提示，"
                    "此时用 offset(起始行,1-based)/limit(行数) 分页读取后续内容（如读 200 行起："
                    "offset=200, limit=100）",
        args_hint='{"path": "utils.py"} 或大文件分页 {"path": "big.py", "offset": 200, "limit": 100}',
        perm=Perm.LOW,
        handler=_read_file,
    ),
    "edit_file": ToolSpec(
        name="edit_file",
        description="精准编辑已有文件：把文件中唯一出现的 old 文本片段替换成 new（改已有代码优先用它，"
                    "只需输出改动片段；old 必须与文件里完全一致且唯一，不唯一就扩大范围）",
        args_hint='{"path": "utils.py", "old": "被替换的原文片段", "new": "新片段"}',
        perm=Perm.MED,
        handler=_edit_file,
    ),
    "write_file": ToolSpec(
        name="write_file",
        description="把文件全文写入任务目录（覆盖同名文件；content 须为完整新内容，含原有 import/函数/"
                    "docstring。新建文件或整文件重写才用它，改已有代码优先 edit_file）",
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
    # HIGH 占位（M2 接线）：模型请求 → loop 走 approval.deny_high_tool 拒绝 + 审计记录。
    # handler 永远不被执行（_run_loop 在 perm>=HIGH 时先拦截），留它只为注册表完整性。
    "install_package": ToolSpec(
        name="install_package",
        description="[高危] 在任务环境安装第三方 Python 包（工作区外副作用，需人工审批；MVP 一律拒绝）",
        args_hint='{"package": "numpy"}',
        perm=Perm.HIGH,
        handler=lambda ws, args: ToolResult("不会执行：HIGH 工具需审批（M2 一律拒绝）"),
    ),
}


def get_tool(name: str) -> ToolSpec | None:
    return TOOLS.get(name)


def list_tool_names() -> list[str]:
    return list(TOOLS)


def register_mcp_tools(server_name: str, client, tools: list) -> int:
    """把 MCP server 拉到的工具动态注册进 TOOLS（M5-B2）。

    命名空间: `mcp_<server>__<tool>`（例 mcp_demo__sqlite_query），与本地工具
    同表同源——describe_tools / get_tool / 审计 trace 全走同一注册表，决策协议
    不改结构（loop 校验只放行内置枚举或 mcp_ 前缀）。
    handler 闭包持有 client：执行即 MCP tools/call，返回文本与本地工具同形态。
    权限定 LOW（只读）：server 侧 demo 只暴露只读工具；若要写类 MCP 工具应提级。
    返回注册数；同名已注册则跳过（重复 spawn 幂等）。
    """
    count = 0
    for t in tools:
        name = f"mcp_{server_name}__{t.name}"
        if name in TOOLS:
            continue
        def _handler(workspace, args, _t=t, _client=client):
            try:
                text = _client.call_tool(_t.name, args or {})
                return ToolResult(text, ok=True)
            except Exception as e:  # noqa: BLE001 - MCP 层错误统一转 ToolError
                raise ToolError(f"MCP {_t.name} 调用失败: {e}") from e
        TOOLS[name] = ToolSpec(
            name=name,
            description=f"[MCP:{t.server}] {t.description}",
            args_hint=t.args_hint,
            perm=Perm.LOW,
            handler=_handler,
        )
        count += 1
    return count


def unregister_mcp_tools(server_name: str) -> int:
    """按 server 名卸载动态注册的 MCP 工具（测试隔离 / server 重连用）。"""
    prefix = f"mcp_{server_name}__"
    removed = [n for n in TOOLS if n.startswith(prefix)]
    for n in removed:
        del TOOLS[n]
    return len(removed)


def describe_tools() -> str:
    """生成 system prompt 里的工具说明书（与注册表同源，不会说一套做一套）。"""
    lines = []
    for spec in TOOLS.values():
        perm_name = spec.perm.name
        lines.append(f"- {spec.name}(权限{perm_name}): {spec.description}。参数示例: {spec.args_hint}")
    return "\n".join(lines)
