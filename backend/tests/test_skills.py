"""M5-B1 Skills 注册体系单测：SKILL.md 解析 / 发现容错 / 语义匹配 / loop 注入。"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.skills.skill_registry import (  # noqa: E402
    SkillError,
    discover_skills,
    load_skill,
    match_skills,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SKILLS_REPO = FIXTURES / "skills_repo"


# ---------- 解析 ----------

def test_load_skill_parses_frontmatter_and_body():
    s = load_skill(SKILLS_REPO / "skill_pytest")
    assert s.name == "pytest-red-fix"
    assert "红测试" in s.description
    assert "run_tests" in s.body
    assert "绝不修改测试文件" in s.body
    assert s.source.endswith("skill_pytest")


def test_load_skill_missing_frontmatter_raises():
    with pytest.raises(SkillError):
        load_skill(SKILLS_REPO / "not_a_skill")


def test_load_skill_missing_description_raises():
    with pytest.raises(SkillError):
        load_skill(SKILLS_REPO / "skill_bad")


# ---------- 发现（容错） ----------

def test_discover_skills_skips_bad_and_plain_dirs():
    skills = discover_skills(SKILLS_REPO)
    names = [s.name for s in skills]
    assert names == ["pytest-red-fix"]      # 坏 skill/普通目录都被跳过


def test_discover_skills_nonexistent_root_returns_empty():
    assert discover_skills(SKILLS_REPO / "nope") == []


# ---------- 匹配 ----------

def test_match_skills_hits_on_goal_keywords():
    # goal 提到"测试/红" → 命中 pytest skill
    hits = match_skills(SKILLS_REPO, "修复 utils.py 里导致 pytest 测试失败的 bug，让测试全绿")
    assert [s.name for s in hits] == ["pytest-red-fix"]


def test_match_skills_irrelevant_goal_returns_empty():
    assert match_skills(SKILLS_REPO, "写一个快排算法并打印结果") == []


def test_match_skills_none_dir_returns_empty():
    assert match_skills(None, "随便什么目标") == []
    assert match_skills("/no/such/dir", "随便什么目标") == []


# ---------- loop 注入 ----------

def _make_loop(use_skills: bool):
    from app.runtime.loop import HarnessLoop

    class FakeDecider:
        def __call__(self, messages):
            return None  # 不真跑，只验初始消息

    return HarnessLoop(FIXTURES / "demo_pkg", "修复导致 pytest 测试失败的 bug，让测试全绿",
                       decider=FakeDecider(), use_skills=use_skills,
                       skill_dir=str(SKILLS_REPO))


def test_loop_injects_skill_when_enabled():
    loop = _make_loop(use_skills=True)
    msgs = loop._initial_messages(loop.goal, fresh=True)
    system = msgs[0]["content"]
    assert "[技能:pytest-red-fix]" in system
    assert "绝不修改测试文件" in system


def test_loop_no_skill_when_disabled():
    loop = _make_loop(use_skills=False)
    msgs = loop._initial_messages(loop.goal, fresh=True)
    assert "[技能:" not in msgs[0]["content"]


def test_loop_skill_injected_message_persists_in_run_messages():
    """注入只发生在初始消息——run 启动后 messages[0] 含技能段，随 checkpoint 持久化。"""
    loop = _make_loop(use_skills=True)
    assert "任务目标：" in loop._initial_messages(loop.goal, fresh=True)[1]["content"]
