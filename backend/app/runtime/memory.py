"""长期记忆（M5-B3）：跨 run 经验沉淀与复用。

定位（诚实口径，面试可讲）：
- 短期记忆 = 会话内上下文 + checkpoint（M0/M2，断点续跑不丢状态）
- 长期记忆 = **跨 run 行为记忆**：同一任务包第二次跑时，注入上次
  "成功路径 / 失败教训"，让 agent 不重踩坑、少走弯路（经验复用，不是知识库/RAG）

两个动作：
1. `distill(run_id, task_id, actions, status, reason)` — run 结束时调用。
   从动作序列提炼成 1 条记忆：
   - done：找最后一次全绿前的关键写动作 → success_path
   - failed/budget_paused：失败原因 → failure_lesson（比成功路径更值钱：防重踩）
2. `render_for_goal(task_id)` — run 开始时调用。检索该任务历史记忆，
   渲染成注入 user 消息的段落（放 goal 之后）。

只入不出会无限膨胀 → 每任务只留最近 N 条（db 层 LIMIT），沉淀时同 kind
去重（同任务同 kind 只保留最新，旧的下沉——简单实现：写入时若同 kind 已
存在则先删旧的）。
"""
from __future__ import annotations

from ..store import db

KIND_SUCCESS = "success_path"
KIND_FAILURE = "failure_lesson"

MAX_MEMORIES_PER_TASK = 6


def _summarize_writes(actions: list[dict]) -> str:
    """从动作序列提炼"改了什么"（edit/write 的文件清单 + 简短变化）。"""
    edits = [a for a in actions if a.get("tool") in ("edit_file", "write_file")]
    if not edits:
        return ""
    parts = []
    for a in edits[-3:]:  # 最近 3 个写动作足够
        p = str(a.get("args", {}).get("path", "?"))
        if a.get("tool") == "edit_file":
            old = str(a.get("args", {}).get("old", ""))[:40].replace("\n", " ")
            new = str(a.get("args", {}).get("new", ""))[:40].replace("\n", " ")
            parts.append(f"{p}: {old} → {new}")
        else:
            parts.append(f"{p}: 新建/重写")
    return "；".join(parts)


def distill(run_id: str, task_id: str, actions: list[dict],
            status: str, reason: str = "") -> str | None:
    """run 结束后沉淀一条记忆；返回沉淀内容（无则 None）。

    - status == done → success_path：这次怎么改到全绿的（防下次绕路）
    - 其它（failed/paused/budget_paused）→ failure_lesson：为什么没成（防重踩坑）
    """
    if status == "done":
        writes = _summarize_writes(actions)
        if not writes:
            return None
        content = f"[成功路径] 上次该任务修到全绿的关键改动：{writes}"
        db.save_memory(task_id, run_id, KIND_SUCCESS, content)
        return content
    # 失败：reason（LLM 错误/超预算/步数耗尽/漂移拒绝）
    brief = (reason or "")[:200]
    if not brief:
        return None
    content = f"[失败教训] 上次该任务未完成：{brief}"
    db.save_memory(task_id, run_id, KIND_FAILURE, content)
    return content


def render_for_goal(task_id: str, limit: int = 3) -> str:
    """按任务检索历史记忆，渲染成注入段落（run 开始时拼进 user 消息）。

    无记忆返回空串（→ 注入层直接跳过，默认关口径零影响）。
    """
    mems = db.get_memories_for_task(task_id, limit=limit)
    if not mems:
        return ""
    lines = []
    for m in mems:
        tag = {"success_path": "经验", "failure_lesson": "教训"}.get(m["kind"], "备忘")
        lines.append(f"- [{tag}] {m['content']}")
    return ("以下是本任务历史 run 的记忆（同任务跨 run 复用，供参考，以当前"
            "任务目标与实测为准，勿盲从）：\n" + "\n".join(lines))
