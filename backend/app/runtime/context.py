"""为决策循环提供分层上下文压缩。

策略（简单分层，不引向量库/RAG）：
- system 提示 + 任务目标：永远全文保留
- 最近 keep_recent_steps 步（默认 3）消息：全文保留——模型要精确操作当下的文件/测试结果
- 更早的历史：每步消息压成一行摘要，合并进一条"更早步骤摘要"user 消息
- 已压缩的最近一次测试：保留最多600字符结构化事实，标明与最近修改的先后关系

关键原则：压缩只影响"喂给决策器 decider 的视图"（decider view）；
self.messages 始终全量存档（checkpoint/resume/审计不丢任何信息）。
压缩是可逆的开关注入点——关 = 原样全量视图，开 = 分层视图，两档对比出压缩率。

token 估算（本地粗算，用于单元测试/离线对比；硬数字用真实 API prompt_tokens）：
estimate_tokens ≈ ceil(chars / 2)——中文为主时 1 字符约 0.5~1 token，取 2 字/token 保守估。
"""

from __future__ import annotations

import math
import json

DEFAULT_KEEP_RECENT_STEPS = 3

# 一行摘要的截断上限（字符）：只留"这步干了啥"的骨架
ONE_LINE_CAP = 60
TEST_FACT_CAP = 600
NAVIGATION_CAP = 1400
HISTORY_CAP = 1600
SOURCE_FACT_CAP = 6000
SOURCE_FILE_FACT_CAP = 2900
SOURCE_FILE_LIMIT = 2


def _decision(msg: dict) -> dict:
    if msg.get("role") != "assistant":
        return {}
    try:
        value = json.loads(msg.get("content") or "")
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) and isinstance(value.get("args"), dict) else {}


def estimate_tokens(text: str) -> int:
    """本地粗略估算 token；压缩前后采用同一算法以保持比率可比。"""
    return max(1, math.ceil(len(text or "") / 2))


def _one_line(msg: dict, cap: int = ONE_LINE_CAP) -> str:
    """把一条历史消息压成一行：短消息原样保留，超长截断（省的是长工具结果/大文件内容）。

    不加 role 前缀——长消息省空间、短消息不增负，压缩率才真实反映"去掉旧步冗余"。
    """
    content = (msg.get("content") or "").replace("\n", " ")
    decision = _decision(msg)
    if decision.get("tool"):
        args = decision["args"]
        # Keep actual observations/locations, never the model's private reasoning
        # or old/new/content payloads. Those are not a useful activity summary.
        fields = {key: args[key] for key in ("path", "pattern", "offset", "limit") if key in args}
        content = f"{decision['tool']} {json.dumps(fields, ensure_ascii=False)}"
        cap = 240
    if len(content) > cap:
        return content[:cap] + "…"
    return content


def _source_facts(tail: list[dict], recent: list[dict], actions: list[dict]) -> list[str]:
    """Retain bounded recent source pages per file until a write attempt.

    This is a transient working set reconstructed from the saved tool messages,
    so it also works after restart without another mutable cache/checkpoint.
    """
    reads = iter(action for action in reversed(actions) if action["tool"] == "read_file")
    selected_paths: set[str] = set()
    invalid_paths: set[str] = set()
    facts: list[str] = []
    for index in range(len(tail) - 1, -1, -1):
        request = _decision(tail[index])
        tool = request.get("tool")
        if tool in ("edit_file", "write_file"):
            break
        if tool != "read_file":
            continue
        args = request["args"]
        path = str(args.get("path", "?"))[:240]
        action = next(reads, None)
        if action is not None:
            data = action.get("data") or {}
            if not action.get("ok"):
                invalid_paths.add(path)
                continue
            if data.get("source_returned") is not True:
                # A directory provides locations only; it cannot supersede the
                # last source page. Unknown legacy/hint reads stay conservative.
                if data.get("python_navigation"):
                    continue
                invalid_paths.add(path)
                continue
        if index + 1 >= len(tail):
            invalid_paths.add(path)
            continue
        result = tail[index + 1]
        text = result.get("content") or ""
        prefix = "[工具结果 read_file] "
        if result.get("role") != "user" or not text.startswith(prefix):
            invalid_paths.add(path)
            continue
        if result in recent:
            selected_paths.add(path)
            continue
        body = text[len(prefix) :]
        if body.startswith("[工具错误]"):
            invalid_paths.add(path)
            continue
        if "[以下为文件开头片段]" in body:
            continue
        if path in selected_paths or path in invalid_paths:
            continue
        header = (
            f"最近源码读取（{path}；offset={args.get('offset', 1)}；limit={args.get('limit', 0)}；"
            "读取时原文，后续未尝试写入）：\n"
        )
        if len(header) + len(body) <= SOURCE_FILE_FACT_CAP:
            fact = header + body
        else:
            note = "\n[以上为原读取的节选；需要未展示的部分时按原路径/行号重新读取，不能猜测源码。]"
            room = SOURCE_FILE_FACT_CAP - len(header) - len(note)
            excerpt = body[:room].rsplit("\n", 1)[0]
            fact = header + excerpt + note
        facts.append(fact)
        selected_paths.add(path)
        if len(facts) >= SOURCE_FILE_LIMIT:
            break
    return facts


def _recon_cycle_fact(actions: list[dict]) -> str:
    """Explain an observed short read/search cycle without forcing an edit."""
    page_cycle = actions[-4:]
    if len(page_cycle) == 4:
        first_search, first_read, second_search, second_read = page_cycle
        same_search = first_search["tool"] == second_search["tool"] == "search_file" and first_search.get(
            "args", {}
        ) == second_search.get("args", {})
        first_args = first_read.get("args", {})
        second_args = second_read.get("args", {})
        same_page_start = (
            first_read["tool"] == second_read["tool"] == "read_file"
            and first_args.get("path") == second_args.get("path")
            and int(first_args.get("offset", 1)) == int(second_args.get("offset", 1))
        )
        first_limit = first_args.get("limit")
        second_limit = second_args.get("limit")
        contained_page = (
            isinstance(first_limit, int)
            and isinstance(second_limit, int)
            and second_limit <= first_limit
            and (first_read.get("data") or {}).get("source_returned") is True
            and (second_read.get("data") or {}).get("source_returned") is True
        )
        if same_search and same_page_start and contained_page:
            path = str(second_args.get("path", ""))[:120]
            pattern = str(second_search.get("args", {}).get("pattern", ""))[:120]
            return (
                "重复侦察（同一关键词搜索后，又读取同一位置的相同或更小分页）：\n"
                f"search_file {path} / {pattern} → read_file offset={second_args.get('offset', 1)}\n"
                "该页没有带来新范围，不要第三次重复。回到任务目标和失败测试；"
                "优先读取目标点名的测试文件，或搜索尚未检查的函数/符号，再依据实际源码做最小修改。"
            )
    repair_cycle = actions[-4:]
    if len(repair_cycle) == 4:
        repair_signatures = [
            json.dumps([action["tool"], action.get("args", {})], sort_keys=True) for action in repair_cycle
        ]
        repeated_pair = repair_signatures[:2] == repair_signatures[2:]
        read_action, write_action = repair_cycle[:2]
        failed_repair_pair = (
            read_action["tool"] in ("read_file", "search_file")
            and write_action["tool"] in ("edit_file", "write_file")
            and write_action.get("ok") is False
            and repair_cycle[3].get("ok") is False
        )
        if repeated_pair and failed_repair_pair:
            path = str(read_action.get("args", {}).get("path", ""))[:120]
            return (
                "重复侦察（读取同一分页后提交同一修改失败，已连续出现2次）：\n"
                f"{read_action['tool']} {path} → {write_action['tool']} 修改失败\n"
                "不要再读取同一分页，也不要再次提交相同 old/content。回到任务目标和失败测试，"
                "优先定位其中点名的函数、符号或断言；读取尚未看过的准确位置后再做最小修改。"
            )
    recent = actions[-6:]
    if len(recent) < 6 or any(a["tool"] not in ("read_file", "search_file") for a in recent):
        return ""
    signatures = [json.dumps([a["tool"], a.get("args", {})], sort_keys=True) for a in recent]
    for period in (1, 2, 3):
        if not all(signature == signatures[index % period] for index, signature in enumerate(signatures)):
            continue
        route = " → ".join(
            f"{a['tool']} {str(a.get('args', {}).get('path', ''))[:100]}" for a in recent[-period:]
        )
        return (
            "重复侦察（最近6个已执行动作出现相同循环，没有写入或新验证）：\n"
            + route[:280]
            + "\n不要再原样重复该路线。保留的正文可直接使用；"
            "用实际报错文字或已读源码中被调用的函数/变量作为搜索关键词，"
            "定位其定义/赋值来源，核对后做最小修改或验证；不要猜测实现。"
        )
    return ""


def _test_fact(action: dict, last_write_step: int) -> str:
    """Keep bounded verification evidence, not a claim about the current workspace."""
    step = action["step"]
    if last_write_step < 0:
        timing = "尚无成功修改"
    else:
        timing = "发生在最近修改之前，修改后尚未重测" if step < last_write_step else "发生在最近修改之后"
    data = action.get("data") or {}
    header = f"上次测试事实（第{step}步；{timing}）"
    if not data:
        return header + "：缺少结构化结果，不能视为通过。"
    passed, failed = data.get("passed"), data.get("failed")
    status = "当时通过" if data.get("verified") else "当时未通过"
    out = f"{header}：{status}，{failed if failed is not None else '?'} failed, {passed if passed is not None else '?'} passed"

    def brief(value, cap):
        text = " ".join(str(value or "").split())
        return text if len(text) <= cap else text[: cap - 1] + "…"

    failures = data.get("failures") or []
    shown = 0
    for failure in failures[:3]:
        location = brief(failure.get("file") or "?", 96)
        line = failure.get("line") or "?"
        message = brief(failure.get("message") or "未提供异常文字", 140)
        entry = f"\n- {location}:{line}: {message}"
        context = failure.get("assertion_context")
        if context:
            room = min(240, TEST_FACT_CAP - len(out) - len(entry) - 48)
            if room > 30:
                entry += f"；断言现场节选: {brief(context, room)}"
        if len(out) + len(entry) + 32 > TEST_FACT_CAP:
            break
        out += entry
        shown += 1
    if shown < len(failures):
        out += f"\n…其余{len(failures) - shown}项未在此摘要展示。"
    elif not failures and not data.get("verified"):
        out += "\n" + brief(data.get("summary"), 240)
    return out


def _navigation_fact(actions: list[dict], recent: list[dict]) -> str:
    """Keep one bounded directory, invalidating it after any attempted write."""
    for action in reversed(actions):
        if action["tool"] in ("edit_file", "write_file"):
            return ""
        if action["tool"] != "read_file" or not action.get("ok"):
            continue
        outline = action.get("data", {}).get("python_navigation")
        if not outline:
            continue
        if any(outline in (m.get("content") or "") for m in recent):
            return ""
        path = str(action.get("args", {}).get("path", "?"))
        if len(path) > 160:
            path = path[:159] + "…"
        fact = f"最近文件导航（{path}，仅位置，非源码；来自最近读取）：\n{outline}"
        return fact[:NAVIGATION_CAP]
    return ""


def build_view(
    messages: list[dict],
    keep_recent_steps: int = DEFAULT_KEEP_RECENT_STEPS,
    *,
    latest_test: dict | None = None,
    last_write_step: int = -1,
    actions: list[dict] | None = None,
) -> list[dict]:
    """返回喂给 decider 的视图：近 keep_recent_steps 步全文，更早压成摘要。

    keep_recent_steps=0/None 表示全部压缩；极大值（如 10**9）≈ 全量视图（关压缩）。
    latest_test由调用方从已执行动作选择；事实只加入决策视图，不修改原始历史。
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
    title = "—— 更早步骤摘要（已压缩）——\n"
    summary_lines = [_one_line(m) for m in old]
    omitted = 0
    while len(title) + sum(len(line) + 1 for line in summary_lines) + 48 > HISTORY_CAP:
        summary_lines.pop(0)
        omitted += 1
    omission = f"[省略更早{omitted}条消息；原历史仍在断点中]\n" if omitted else ""
    compressed = [{"role": "user", "content": title + omission + "\n".join(summary_lines)}]
    # 最新完整测试结果已在近期消息中时无需重复；只给被压缩的测试保留结构化事实。
    recent_test = any(
        m.get("role") == "user" and (m.get("content") or "").startswith("[工具结果 run_tests]")
        for m in recent
    )
    facts = (
        [{"role": "user", "content": _test_fact(latest_test, last_write_step)}]
        if latest_test is not None and not recent_test
        else []
    )
    navigation = _navigation_fact(actions or [], recent)
    navigation_messages = [{"role": "user", "content": navigation}] if navigation else []
    sources = _source_facts(tail_all, recent, actions or [])
    source_messages = [{"role": "user", "content": source} for source in sources]
    cycle = _recon_cycle_fact(actions or [])
    cycle_messages = [{"role": "user", "content": cycle}] if cycle else []
    return head + compressed + facts + navigation_messages + source_messages + cycle_messages + recent


def view_stats(messages: list[dict], keep_recent_steps: int = DEFAULT_KEEP_RECENT_STEPS) -> dict:
    """统计：原始/压缩后的本地估算 token、压缩率。单元测试与离线对比用。"""
    view = build_view(messages, keep_recent_steps)
    full_t = estimate_tokens("".join((m.get("content") or "") for m in messages))
    view_t = estimate_tokens("".join((m.get("content") or "") for m in view))
    rate = (full_t - view_t) / full_t if full_t else 0.0
    return {
        "full_tokens_est": full_t,
        "view_tokens_est": view_t,
        "rate": rate,
        "n_full": len(messages),
        "n_view": len(view),
    }
