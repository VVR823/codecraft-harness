"""Skill 注册体系（M5-B1，自研 SKILL.md 协议，参照 Anthropic Skills 目录标准）。

一个 skill = 一个目录，内含 SKILL.md：
```markdown
---
name: <技能名>
description: <一句话：何时用、解决什么问题>（用于语义匹配）
---
<正文：给 Agent 的指令/方法论/注意事项>
```

本模块只做三件事：
1. 解析 SKILL.md（YAML frontmatter name/description + 正文 markdown）
2. 从目录发现全部 skills（`discover_skills(dir)`）
3. 按任务 goal 语义匹配（`match_skills(dir, goal)`：关键词命中 description）

设计要点：
- 不引 yaml 库（零新依赖）：frontmatter 只取 name/description 两个字段，
  手写极简解析（`---\nkey: value\n---` 块），值不逃逸即可。
- 匹配是"弱语义"：description 里的词在 goal 里出现（或 goal 词在 description 里出现）
  即候选——不给 LLM 排序，全量注入让 Agent 自己挑。注入与否由 loop 的 use_skills
  开关决定（默认关 → 数字① 口径不动）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.S)


class SkillError(Exception):
    pass


@dataclass
class Skill:
    name: str
    description: str
    body: str            # SKILL.md 正文（给 Agent 的指令）
    source: str          # 目录路径（审计用：可指到具体文件）

    def render(self) -> str:
        """渲染成注入上下文的段落（system prompt 尾部用）。"""
        return f"[技能:{self.name}] {self.description}\n{self.body}"


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """解析 SKILL.md：返回 (frontmatter dict, 正文)。零依赖极简实现。"""
    m = FRONTMATTER_RE.match(text)
    if not m:
        raise SkillError("SKILL.md 缺少 --- frontmatter 块")
    fm_raw, body = m.group(1), m.group(2)
    fm: dict = {}
    for line in fm_raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        fm[key.strip()] = value.strip().strip('"').strip("'")
    if not fm.get("name") or not fm.get("description"):
        raise SkillError(f"SKILL.md frontmatter 缺 name/description: {fm}")
    return fm, body.strip()


def load_skill(skill_dir: str | Path) -> Skill:
    """从单个 skill 目录加载（读目录里的 SKILL.md）。"""
    d = Path(skill_dir)
    md = d / "SKILL.md"
    if not md.is_file():
        raise SkillError(f"skill 目录缺 SKILL.md: {d}")
    fm, body = _parse_frontmatter(md.read_text(encoding="utf-8"))
    return Skill(name=fm["name"], description=fm["description"],
                 body=body, source=str(d))


def discover_skills(skills_root: str | Path) -> list[Skill]:
    """扫描 skills 根目录（一层子目录=一个 skill），坏 skill 跳过并提示。"""
    root = Path(skills_root)
    if not root.is_dir():
        return []
    out: list[Skill] = []
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        try:
            out.append(load_skill(d))
        except SkillError as e:
            print(f"[skills] 跳过坏 skill {d.name}: {e}")
    return out


def match_skills(skills_root: str | Path | None, goal: str, top_k: int = 3) -> list[Skill]:
    """按任务 goal 语义匹配 skills（弱匹配：双向关键词命中）。

    - skills_root 为 None / 目录不存在 / 无 skills → 返回空（默认关口径零影响）
    - 匹配：把 description 与 goal 各自切成词，任一方向命中即候选；
      按命中词数排序取 top_k。命中词数相同保持发现顺序（稳定）。
    """
    if not skills_root:
        return []
    root = Path(skills_root)
    if not root.is_dir():
        return []
    skills = discover_skills(root)
    if not skills:
        return []

    def _words(text: str) -> set[str]:
        # 中文按 2-gram 切 + 英文按词切；去常见停用词
        text = text.lower()
        tokens: set[str] = set()
        for m in re.finditer(r"[a-z][a-z0-9_]{1,}", text):
            tokens.add(m.group())
        cn = re.sub(r"[^\u4e00-\u9fff]", "", text)
        tokens.update(cn[i:i+2] for i in range(len(cn) - 1))
        tokens.discard("任务")
        return tokens

    goal_words = _words(goal)
    scored: list[tuple[int, Skill]] = []
    for s in skills:
        desc_words = _words(s.description)
        hits = len(goal_words & desc_words)
        if hits > 0:
            scored.append((hits, s))
    scored.sort(key=lambda x: (-x[0], x[1].name))
    return [s for _, s in scored[:top_k]]
