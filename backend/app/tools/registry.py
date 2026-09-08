"""工具注册表 + 权限分级（Day5 / 执行计划 §4 tools/registry.py）。

把 read_file / write_file / run_tests 从 loop 的 if/elif 搬进注册表统一管理：
- 权限分级：LOW=只读/沙箱内可随便跑；MED=写工作区（git 可还原）；HIGH=工作区外/
  装包等（需人工审批，M2 接 approval.py，现阶段直接拦截抛错）。
- loop 查表分发，handler 返回 ToolResult(output, ok)，不再散落 if/elif。
- describe_tools() 生成"喂给 LLM 的工具说明书"，与运行时校验表永远同源，
  避免 LLM 调出注册表里不存在的工具。
"""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path

from ..verifier.pytest_runner import format_for_llm, run_pytest

# GitHub 交付模式环境变量门闩：drive_task --github 时置 "1"，四个 git/gh 工具
# 才放行执行（权限 MED 直接过 loop，不加审批层——门闩在 handler 内自检，避免给
# 基线 run 的审批语义开洞）。不开 --github 时工具菜单可见但调用即得明确报错，
# 引导模型回到代码修复动作，不影响 6/6 基线口径。
GITHUB_ENV_FLAG = "HARNESS_GITHUB"
GIT_TIMEOUT_S = 90   # git/gh 子进程超时（网络推拉慢，比沙箱 120s 略紧）

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
        req_end = min(offset + limit - 1, total)
        rows = lines[offset - 1: req_end]
        # 页面超长自动收缩：只返回"完整行"，绝不拦腰截断（T4 run10 实证：1500 行源码
        # 每页 100 行 ≈6500 字符 > 6000 字符上限，旧实现把页尾 ~25 行砍成残行——模型
        # 看到"[截断 N 字符]"以为漏内容，反复折返重读同一 offset、乱序跳读漏行）。
        while len(rows) > 1:
            budget = READ_FILE_CAP - (len(f"[行 {offset}-{offset + len(rows) - 1} / 共 {total} 行]") + 1)
            if sum(len(r) + 1 for r in rows) <= budget:
                break
            rows = rows[:-1]  # 从页尾逐行裁，直到整页能完整放进单次上限
        seg = "\n".join(rows)
        end = offset + len(rows) - 1
        header = f"[行 {offset}-{end} / 共 {total} 行]"
        if end >= total:
            header += "（已到文件末尾）"
        out = header + "\n" + seg
        if end < req_end:
            out += (f"\n（注：请求的 {limit} 行超出单次上限 {READ_FILE_CAP} 字符，"
                    f"已自动收缩到行 {offset}-{end}（完整行，无截断）。"
                    f"继续请用 offset={end + 1}；只需一小段就减小 limit）")
        return ToolResult(out)

    # 全文（自动截断防上下文爆炸）
    if len(content) <= READ_FILE_CAP:
        return ToolResult(content)
    cap_note = (f"\n...[文件共 {total} 行 / {len(content)} 字符，超过单次读取上限，"
                f"当前仅显示前 {READ_FILE_CAP} 字符]...\n"
                f"注意：大文件不要逐页通读！若你在找特定定义/引用（函数名、变量名、格式名等），"
                f"用 search_file 直接定位（{{\"pattern\": \"关键词\", \"path\": \"{str(args.get('path',''))}\"}}）"
                f"拿到文件:行号后，再 read_file 分页精读目标区间。")
    return ToolResult(_truncate(content, READ_FILE_CAP) + cap_note)


def _search_file(workspace: Path, args: dict) -> ToolResult:
    """按子串/正则搜索文件内容，返回 文件:行号:内容 匹配清单（带行号，可配合 read_file 分页精读）。

    真实库/大文件定位的必备工具：先 search 找到相关定义/引用在哪几行，
    再 read_file offset/limit 精读目标区间——避免从头通读大文件。
    """
    pattern = str(args.get("pattern", "")).strip()
    if not pattern:
        return ToolResult("pattern 不能为空。示例: {\"pattern\": \"def _table_formats\", \"path\": \"tabulate/__init__.py\"}")
    rel = str(args.get("path", ""))
    if rel:
        p = _path_in_workspace(workspace, rel)
        targets = [p] if p.is_file() else []
        if not targets:
            return ToolResult(f"文件不存在: {rel}")
    else:
        # 无 path = 搜整个任务目录（排除 .git/__pycache__）
        targets = [p for p in workspace.rglob("*")
                   if p.is_file() and p.suffix in (".py", ".md", ".txt", ".json", ".toml", ".ini")
                   and ".git" not in p.parts and "__pycache__" not in p.parts]
        if len(targets) > 50:
            return ToolResult(f"任务目录文件过多（{len(targets)} 个），请指定 path 限定到单个文件再搜")
    import re as _re
    try:
        rx = _re.compile(pattern)
    except _re.error as e:
        return ToolResult(f"正则非法: {e}。若想搜普通文本（含特殊字符），请转义或用简单子串。")
    hits: list[str] = []
    cap = 20  # 最多返回 20 条，防上下文爆炸
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
    return ToolResult(f"找到 {len(hits)} 处匹配（最多显示 {cap} 条，行号可配 read_file offset 精读）:\n"
                      + "\n".join(hits))


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


# ---------- GitHub 交付工具（2026-09-08，对标 MyCoder GitHub mode） ----------
# 把"自修到全绿"升级为"自修到交付"：branch → commit → push → PR 全链由 agent 自己走。
# workspace = 交付仓库的独立 clone（沙箱跑测试，git 动作在真实目录执行）。
# 所有工具 handler 首行自检门闩：HARNESS_GITHUB != "1" → ToolError（loop 转 ok=False
# 喂回模型，不崩局）。子进程环境剔除代理（直连 GitHub）+ 带 GH_CONFIG_DIR（gh 凭证）。

_BRANCH_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$")


def _require_github_mode() -> None:
    if os.environ.get(GITHUB_ENV_FLAG) != "1":
        raise ToolError("GitHub 交付模式未开启。本工具只在 drive_task --github 的交付任务里可用；"
                        "普通修复任务请继续用 edit_file/write_file/run_tests。")


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
            ["git", "-c", "http.sslVerify=false", *args],
            cwd=workspace, capture_output=True, text=True,
            timeout=GIT_TIMEOUT_S, env=_git_env())
    except subprocess.TimeoutExpired:
        raise ToolError(f"git {' '.join(args[:2])} 超时（>{GIT_TIMEOUT_S}s）。若在推送到远端，网络慢可重试一次。")
    except OSError as e:
        raise ToolError(f"git 执行失败: {e}")
    text = (p.stdout or "").strip() or (p.stderr or "").strip()
    text = text[:cap] if len(text) > cap else text
    return ToolResult(text, ok=(p.returncode == 0))


def _current_branch(workspace: Path) -> str:
    """当前分支名（symbolic-ref：unborn/正常分支都返回名字；detached/非仓库才报错）。

    不用 rev-parse --abbrev-ref HEAD：它遇到 unborn 分支输出 "HEAD" 且退出码非零，
    语义含糊（曾把"分支 ref 写入丢失"误报成"不是 git 仓库"，见 git_push 首局失败）。
    """
    r = _run_git(workspace, ["symbolic-ref", "--short", "HEAD"])
    if not r.ok or not r.output.strip():
        raise ToolError(f"工作区 HEAD 不在任何分支上（{r.output[:120]}）。"
                        "交付任务要求 workspace 是独立 clone 且在 main 分支。")
    return r.output.strip()


def _head_sha(workspace: Path) -> str:
    """HEAD 解析出的 commit sha；unborn / 零 oid 残留 / detached 一律返回空串。

    不能只看 rc：竞态残留的零 oid ref 文件会让 `rev-parse --verify HEAD`
    返回 0 且输出 40 个 0（假 born，单测实证）——必须同时查 sha 非空非全零。
    """
    r = _run_git(workspace, ["rev-parse", "HEAD"])
    sha = r.output.strip() if r.ok else ""
    if sha and len(sha) == 40 and set(sha) != {"0"}:
        return sha
    return ""


def _ensure_branch_born(workspace: Path, branch: str, base_sha: str) -> None:
    """校验"分支确实建出来且有 commit"，否则自修复，再失败才抛错。

    背景（2026-09-08 真机首局实证）：Windows + git 2.55 下偶发竞态——
    `git checkout -b` 返回成功、reflog 也写了，但 refs/heads/<branch> 没落地
    （unborn，或残留零 oid 文件），随后 rev-parse HEAD / git log 全部失败，
    push 无从谈起（约 1/25 采样率，无法稳定复现，纯环境层）。
    对策：检出后立即用 _head_sha 验证真的 born；unborn 就用 update-ref 把 ref
    补到 base_sha。【不要】用 branch --list 判空做守卫——竞态残留的零 oid ref
    会被列出，反而把修复挡在门外（真机第二轮 7 连败实证）。补不上才 raise。
    """
    if _head_sha(workspace):
        return  # 真正 born（有非零 commit）
    if not base_sha:
        raise ToolError(f"分支 {branch} 处于 unborn 且没有可用的基线 commit，无法自修复。"
                        "请重新执行 git_branch（换一个分支名）。")
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
    raise ToolError(f"分支 {branch} 检出后 ref 校验失败（unborn 竞态），update-ref 修复也未成功。"
                    "请重新执行 git_branch（原分支名或换一个均可）。")


def _git_branch(workspace: Path, args: dict) -> ToolResult:
    """从当前 HEAD 新建并切换到分支（已存在则直接切）。

    检出后走 _ensure_branch_born 校验 + unborn 自修复（Windows git 偶发 ref 写入
    丢失竞态，真机首局实证——不加校验，push 会在 unborn 分支上直接失败）。
    """
    _require_github_mode()
    branch = str(args.get("branch", "")).strip()
    if not branch:
        return ToolResult("branch 不能为空。示例: {\"branch\": \"fix/split-csv-quoting\"}")
    if not _BRANCH_NAME.match(branch) or branch.endswith((".", "/")) or ".." in branch:
        return ToolResult(f"分支名非法: {branch!r}。用字母/数字开头，只含 [A-Za-z0-9._/-]（例 fix/split-csv-quoting）")
    cur = _current_branch(workspace)
    if cur == branch:
        return ToolResult(f"已在分支 {branch}（无需切换）。")
    # 基线 commit（unborn 竞态修复用：把分支补建到基线 sha 上）。
    # 注意：若上一次 checkout 已把 HEAD 卡在 unborn 分支（本地 rev-parse HEAD 失败），
    # 兜底用 origin/main（clone 的远端基线）——真机第二轮实证：7 连败后 commit 才救回。
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
    return ToolResult(f"{act}成功。当前分支: {branch}。之后用 edit_file 改代码 → run_tests 全绿 → git_commit。")


def _git_commit(workspace: Path, args: dict) -> ToolResult:
    """把工作区内全部改动提交到当前分支（git add -A，范围=workspace 内）。

    commit 成功后校验 HEAD ref 真的前移了；unborn 竞态（ref 写入丢失）时用
    commit 输出的 sha 经 update-ref 补回——不然分支停在 unborn，push 必失败。
    """
    _require_github_mode()
    msg = str(args.get("message", "")).strip()
    if not msg:
        return ToolResult("message 不能为空。示例: {\"message\": \"fix: split_csv 支持引号内逗号\"}")
    st = _run_git(workspace, ["status", "--porcelain"])
    if not st.ok:
        raise ToolError(f"git status 失败: {st.output[:300]}")
    if not st.output.strip():
        return ToolResult("没有待提交的改动（git status 为空）。请先 edit_file/write_file 修改代码并 run_tests 验证。")
    add = _run_git(workspace, ["add", "-A"])
    if not add.ok:
        raise ToolError(f"git add 失败: {add.output[:300]}")
    r = _run_git(workspace, ["commit", "-m", msg])
    if not r.ok and ("user.name" in r.output or "user.email" in r.output):
        # 兜底：clone 未配身份时用本地身份补一次（只写本仓库 local config，不碰全局）
        _run_git(workspace, ["config", "user.name", "codecraft-agent"])
        _run_git(workspace, ["config", "user.email", "agent@codecraft.local"])
        r = _run_git(workspace, ["commit", "-m", msg])
    if not r.ok:
        raise ToolError(f"git commit 失败: {r.output[:300]}")
    branch = _current_branch(workspace)
    if not _run_git(workspace, ["rev-parse", "--verify", "HEAD"]).ok:
        # unborn 竞态：commit 对象与 reflog 都写了，但分支 ref 没落地 → 从输出补 ref
        m = re.search(r"([0-9a-f]{7,40})\] ", r.output)
        if m:
            full = _run_git(workspace, ["rev-parse", m.group(1)])
            if full.ok:
                fix = _run_git(workspace, ["update-ref", f"refs/heads/{branch}", full.output.strip()])
                if fix.ok and _run_git(workspace, ["rev-parse", "--verify", "HEAD"]).ok:
                    r = ToolResult(r.output + f"\n（注: 已自修复分支 ref 到 {full.output.strip()[:8]}）")
        if not _run_git(workspace, ["rev-parse", "--verify", "HEAD"]).ok:
            raise ToolError("commit 后分支 ref 校验失败（unborn 竞态），自动修复也未成功。请重试 git_commit。")
    files = st.output.strip().splitlines()
    changed = len(files)
    head = _run_git(workspace, ["rev-parse", "--short", "HEAD"]).output.strip()
    return ToolResult(f"已提交 {changed} 个文件到当前分支（HEAD {head}）:\n{r.output[:600]}\n"
                      "下一步: git_push（推到远端）。若还有改动要一起交付，先 edit_file 再提交一次。")


def _git_push(workspace: Path, args: dict) -> ToolResult:
    """把当前分支推到远端（git push -u origin <branch>）。"""
    _require_github_mode()
    remote = str(args.get("remote", "origin")).strip() or "origin"
    cur = _current_branch(workspace)
    r = _run_git(workspace, ["push", "-u", remote, cur])
    if not r.ok:
        raise ToolError(f"git push 失败: {r.output[:300]}。常见原因：分支已推送过但远端有新提交（先 git pull --rebase 再推，"
                        "或检查网络/凭证）。")
    url = _run_git(workspace, ["remote", "get-url", remote]).output.strip()
    return ToolResult(f"已推送分支 {cur} → {remote}（{url}）。\n{r.output[:600]}\n"
                      "下一步: gh_create_pr 开 PR（title/body 必填，base 默认 main）。")


def _find_gh() -> str:
    """定位 gh CLI：PATH → GH_BIN 环境变量 → 本项目惯例安装位。找不到抛 ToolError。

    真机实证（第二轮）：drive_task 进程 PATH 不含 gh（装在 ~/.workbuddy/binaries/gh），
    subprocess 裸调 "gh" 报 [WinError 2]。交付模式启动方（drive_task/用户）也可用
    GH_BIN 显式指路。
    """
    import shutil
    p = shutil.which("gh")
    if p:
        return p
    for cand in (os.environ.get("GH_BIN", ""),
                 str(Path.home() / ".workbuddy" / "binaries" / "gh" / "bin" / "gh.exe")):
        if cand and Path(cand).is_file():
            return cand
    raise ToolError("gh CLI 不可用（PATH 找不到，默认安装位也不存在）。分支已推送，"
                    "可用网页手动开 PR，或用 GH_BIN 环境变量指定 gh 路径后重试 gh_create_pr。")


def _gh_create_pr(workspace: Path, args: dict) -> ToolResult:
    """用 gh CLI 给当前分支开 PR（base 默认 main）。非 GitHub 远端时明确提示不创建。"""
    _require_github_mode()
    title = str(args.get("title", "")).strip()
    body = str(args.get("body", "")).strip()
    base = str(args.get("base", "main")).strip() or "main"
    if not title:
        return ToolResult("title 不能为空。示例: {\"title\": \"fix: split_csv 支持引号内逗号\", \"body\": \"...\"}")
    cur = _current_branch(workspace)
    url_r = _run_git(workspace, ["remote", "get-url", "origin"])
    if not url_r.ok or "github.com" not in (url_r.output or ""):
        return ToolResult("当前 origin 不是 GitHub 远端（无法创建真实 PR）。交付链验证到此为止：分支已推送、"
                          "代码已提交。任务可在本地仓库继续验证，但不会产生 GitHub PR。", ok=True)
    gh_bin = _find_gh()
    try:
        p = subprocess.run(
            [gh_bin, "pr", "create", "--base", base, "--head", cur,
             "--title", title, "--body", body or title],
            cwd=workspace, capture_output=True, text=True,
            timeout=GIT_TIMEOUT_S, env=_git_env())
    except OSError as e:
        raise ToolError(f"gh CLI 执行失败: {e}。分支已推送，可用网页手动开 PR。")
    except subprocess.TimeoutExpired:
        raise ToolError("gh pr create 超时。可稍后重试，或到 GitHub 网页手动开 PR（分支已推送）。")
    out = (p.stdout or "").strip()
    err = (p.stderr or "").strip()
    if p.returncode != 0:
        raise ToolError(f"gh pr create 失败: {(err or out)[:400]}")
    return ToolResult(f"PR 已创建: {out}\n（title: {title} | base: {base} ← head: {cur}）")


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
