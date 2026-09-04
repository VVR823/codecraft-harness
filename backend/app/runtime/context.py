"""分层上下文压缩（Q14，M3 数字③）。

策略（简单分层，不引向量库/RAG）：
- system 提示 + 任务目标：永远全文保留
- 最近 keep_recent_steps 步（默认 3）消息：全文保留——模型要精确操作当下的文件/测试结果
- 更早的历史：每步消息压成一行摘要，合并进一条"更早步骤摘要"user 消息

关键原则：压缩只影响"喂给决策器 decider 的视图"（decider view）；
self.messages 始终全量存档（checkpoint/resume/审计不丢任何信息）。
压缩是可逆的开关注入点——关 = 原样全量视图，开 = 分层视图，两档对比出压缩率。

token 估算（本地粗算，用于单元测试/离线对比；硬数字用真实 API prompt_tokens）：
estimate_tokens ≈ ceil(chars / 2)——中文为主时 1 字符约 0.5~1 token，取 2 字/token 保守估。
"""
from __future__ import annotations

import math

DEFAULT_KEEP_RECENT_STEPS = 3

# 一行摘要的截断上限（字符）：只留"这步干了啥"的骨架
ONE_LINE_CAP = 60


def estimate_tokens(text: str) -> int:
    """本地粗 token 估算（每 2 字符 ≈ 1 token；压缩率分母分子同口径，比率基本不受估算法影响）。"""
    return max(1, math.ceil(len(text or "") / 2))


def _one_line(msg: dict, cap: int = ONE_LINE_CAP) -> str:
    """把一条历史消息压成一行：短消息原样保留，超长截断（省的是长工具结果/大文件内容）。

    不加 role 前缀——长消息省空间、短消息不增负，压缩率才真实反映"去掉旧步冗余"。
    """
    content = (msg.get("content") or "").replace("\n", " ")
    if len(content) > cap:
        return content[:cap] + "…"
    return content


def build_view(messages: list[dict], keep_recent_steps: int = DEFAULT_KEEP_RECENT_STEPS) -> list[dict]:
    """返回喂给 decider 的视图：近 keep_recent_steps 步全文，更早压成摘要。

    keep_recent_steps=0/None 表示全部压缩；极大值（如 10**9）≈ 全量视图（关压缩）。
    """
    if not messages:
        return []
    # system 与任务目标（前两条，约定 _initial_messages 结构）永远全文
    head_n = min(2, len(messages))
    head = messages[:head_n]
    tail_all = messages[head_n:]

    # 保留最近 N 步 = 最近 N*2 条（每步 assistant+user 一对）；不足则全留
    keep = keep_recent_steps * 2
    if keep <= 0:
        old, recent = tail_all, []
    else:
        recent = tail_all[-keep:]
        old = tail_all[:-keep]

    if not old:
        return list(messages)

    # 更早历史 → 一行一条摘要，合并为一条 user 消息（放在目标之后、最近步骤之前）
    summary_lines = [_one_line(m) for m in old]
    compressed = [{"role": "user",
                   "content": "—— 更早步骤摘要（已压缩）——\n"
                              + "\n".join(summary_lines)}]
    return head + compressed + recent


def view_stats(messages: list[dict], keep_recent_steps: int = DEFAULT_KEEP_RECENT_STEPS) -> dict:
    """统计：原始/压缩后的本地估算 token、压缩率。单元测试与离线对比用。"""
    view = build_view(messages, keep_recent_steps)
    full_t = estimate_tokens("".join((m.get("content") or "") for m in messages))
    view_t = estimate_tokens("".join((m.get("content") or "") for m in view))
    rate = (full_t - view_t) / full_t if full_t else 0.0
    return {"full_tokens_est": full_t, "view_tokens_est": view_t,
            "rate": rate, "n_full": len(messages), "n_view": len(view)}
