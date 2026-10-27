"""工具注册表、参数合同和权限分级。

把 read_file / write_file / run_tests 从 loop 的 if/elif 搬进注册表统一管理：
- 权限分级：LOW=只读或沙箱内执行；MED=写工作区；HIGH=工作区外或不可逆操作。
- loop 查表分发，handler 返回 ToolResult(output, ok)，不再散落 if/elif。
- describe_tools() 从同一注册表生成模型工具说明，避免描述与运行时校验漂移。
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
from dataclasses import dataclass, field, replace
from enum import IntEnum
from contextvars import ContextVar
from pathlib import Path

from ..verifier.pytest_runner import format_for_llm, run_pytest

PROTECTED_WRITE_GUIDANCE = (
    "原测试与测试配置只读，不能通过修改断言或删除测试完成修复。"
    "请根据测试中的 import 或 search_file 定位业务实现文件，"
    "先 read_file 读取实现文件，再 edit_file 修复实现，最后 run_tests 验证；不要再次修改测试。"
)

# GitHub 工具除 MED 权限外还要求显式交付模式，避免普通修复任务产生远端副作用。
GITHUB_ENV_FLAG = "HARNESS_GITHUB"
_github_scope = ContextVar("codecraft_github", default=None)
GIT_TIMEOUT_S = 90  # git/gh 子进程超时（网络推拉慢，比沙箱 120s 略紧）

# 工具输出先做局部限长；跨步骤的上下文压缩由 runtime/context.py 处理。
TOOL_OUTPUT_CAP = 3000
READ_FILE_CAP = 6000


class Perm(IntEnum):
    """权限级别：数值越大越危险。"""

    LOW = 1  # 只读 / 沙箱内执行，无副作用
    MED = 2  # 写工作区文件（git 管理，可还原）
    HIGH = 3  # 工作区外或不可逆操作，需要审批


class ToolError(Exception):
    """可恢复的工具执行错误，由 loop 转为失败结果反馈给决策器。"""


class ToolOutcomeUnknown(Exception):
    """The operation may have completed; keep its intent for reconciliation."""


class NeedsApprovalError(ToolError):
    """高危工具未获人工审批。"""


@dataclass
class ToolResult:
    output: str
    ok: bool = True
    data: dict = field(default_factory=dict)


@dataclass
class ToolSpec:
    name: str
    description: str  # 给 LLM 看的一句话
    args_hint: str  # 给 LLM 看的参数示例（JSON）
    perm: Perm
    handler: object  # (workspace: Path, args: dict) -> ToolResult
    input_schema: dict = field(default_factory=dict)


def _truncate(text: str, cap: int = TOOL_OUTPUT_CAP) -> str:
    text = text or ""
    return text if len(text) <= cap else text[:cap] + f"\n...[截断 {len(text) - cap} 字符]"


def _path_in_workspace(workspace: Path, rel: str) -> Path:
    """解析并校验路径必须落在 workspace 内（路径穿越拦截）。"""
    p = (workspace / rel).resolve()
    if not p.is_relative_to(workspace.resolve()):
        raise ToolError(f"路径越界，拒绝: {rel}")
    return p


def _missing_file_message(workspace: Path, rel: str) -> str:
    """Return a bounded, actionable hint for a basename-only model guess."""
    name = Path(rel).name.casefold()
    if not name:
        return f"文件不存在: {rel}"
    matches: list[str] = []
    root = workspace.resolve()
    ignored = {".git", "__pycache__", ".venv", "node_modules"}
    for directory, child_dirs, filenames in os.walk(root):
        child_dirs[:] = [child for child in child_dirs if child not in ignored]
        for filename in filenames:
            if filename.casefold() != name:
                continue
            relative = (Path(directory) / filename).relative_to(root)
            matches.append(relative.as_posix())
            if len(matches) >= 5:
                break
        if len(matches) >= 5:
            break
    if not matches:
        return f"文件不存在: {rel}"
    return f"文件不存在: {rel}。仓库中同名文件: {', '.join(matches)}；请使用上面的完整相对路径重试。"


# ---------- handlers ----------


def _python_outline(content: str) -> str:
    """Bounded locations only: never execute source or replace its actual body."""
    try:
        tree = ast.parse(content)
    except (SyntaxError, ValueError, RecursionError):
        return ""
    definitions = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            definitions.append((node.name, node))
        elif isinstance(node, ast.ClassDef):
            for method in node.body:
                if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    definitions.append((f"{node.name}.{method.name}", method))
    if not definitions:
        return ""
    # AST counts physical newlines; paged reads also split form feeds and other
    # Unicode separators. Report ranges in the same coordinates as those pages.
    display_starts = [1]
    for physical_line in content.split("\n"):
        display_starts.append(display_starts[-1] + len((physical_line + "\n").splitlines()))
    header = "[Python定义位置目录（不是正文）；按需 read_file offset=起始行 limit=行数]\n"
    rows = []
    size = len(header)
    for name, node in definitions:
        start = display_starts[node.lineno - 1]
        end = display_starts[node.end_lineno] - 1
        row = f"{name}: {start}-{end}\n"
        # Reserve room for an honest omission count and the body separator.
        if size + len(row) > 1100:
            break
        rows.append(row)
        size += len(row)
    omitted = len(definitions) - len(rows)
    note = f"（省略 {omitted} 项定义；可用 search_file 定位）\n" if omitted else ""
    return header + "".join(rows) + note + "[以下为文件开头片段]\n"


def _read_file(workspace: Path, args: dict) -> ToolResult:
    rel = str(args.get("path", ""))
    path = _path_in_workspace(workspace, rel)
    if not path.is_file():
        raise ToolError(_missing_file_message(workspace, rel))
    content = path.read_text(encoding="utf-8", errors="replace")
    lines = content.splitlines()
    total = len(lines)

    # 分页读取：offset 为 1-based 起始行，limit 为最大行数。
    # 可纠正的参数错误返回工具结果，不让整个运行异常退出。
    offset = args.get("offset", 1)
    limit = args.get("limit", 0)
    try:
        offset = int(offset) if offset not in (None, "") else 1
        limit = int(limit) if limit not in (None, "") else 0
    except (TypeError, ValueError):
        return ToolResult('offset/limit 必须是整数行号。示例: {"offset": 100, "limit": 50}')
    if offset < 1:
        offset = 1  # clamp：模型传 0/负数时从第 1 行读，不崩
    if limit < 0:
        limit = 0
    if offset > total:
        return ToolResult(
            f"offset={offset} 超出文件范围：该文件共 {total} 行（最后一行是第 {total} 行）。"
            "往回读，或先确认你要找的内容在哪个区间。",
            data={"source_returned": False},
        )

    if limit > 0:
        req_end = min(offset + limit - 1, total)
        rows = lines[offset - 1 : req_end]
        # 超过字符预算时逐行收缩，保证永远不返回被截断的半行源码。
        while rows:
            end = offset + len(rows) - 1
            header = f"[行 {offset}-{end} / 共 {total} 行]"
            if end >= total:
                header += "（已到文件末尾）"
            note = ""
            if end < req_end:
                note = (
                    f"\n（注：请求的 {limit} 行超出单次上限 {READ_FILE_CAP} 字符，"
                    f"已自动收缩到行 {offset}-{end}（完整行，无截断）。"
                    f"继续请用 offset={end + 1}；只需一小段就减小 limit）"
                )
            out = header + "\n" + "\n".join(rows) + note
            # 行号和翻页提示也占预算，必须在消息层再次限长前一起计入。
            if len(out) <= READ_FILE_CAP:
                return ToolResult(out, data={"source_returned": True})
            rows = rows[:-1]
        return ToolResult(
            f"第 {offset} 行单行内容连同行号提示超过 {READ_FILE_CAP} 字符上限，"
            "无法完整展示。请用 search_file 定位具体关键词，或 read_file 读取其他行；"
            "本次未返回该行内容。",
            ok=False,
        )

    # 全文读取；超过上限时截断以控制上下文大小。
    if len(content) <= READ_FILE_CAP:
        return ToolResult(content, data={"source_returned": True})
    cap_note = (
        f"\n...[文件共 {total} 行 / {len(content)} 字符，超过单次读取上限，"
        "当前仅显示开头片段]...\n"
        f"注意：大文件不要逐页通读！若你在找特定定义/引用（函数名、变量名、格式名等），"
        "用 search_file 直接定位（pattern 填关键词，path 填当前文件路径），"
        f"拿到文件:行号后，再 read_file 分页精读目标区间。"
    )
    outline = _python_outline(content) if path.suffix == ".py" else ""
    if outline:
        # A directory is useful for choosing a range; the unrelated file prefix
        # must not consume almost the entire small local model's context.
        return ToolResult(
            outline + content[:1200] + cap_note,
            data={"source_returned": False, "python_navigation": outline.split("[以下为文件开头片段]", 1)[0]},
        )
    return ToolResult(outline + content[: READ_FILE_CAP - len(outline) - len(cap_note)] + cap_note)


def _search_file(workspace: Path, args: dict) -> ToolResult:
    """按子串/正则搜索文件内容，返回 文件:行号:内容 匹配清单（带行号，可配合 read_file 分页精读）。

    真实库/大文件定位的必备工具：先 search 找到相关定义/引用在哪几行，
    再 read_file offset/limit 精读目标区间——避免从头通读大文件。
    """
    pattern = str(args.get("pattern", "")).strip()
    if not pattern:
        return ToolResult(
            'pattern 不能为空。示例: {"pattern": "def _table_formats", "path": "tabulate/__init__.py"}'
        )
    rel = str(args.get("path", ""))
    if rel:
        p = _path_in_workspace(workspace, rel)
        targets = [p] if p.is_file() else []
        if not targets:
            return ToolResult(_missing_file_message(workspace, rel))
    else:
        # 无 path = 搜整个任务目录（排除 .git/__pycache__）
        targets = [
            p
            for p in workspace.rglob("*")
            if p.is_file()
            and p.suffix in (".py", ".md", ".txt", ".json", ".toml", ".ini")
            and ".git" not in p.parts
            and "__pycache__" not in p.parts
        ]
        if len(targets) > 50:
            return ToolResult(f"任务目录文件过多（{len(targets)} 个），请指定 path 限定到单个文件再搜")
    import re as _re

    try:
        rx = _re.compile(pattern)
    except _re.error as e:
        return ToolResult(f"正则非法: {e}。若想搜普通文本（含特殊字符），请转义或用简单子串。")
    hits: list[str] = []
    cap = 20  # 限制返回条数以控制上下文大小。
    ws_abs = workspace.resolve()
    for p in targets:
        try:
            for lineno, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if rx.search(line):
                    rel_p = p.resolve().relative_to(ws_abs)
                    hits.append(f"{rel_p}:{lineno}: {line.strip()[:150]}")
                    if len(hits) >= cap:
                        break
        except (OSError, UnicodeDecodeError):
            continue
        if len(hits) >= cap:
            break
    if not hits:
        return ToolResult(f"未找到匹配 {pattern!r} 的行（{len(targets)} 个文件）")
    return ToolResult(
        f"找到 {len(hits)} 处匹配（最多显示 {cap} 条，行号可配 read_file offset 精读）:\n" + "\n".join(hits)
    )


def _syntax_check(path: Path, content: str) -> str | None:
    """.py 文件语法体检：有问题返回错误提示，没问题返回 None。"""
    if path.suffix == ".py" and content.strip():
        try:
            compile(content, str(path), "exec")
        except SyntaxError as e:
            return (
                f"[语法检查失败] {path.name}:{e.lineno}: {e.msg}"
                "\n你写入的 Python 有语法错误，先 read_file 看当前内容，修正后再写入。"
            )
    return None


def _write_file(workspace: Path, args: dict) -> ToolResult:
    path = _path_in_workspace(workspace, str(args.get("path", "")))
    from ..runtime.workspace import protected

    if protected(path.relative_to(workspace.resolve())):
        raise ToolError("禁止写入原测试或受保护配置。" + PROTECTED_WRITE_GUIDANCE)
    content = str(args.get("content", ""))
    err = _syntax_check(path, content)
    if err:
        return ToolResult(err, ok=False)
    existed = path.exists()
    if existed and path.read_text(encoding="utf-8") == content:
        return ToolResult("内容未变化，未写入文件。", data={"changed": False})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    out = f"已写入 {path.relative_to(workspace)}（{len(content)} 字符）"
    if existed:
        out += "\n[提示] 请 run_tests 验证本次修改，测试全绿后才能完成。"
    # 即时语法检查：尽早暴露截断或拼写造成的 Python 语法错误。
    err = _syntax_check(path, content)
    if err:
        out += "\n" + err
    return ToolResult(out, data={"changed": True})


def _edit_file(workspace: Path, args: dict) -> ToolResult:
    """精准编辑：把文件里唯一的 old 片段替换成 new 片段。

    - 设计动机：改已有代码时模型只需输出"改动片段"，content 短、几乎没有
      JSON 转义问题（write_file 整文件长 content 是免费模型的翻车点）。
    - 安全：old 必须存在且唯一（不唯一=模型没找准，引导先 read_file）。
    """
    path = _path_in_workspace(workspace, str(args.get("path", "")))
    from ..runtime.workspace import protected

    if protected(path.relative_to(workspace.resolve())):
        raise ToolError("禁止修改原测试或受保护配置。" + PROTECTED_WRITE_GUIDANCE)
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
    updated = content.replace(old, new, 1)
    err = _syntax_check(path, updated)
    if err:
        return ToolResult(err, ok=False)
    if updated == content:
        return ToolResult("内容未变化，未写入文件。", data={"changed": False})
    path.write_text(updated, encoding="utf-8")
    out = f"已替换 {path.relative_to(workspace)} 中 1 处（old {len(old)} 字符 → new {len(new)} 字符）"
    err = _syntax_check(path, updated)
    if err:
        out += "\n" + err
    return ToolResult(out, data={"changed": True})


def _run_tests(workspace: Path, args: dict) -> ToolResult:
    """在沙箱中运行 pytest，返回失败数、文件位置和断言摘要。"""
    parsed = run_pytest(workspace)
    return ToolResult(format_for_llm(parsed), ok=parsed["ok"], data=parsed)


# ---------- GitHub 交付工具 ----------
# 交付在独立 clone 中完成 branch → commit → push → PR；普通修复模式无法调用。

_BRANCH_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$")


def _require_github_mode() -> None:
    scoped = _github_scope.get()
    if not (scoped if scoped is not None else os.environ.get(GITHUB_ENV_FLAG) == "1"):
        raise ToolError(
            "GitHub 交付模式未开启。本工具只在 drive_task --github 的交付任务里可用；"
            "普通修复任务请继续用 edit_file/write_file/run_tests。"
        )


def _git_env() -> dict:
    """git/gh 子进程环境：代理会干扰直连 GitHub（吊销/超时），gh 凭证在 GH_CONFIG_DIR。"""
    env = dict(os.environ)
    for key in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        env.pop(key, None)
    return env


def _run_git(workspace: Path, args: list, cap: int = 2500) -> ToolResult:
    """跑 git，返回 (ok, output)。失败时 output 带 stderr 摘要（模型可读可纠正）。"""
    try:
        p = subprocess.run(
            ["git", *args],
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_S,
            env=_git_env(),
        )
    except subprocess.TimeoutExpired:
        if args and args[0] in {"push", "commit", "checkout", "update-ref"}:
            raise ToolOutcomeUnknown("Git操作超时，副作用结果未知；停止执行并保留现场，不自动重试") from None
        raise ToolError(f"git只读查询超时（>{GIT_TIMEOUT_S}s）")
    except OSError as e:
        raise ToolError(f"git 执行失败: {e}")
    text = (p.stdout or "").strip() or (p.stderr or "").strip()
    text = text[:cap] if len(text) > cap else text
    return ToolResult(text, ok=(p.returncode == 0))


def _current_branch(workspace: Path) -> str:
    """当前分支名（symbolic-ref：unborn/正常分支都返回名字；detached/非仓库才报错）。

    不用 rev-parse --abbrev-ref HEAD：它遇到 unborn 分支输出 "HEAD" 且退出码非零，
    语义含糊，无法区分未出生分支与非仓库目录。
    """
    r = _run_git(workspace, ["symbolic-ref", "--short", "HEAD"])
    if not r.ok or not r.output.strip():
        raise ToolError(
            f"工作区 HEAD 不在任何分支上（{r.output[:120]}）。"
            "交付任务要求 workspace 是独立 clone 且在 main 分支。"
        )
    return r.output.strip()


def _head_sha(workspace: Path) -> str:
    """HEAD 解析出的 commit sha；unborn / 零 oid 残留 / detached 一律返回空串。

    不能只看返回码：竞态残留的零 oid ref 可能输出 40 个 0，必须同时校验 sha。
    """
    r = _run_git(workspace, ["rev-parse", "HEAD"])
    sha = r.output.strip() if r.ok else ""
    if sha and len(sha) == 40 and set(sha) != {"0"}:
        return sha
    return ""


def _ensure_branch_born(workspace: Path, branch: str, base_sha: str) -> None:
    """确保检出的分支指向有效 commit，必要时用基线 sha 修复分支引用。

    某些 Git/文件系统组合可能在 checkout 成功后留下 unborn 或零 oid 引用。
    branch --list 仍会列出这种损坏引用，因此必须直接校验 HEAD。
    """
    if _head_sha(workspace):
        return  # 真正 born（有非零 commit）
    if not base_sha:
        raise ToolError(
            f"分支 {branch} 处于 unborn 且没有可用的基线 commit，无法自修复。"
            "请重新执行 git_branch（换一个分支名）。"
        )
    # update-ref 补 ref；失败先清可能残留的 .lock（竞态若留锁，补写必失败），重试一次
    for attempt in range(2):
        fix = _run_git(workspace, ["update-ref", f"refs/heads/{branch}", base_sha])
        if fix.ok and _head_sha(workspace):
            return
        if attempt == 0:
            # 竞态若留下 <ref>.lock（checkout 写 ref 中途夭折），update-ref 会被锁挡住
            try:
                (workspace / ".git" / "refs" / "heads" / f"{branch}.lock").unlink()
            except OSError:
                pass
    raise ToolError(
        f"分支 {branch} 检出后 ref 校验失败（unborn 竞态），update-ref 修复也未成功。"
        "请重新执行 git_branch（原分支名或换一个均可）。"
    )


def _git_branch(workspace: Path, args: dict) -> ToolResult:
    """从当前 HEAD 新建并切换分支；检出后校验分支引用。"""
    _require_github_mode()
    branch = str(args.get("branch", "")).strip()
    if not branch:
        return ToolResult('branch 不能为空。示例: {"branch": "fix/split-csv-quoting"}')
    if not _BRANCH_NAME.match(branch) or branch.endswith((".", "/")) or ".." in branch:
        return ToolResult(
            f"分支名非法: {branch!r}。用字母/数字开头，只含 [A-Za-z0-9._/-]（例 fix/split-csv-quoting）"
        )
    cur = _current_branch(workspace)
    if cur == branch:
        return ToolResult(f"已在分支 {branch}（无需切换）。")
    # HEAD 无法解析时使用 origin/main 作为修复 unborn 分支的基线。
    base = _run_git(workspace, ["rev-parse", "HEAD"])
    base_sha = base.output.strip() if base.ok else ""
    if not base_sha:
        fb = _run_git(workspace, ["rev-parse", "refs/remotes/origin/main"])
        base_sha = fb.output.strip() if fb.ok else ""
    exist = _run_git(workspace, ["branch", "--list", branch])
    if exist.ok and exist.output.strip():
        r = _run_git(workspace, ["checkout", branch])
        act = f"切换到已有分支 {branch}"
    else:
        r = _run_git(workspace, ["checkout", "-b", branch])
        act = f"新建分支 {branch} 并切换"
        if not r.ok:
            raise ToolError(f"{act}失败: {r.output[:300]}")
    if not r.ok:
        raise ToolError(f"{act}失败: {r.output[:300]}")
    _ensure_branch_born(workspace, branch, base_sha)
    return ToolResult(
        f"{act}成功。当前分支: {branch}。之后用 edit_file 改代码 → run_tests 全绿 → git_commit。"
    )


def _git_commit(workspace: Path, args: dict) -> ToolResult:
    """把工作区内全部改动提交到当前分支（git add -A，范围=workspace 内）。

    commit 成功后校验 HEAD ref 真的前移了；unborn 竞态（ref 写入丢失）时用
    commit 输出的 sha 经 update-ref 补回——不然分支停在 unborn，push 必失败。
    """
    _require_github_mode()
    msg = str(args.get("message", "")).strip()
    if not msg:
        return ToolResult('message 不能为空。示例: {"message": "fix: split_csv 支持引号内逗号"}')
    st = _run_git(workspace, ["status", "--porcelain"])
    if not st.ok:
        raise ToolError(f"git status 失败: {st.output[:300]}")
    if not st.output.strip():
        return ToolResult(
            "没有待提交的改动（git status 为空）。请先 edit_file/write_file 修改代码并 run_tests 验证。"
        )
    add = _run_git(workspace, ["add", "-A"])
    if not add.ok:
        raise ToolError(f"git add 失败: {add.output[:300]}")
    r = _run_git(workspace, ["commit", "-m", msg])
    if not r.ok and ("user.name" in r.output or "user.email" in r.output):
        # clone 未配身份时只设置当前仓库的本地身份，不修改全局配置。
        _run_git(workspace, ["config", "user.name", "codecraft-agent"])
        _run_git(workspace, ["config", "user.email", "agent@codecraft.local"])
        r = _run_git(workspace, ["commit", "-m", msg])
    if not r.ok:
        raise ToolError(f"git commit 失败: {r.output[:300]}")
    branch = _current_branch(workspace)
    if not _run_git(workspace, ["rev-parse", "--verify", "HEAD"]).ok:
        # commit 已生成但分支引用未落地时，从输出恢复引用。
        m = re.search(r"([0-9a-f]{7,40})\] ", r.output)
        if m:
            full = _run_git(workspace, ["rev-parse", m.group(1)])
            if full.ok:
                fix = _run_git(workspace, ["update-ref", f"refs/heads/{branch}", full.output.strip()])
                if fix.ok and _run_git(workspace, ["rev-parse", "--verify", "HEAD"]).ok:
                    r = ToolResult(r.output + f"\n（注: 已自修复分支 ref 到 {full.output.strip()[:8]}）")
        if not _run_git(workspace, ["rev-parse", "--verify", "HEAD"]).ok:
            raise ToolError(
                "commit 后分支 ref 校验失败（unborn 竞态），自动修复也未成功。请重试 git_commit。"
            )
    files = st.output.strip().splitlines()
    changed = len(files)
    head = _run_git(workspace, ["rev-parse", "--short", "HEAD"]).output.strip()
    return ToolResult(
        f"已提交 {changed} 个文件到当前分支（HEAD {head}）:\n{r.output[:600]}\n"
        "下一步: git_push（推到远端）。若还有改动要一起交付，先 edit_file 再提交一次。"
    )


def _git_push(workspace: Path, args: dict) -> ToolResult:
    """把当前分支推到远端（git push -u origin <branch>）。"""
    _require_github_mode()
    remote = str(args.get("remote", "origin")).strip() or "origin"
    cur = _current_branch(workspace)
    r = _run_git(workspace, ["push", "-u", remote, cur])
    if not r.ok:
        raise ToolError(
            f"git push 失败: {r.output[:300]}。常见原因：分支已推送过但远端有新提交（先 git pull --rebase 再推，"
            "或检查网络/凭证）。"
        )
    url = _run_git(workspace, ["remote", "get-url", remote]).output.strip()
    return ToolResult(
        f"已推送分支 {cur} → {remote}（{url}）。\n{r.output[:600]}\n"
        "下一步: gh_create_pr 开 PR（title/body 必填，base 默认 main）。"
    )


def _find_gh() -> str:
    """定位 gh CLI：PATH → GH_BIN 环境变量 → 本项目惯例安装位。找不到抛 ToolError。

    部分启动环境不会继承用户安装目录，因此同时支持 GH_BIN 和项目惯例安装位。
    """
    import shutil

    p = shutil.which("gh")
    if p:
        return p
    for cand in (
        os.environ.get("GH_BIN", ""),
        str(Path.home() / ".workbuddy" / "binaries" / "gh" / "bin" / "gh.exe"),
    ):
        if cand and Path(cand).is_file():
            return cand
    raise ToolError(
        "gh CLI 不可用（PATH 找不到，默认安装位也不存在）。分支已推送，"
        "可用网页手动开 PR，或用 GH_BIN 环境变量指定 gh 路径后重试 gh_create_pr。"
    )


def _gh_create_pr(workspace: Path, args: dict) -> ToolResult:
    """用 gh CLI 给当前分支开 PR（base 默认 main）。非 GitHub 远端时明确提示不创建。"""
    _require_github_mode()
    title = str(args.get("title", "")).strip()
    body = str(args.get("body", "")).strip()
    base = str(args.get("base", "main")).strip() or "main"
    if not title:
        return ToolResult('title 不能为空。示例: {"title": "fix: split_csv 支持引号内逗号", "body": "..."}')
    cur = _current_branch(workspace)
    url_r = _run_git(workspace, ["remote", "get-url", "origin"])
    if not url_r.ok or "github.com" not in (url_r.output or ""):
        return ToolResult(
            "当前 origin 不是 GitHub 远端（无法创建真实 PR）。交付链验证到此为止：分支已推送、"
            "代码已提交。任务可在本地仓库继续验证，但不会产生 GitHub PR。",
            ok=True,
        )
    gh_bin = _find_gh()
    try:
        p = subprocess.run(
            [
                gh_bin,
                "pr",
                "create",
                "--base",
                base,
                "--head",
                cur,
                "--title",
                title,
                "--body",
                body or title,
            ],
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_S,
            env=_git_env(),
        )
    except OSError as e:
        raise ToolError(f"gh CLI 执行失败: {e}。分支已推送，可用网页手动开 PR。")
    except subprocess.TimeoutExpired:
        raise ToolOutcomeUnknown("gh pr create超时，PR结果未知；请人工核查，不自动重试") from None
    out = (p.stdout or "").strip()
    err = (p.stderr or "").strip()
    if p.returncode != 0:
        raise ToolError(f"gh pr create 失败: {(err or out)[:400]}")
    return ToolResult(
        f"PR 已创建: {out}\n（title: {title} | base: {base} ← head: {cur}）", data={"pr_url": out}
    )


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
    "search_file": ToolSpec(
        name="search_file",
        description="按正则/子串搜索文件内容，返回 文件:行号:内容 匹配清单。真实库/大文件定位必备："
        "先 search 找定义/引用在哪几行，再 read_file 分页精读，别从头通读大文件。"
        "path 省略则搜整个任务目录（限 50 文件内）",
        args_hint='{"pattern": "GITHUB_ESCAPE_RULES"} 或 {"pattern": "def tabulate", "path": "tabulate/__init__.py"}',
        perm=Perm.LOW,
        handler=_search_file,
    ),
    "edit_file": ToolSpec(
        name="edit_file",
        description="精准编辑大文件的局部内容：把文件中唯一出现的 old 文本片段替换成 new（"
        "只需输出改动片段；old 必须与文件里完全一致且唯一，不唯一就扩大范围）",
        args_hint='{"path": "utils.py", "old": "被替换的原文片段", "new": "新片段"}',
        perm=Perm.MED,
        handler=_edit_file,
    ),
    "write_file": ToolSpec(
        name="write_file",
        description="把文件全文写入任务目录（覆盖同名文件；content 须为完整新内容，含原有 import/函数/"
        "docstring。已完整读取、无截断且不超过100行的小实现文件优先用 write_file；大文件使用 edit_file）",
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
    "git_branch": ToolSpec(
        name="git_branch",
        description="[GitHub 交付，须 drive_task --github] 从当前 HEAD 新建分支并切换"
        "（交付第一步：把修复隔离到独立分支；已存在则直接切）。分支名用字母开头，"
        "只含字母/数字/._/-，例如 fix/split-csv-quoting",
        args_hint='{"branch": "fix/split-csv-quoting"}',
        perm=Perm.MED,
        handler=_git_branch,
    ),
    "git_commit": ToolSpec(
        name="git_commit",
        description="[GitHub 交付，须 drive_task --github] 把工作区内的全部改动提交到当前分支"
        "（git add -A，范围=本目录）。message 用约定式前缀（fix:/feat:/docs:）。"
        "提交前必须已 run_tests 全绿",
        args_hint='{"message": "fix: split_csv 支持引号内逗号"}',
        perm=Perm.MED,
        handler=_git_commit,
    ),
    "git_push": ToolSpec(
        name="git_push",
        description="[GitHub 交付，须 drive_task --github] 把当前分支推送到远端 origin"
        "（git push -u）。成功后即可开 PR",
        args_hint="{}",
        perm=Perm.MED,
        handler=_git_push,
    ),
    "gh_create_pr": ToolSpec(
        name="gh_create_pr",
        description="[GitHub 交付，须 drive_task --github] 用 gh CLI 为当前分支创建 PR 到 main"
        "（title/body 必填）。origin 不是 GitHub 远端时明确提示不创建",
        args_hint='{"title": "fix: split_csv 支持引号内逗号", "body": "修复描述", "base": "main"}',
        perm=Perm.MED,
        handler=_gh_create_pr,
    ),
    # HIGH 工具由执行循环先行拒绝并记录；handler 仅保持注册表合同完整。
    "install_package": ToolSpec(
        name="install_package",
        description="[高危] 在任务环境安装第三方 Python 包（工作区外副作用，需人工审批；当前策略一律拒绝）",
        args_hint='{"package": "numpy"}',
        perm=Perm.HIGH,
        handler=lambda ws, args: ToolResult("不会执行：HIGH 工具需审批，当前策略一律拒绝"),
    ),
}


def get_tool(name: str) -> ToolSpec | None:
    return TOOLS.get(name)


def list_tool_names() -> list[str]:
    return list(TOOLS)


def register_mcp_tools(server_name: str, client, tools: list) -> int:
    """把 MCP server 返回的工具动态注册进 TOOLS。

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


def _schema(properties, required=()):
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


_S = {"type": "string"}
_BUILTIN_SCHEMAS = {
    "read_file": _schema(
        {"path": _S, "offset": {"type": "integer", "minimum": 1}, "limit": {"type": "integer", "minimum": 0}},
        ["path"],
    ),
    "search_file": _schema({"path": _S, "pattern": {"type": "string", "minLength": 1}}, ["pattern"]),
    "edit_file": _schema({"path": _S, "old": _S, "new": _S}, ["path", "old", "new"]),
    "write_file": _schema({"path": _S, "content": _S}, ["path", "content"]),
    "run_tests": _schema({}),
    "install_package": _schema({"package": _S}, ["package"]),
    "git_branch": _schema({"branch": _S}, ["branch"]),
    "git_commit": _schema({"message": _S}, ["message"]),
    "git_push": _schema({"remote": _S}),
    "gh_create_pr": _schema({"title": _S, "body": _S, "base": _S}, ["title", "body"]),
}
for _name, _spec in TOOLS.items():
    _spec.input_schema = _BUILTIN_SCHEMAS.get(_name, _schema({}))


def validate_args(spec: ToolSpec, args: dict) -> dict:
    from jsonschema import Draft202012Validator

    # External tool schemas are local data; never resolve network/file refs.
    def has_ref(value):
        if isinstance(value, dict):
            return "$ref" in value or any(has_ref(v) for v in value.values())
        return isinstance(value, list) and any(has_ref(v) for v in value)

    if has_ref(spec.input_schema):
        raise ToolError("工具 schema 不支持外部引用")
    error = next(Draft202012Validator(spec.input_schema).iter_errors(args), None)
    if error:
        raise ToolError(f"工具 {spec.name} 参数不合法: {error.message[:200]}")
    return args


class ToolRegistry:
    """One run owns its tools and MCP clients; no global client closures."""

    def __init__(self, *, github=None, sandbox_timeout=120):
        self.github = github
        self.sandbox_timeout = sandbox_timeout
        self.tools = {name: replace(spec) for name, spec in TOOLS.items() if not name.startswith("mcp_")}
        self.clients = {}

    def get(self, name):
        return self.tools.get(name)

    def describe(self, *, github=True, include_high=True):
        return "\n".join(
            f"- {s.name}(权限{s.perm.name}): {s.description}。参数: {s.args_hint}"
            for s in self.tools.values()
            if (include_high or s.perm < Perm.HIGH)
            and (github or s.name not in {"git_branch", "git_commit", "git_push", "gh_create_pr"})
        )

    def execute(self, name, workspace, args):
        spec = self.get(name)
        if spec is None:
            raise ToolError(f"未知工具: {name}")
        if spec.perm >= Perm.HIGH:
            raise NeedsApprovalError("高危工具默认拒绝，未执行")
        args = validate_args(spec, args)
        token = _github_scope.set(self.github)
        try:
            if name == "run_tests":
                parsed = run_pytest(workspace, timeout_s=self.sandbox_timeout)
                return ToolResult(format_for_llm(parsed), ok=parsed["ok"], data=parsed)
            return spec.handler(workspace, args)
        finally:
            _github_scope.reset(token)

    def register_mcp(self, server_name, client, tools, *, readonly=()):
        if server_name in self.clients and self.clients[server_name] is not client:
            self.clients[server_name].stop()
        prefix = f"mcp_{server_name}__"
        self.tools = {k: v for k, v in self.tools.items() if not k.startswith(prefix)}
        self.clients[server_name] = client
        for tool in tools:
            name = prefix + tool.name

            def handler(ws, args, t=tool, c=client):
                try:
                    return ToolResult(c.call_tool(t.name, args))
                except Exception as exc:
                    raise ToolError(f"MCP {t.name} 调用失败") from exc

            schema = getattr(tool, "input_schema", None) or _schema({})
            self.tools[name] = ToolSpec(
                name,
                tool.description,
                tool.args_hint,
                Perm.LOW if tool.name in readonly else Perm.HIGH,
                handler,
                schema,
            )
        return len(tools)

    def close(self):
        for client in self.clients.values():
            client.stop()
        self.clients.clear()
        self.tools = {k: v for k, v in self.tools.items() if not k.startswith("mcp_")}
