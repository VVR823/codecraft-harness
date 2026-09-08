"""GitHub 交付四工具（git_branch/commit/push + gh_create_pr）单测（2026-09-08）。

- 环境门闩：HARNESS_GITHUB != "1" → 四个工具一律 ToolError（loop 转 ok=False 喂回模型），
  基线 run（不开 --github）的提示词/工具面不受污染。
- 交付链路用**真实 git 本地操作**验证（tmp_path init + 本地 bare origin），零网络依赖；
  gh_create_pr 只验证"非 GitHub 远端 → 明确提示不创建、不真调 gh"的安全分支。
"""
import subprocess

import pytest

from app.runtime.protocol import Tool, parse_step
from app.tools.registry import (  # noqa: E402
    ToolError,
    _current_branch,
    _ensure_branch_born,
    _find_gh,
    get_tool,
)

GIT_TOOLS = ["git_branch", "git_commit", "git_push", "gh_create_pr"]


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True,
                   capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    """本地 git 仓库（main 分支 + 一个已提交文件 + 本地身份）。"""
    ws = tmp_path / "repo"
    ws.mkdir()
    _git("init", "-b", "main", cwd=ws)
    _git("config", "user.name", "tester", cwd=ws)
    _git("config", "user.email", "t@t.local", cwd=ws)
    (ws / "a.py").write_text("x = 1\n", encoding="utf-8")
    _git("add", "-A", cwd=ws)
    _git("commit", "-m", "init", cwd=ws)
    return ws


@pytest.fixture
def repo_no_identity(tmp_path):
    """没有 user.name/email 的仓库（验证 commit 兜底自动配本地身份）。"""
    ws = tmp_path / "repo2"
    ws.mkdir()
    _git("init", "-b", "main", cwd=ws)
    (ws / "a.py").write_text("x = 1\n", encoding="utf-8")
    _git("add", "-A", cwd=ws)
    return ws


@pytest.fixture
def origin(tmp_path_factory):
    """本地 bare 远端（模拟 origin，离线验证 push 全链）。"""
    d = tmp_path_factory.mktemp("origin") / "origin.git"
    d.mkdir()
    _git("init", "--bare", cwd=d)
    return d


@pytest.fixture
def linked(repo, origin):
    _git("remote", "add", "origin", str(origin), cwd=repo)
    return repo, origin


def _call(name, ws, args, monkeypatch, mode=True):
    if mode:
        monkeypatch.setenv("HARNESS_GITHUB", "1")
    else:
        monkeypatch.delenv("HARNESS_GITHUB", raising=False)
    return get_tool(name).handler(ws, args)


# ---------- 环境门闩（不开 --github 一律拒绝） ----------

def test_all_github_tools_denied_without_mode(repo, monkeypatch):
    for name in GIT_TOOLS:
        with pytest.raises(ToolError, match="交付模式未开启"):
            _call(name, repo, {"branch": "x", "message": "m", "title": "t"},
                  monkeypatch, mode=False)


def test_github_tools_registered_med():
    """四工具已注册、权限 MED（不过 HIGH 审批层，靠环境门闩自检）。"""
    for name in GIT_TOOLS:
        spec = get_tool(name)
        assert spec is not None
        assert spec.perm.value == 2  # Perm.MED


def test_github_tools_in_protocol_whitelist():
    """协议白名单漏加 = 模型调用被打回（T4 search_file 教训），四个新工具必须在枚举里。"""
    for name in GIT_TOOLS:
        step = parse_step(f'{{"thought": "t", "tool": "{name}", "args": {{}}, "done": false}}')
        assert step.tool_name == name
    assert Tool.git_branch.value == "git_branch"


def test_github_tools_in_describe():
    from app.tools.registry import describe_tools
    doc = describe_tools()
    for name in GIT_TOOLS:
        assert name in doc


# ---------- git_branch ----------

def test_git_branch_create_and_switch(repo, monkeypatch):
    r = _call("git_branch", repo, {"branch": "fix/split-csv-quoting"}, monkeypatch)
    assert r.ok and "新建分支" in r.output
    cur = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                         cwd=repo, capture_output=True, text=True).stdout.strip()
    assert cur == "fix/split-csv-quoting"


def test_git_branch_already_on_it(repo, monkeypatch):
    _call("git_branch", repo, {"branch": "fix/a"}, monkeypatch)
    r = _call("git_branch", repo, {"branch": "fix/a"}, monkeypatch)
    assert r.ok and "已在分支" in r.output


def test_git_branch_checkout_existing(repo, monkeypatch):
    _call("git_branch", repo, {"branch": "fix/a"}, monkeypatch)
    _call("git_branch", repo, {"branch": "main"}, monkeypatch)
    r = _call("git_branch", repo, {"branch": "fix/a"}, monkeypatch)
    assert r.ok and "切换" in r.output
    cur = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                         cwd=repo, capture_output=True, text=True).stdout.strip()
    assert cur == "fix/a"


def test_git_branch_requires_valid_name(repo, monkeypatch):
    """空/非法分支名返回引导提示（参数级提示 ok=True，与 read_file 惯例一致）。"""
    assert "不能为空" in _call("git_branch", repo, {"branch": "  "}, monkeypatch).output
    assert "分支名非法" in _call("git_branch", repo, {"branch": "-bad"}, monkeypatch).output
    assert "分支名非法" in _call("git_branch", repo, {"branch": "a..b"}, monkeypatch).output
    # 未开启交付模式才是硬拒绝（ToolError）
    with pytest.raises(ToolError):
        _call("git_branch", repo, {"branch": "fix/x"}, monkeypatch, mode=False)


# ---------- git_commit ----------

def test_git_commit_changes(repo, monkeypatch):
    (repo / "a.py").write_text("x = 2\n", encoding="utf-8")
    r = _call("git_commit", repo, {"message": "fix: bump x"}, monkeypatch)
    assert r.ok and "已提交 1 个文件" in r.output
    log = subprocess.run(["git", "log", "-1", "--format=%s"],
                         cwd=repo, capture_output=True, text=True).stdout.strip()
    assert log == "fix: bump x"


def test_git_commit_nothing_to_commit(repo, monkeypatch):
    r = _call("git_commit", repo, {"message": "fix: nothing"}, monkeypatch)
    assert "没有待提交的改动" in r.output


def test_git_commit_requires_message(repo, monkeypatch):
    (repo / "a.py").write_text("x = 3\n", encoding="utf-8")
    r = _call("git_commit", repo, {"message": "  "}, monkeypatch)
    assert "message 不能为空" in r.output


def test_git_commit_auto_identity_fallback(repo_no_identity, monkeypatch):
    """clone 没配 user.name/email → commit 失败 → 兜底本地身份，仍成功。"""
    r = _call("git_commit", repo_no_identity, {"message": "fix: x"}, monkeypatch)
    assert r.ok and "已提交" in r.output
    author = subprocess.run(["git", "log", "-1", "--format=%an"],
                            cwd=repo_no_identity, capture_output=True,
                            text=True).stdout.strip()
    assert author == "codecraft-agent"


# ---------- unborn 竞态自修复（Windows git 偶发 ref 写入丢失，真机首局实证） ----------

def _orphan_branch(repo, name):
    """确定性构造 unborn 态：checkout --orphan 后 HEAD 指向无 commit 的分支。"""
    _git("checkout", "--orphan", name, cwd=repo)


def test_current_branch_symbolic_ref_on_unborn(repo):
    """unborn 分支下 _current_branch 仍返回分支名（symbolic-ref，不误报"不是仓库"）。"""
    _orphan_branch(repo, "fix/orphan")
    assert _current_branch(repo) == "fix/orphan"


def test_ensure_branch_born_repairs_unborn(repo):
    """unborn 态 + base_sha → update-ref 补 ref，分支 born 在基线 commit 上。"""
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                          capture_output=True, text=True).stdout.strip()
    _orphan_branch(repo, "fix/orphan")
    assert not subprocess.run(["git", "rev-parse", "--verify", "HEAD"], cwd=repo,
                              capture_output=True, text=True).stdout.strip()  # 确认 unborn
    _ensure_branch_born(repo, "fix/orphan", base)  # 不应 raise
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                          capture_output=True, text=True).stdout.strip()
    assert head == base


def test_ensure_branch_born_repairs_zero_ref(repo):
    """竞态残留"零 oid ref 文件"（branch --list 列得出）也要修复——旧守卫会挡掉。

    真机第二轮实证：checkout -b 偶发留下 unborn + 零 oid 残留，旧逻辑因
    branch --list 非空跳过 update-ref → 7 连败。修复=不看列表，unborn 即补。
    """
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                          capture_output=True, text=True).stdout.strip()
    _git("checkout", "--orphan", "fix/zeroref", cwd=repo)
    ref_dir = repo / ".git" / "refs" / "heads" / "fix"
    ref_dir.mkdir(parents=True, exist_ok=True)
    (ref_dir / "zeroref").write_text("0" * 40)   # 零 oid 残留
    _ensure_branch_born(repo, "fix/zeroref", base)  # 不应 raise
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                          capture_output=True, text=True).stdout.strip()
    assert head == base


def test_ensure_branch_born_raises_without_base(repo):
    """unborn 且无 base_sha 可补 → 明确报错（不静默）。"""
    _orphan_branch(repo, "fix/nohelp")
    with pytest.raises(ToolError, match="无法自修复"):
        _ensure_branch_born(repo, "fix/nohelp", "")


def test_find_gh_prefers_which(monkeypatch):
    """gh 定位：PATH 优先；找不到走 GH_BIN；都没有抛 ToolError（不 WinError 裸崩）。"""
    import os
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as td:
        gh = os.path.join(td, "gh.exe")
        with open(gh, "w") as f:
            f.write("")
        monkeypatch.setenv("PATH", td + os.pathsep + os.environ.get("PATH", ""))
        assert _find_gh().lower().endswith("gh.exe")
    # GH_BIN 指定场景（which 已无命中）
    monkeypatch.delenv("GH_BIN", raising=False)
    with tempfile.TemporaryDirectory() as td:
        gh = os.path.join(td, "gh_custom.exe")
        with open(gh, "w") as f:
            f.write("")
        monkeypatch.setenv("GH_BIN", gh)
        assert _find_gh() == gh
    # 都没有 → 明确 ToolError（把 home 指到空目录，排除本机默认安装位的干扰）
    monkeypatch.delenv("GH_BIN", raising=False)
    monkeypatch.setenv("PATH", "")
    with tempfile.TemporaryDirectory() as td:
        monkeypatch.setattr(Path, "home", staticmethod(lambda: Path(td)))
        with pytest.raises(ToolError, match="gh CLI 不可用"):
            _find_gh()


def test_git_branch_verifies_after_checkout(repo, monkeypatch):
    """正常检出后 git_branch 返回成功（校验路径无副作用）。"""
    r = _call("git_branch", repo, {"branch": "fix/verify"}, monkeypatch)
    assert r.ok and "fix/verify" in r.output
    cur = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                         cwd=repo, capture_output=True, text=True).stdout.strip()
    assert cur == "fix/verify"


# ---------- git_push（本地 bare origin，离线验证全链） ----------

def test_git_push_to_origin(linked, monkeypatch):
    repo, origin = linked
    _call("git_branch", repo, {"branch": "fix/delivery"}, monkeypatch)
    (repo / "a.py").write_text("x = 9\n", encoding="utf-8")
    _call("git_commit", repo, {"message": "fix: delivery"}, monkeypatch)
    r = _call("git_push", repo, {}, monkeypatch)
    assert r.ok and "fix/delivery" in r.output
    # 远端确实收到分支
    remote_branches = subprocess.run(["git", "branch", "-r"],
                                     cwd=repo, capture_output=True,
                                     text=True).stdout
    assert "origin/fix/delivery" in remote_branches


# ---------- gh_create_pr ----------

def test_gh_create_pr_requires_title(linked, monkeypatch):
    repo, _ = linked
    _call("git_branch", repo, {"branch": "fix/t"}, monkeypatch)
    (repo / "a.py").write_text("x = 8\n", encoding="utf-8")
    _call("git_commit", repo, {"message": "fix: t"}, monkeypatch)
    r = _call("gh_create_pr", repo, {"title": "  ", "body": "b"}, monkeypatch)
    assert "title 不能为空" in r.output


def test_gh_create_pr_non_github_origin_no_gh_call(linked, monkeypatch):
    """origin 是本地 bare（非 github.com）→ 明确提示不创建 PR，绝不真调 gh。"""
    repo, _ = linked
    _call("git_branch", repo, {"branch": "fix/local"}, monkeypatch)
    (repo / "a.py").write_text("x = 7\n", encoding="utf-8")
    _call("git_commit", repo, {"message": "fix: local"}, monkeypatch)
    _call("git_push", repo, {}, monkeypatch)
    r = _call("gh_create_pr", repo, {"title": "fix", "body": "b"}, monkeypatch)
    assert r.ok and "不是 GitHub 远端" in r.output
