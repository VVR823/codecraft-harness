"""LLM 薄封装（Q4/Q5 决策）。

- 只做一件事：把 messages 发出去、把文本拿回来。不做规划、不做解析。
- OpenAI 兼容端点，默认智谱 GLM-4.5-Flash（免费，Q4）。
- API Key 从 backend/.env 的 ZHIPU_API_KEY 读（python-dotenv），缺失时抛清晰错误。
- 真实调用 + 测试 Fake 都走同一签名：chat(messages) -> str。
- run_all 统计 token 用 chat_with_usage(messages) -> (str, dict)（含 prompt/completion tokens）。

## 韧性层（O1，2026-09-05）

免费模型（glm-4.5-flash）的真实痛点不是模型质量，是服务端限流/高压（B6a 背靠背补测暴露）：
- **账户级限流**（智谱 code 1302，窗口可达数分钟）：6 局连跑触发后 90s 冷却都不够，退避 60/180/300s
- **请求级 429**：秒级窗口，退避 5/10/15s
- **空内容/坏输出**：服务端高压时偶发，短退避 2/4/8s 即时重试
- **连续失败熔断**：连败 ≥3 次（跨调用累积）后每次失败硬冷却 300s，避免账户高压时反复撞
- **请求限速**：公开调用最小间隔 1.5s，从源头降低 RPM 峰值（6 局连跑把账户打满的教训）

成功一次清零连败计数。单次调用韧性在 llm 层，整局重试（git restore 重跑）留在脚本层。
"""
from __future__ import annotations

import os
import queue
import threading
import time
from pathlib import Path

from dotenv import load_dotenv

from ..config import BASE_DIR, LLM_BASE_URL, LLM_MODEL

load_dotenv(BASE_DIR / ".env")

# ---- 韧性层参数 ----
MIN_CALL_INTERVAL = 1.5    # 公开调用最小间隔（秒）：防突发密集请求触发限流
CIRCUIT_BREAK_AFTER = 3    # 连续失败 ≥N 次（跨调用累积）触发熔断
CIRCUIT_COOLDOWN = 300     # 熔断硬冷却（秒）
CALL_TIMEOUT = 120         # 单次调用总超时（秒）：看门狗，防代理吞超时/服务端假活挂死

_fail_streak = 0           # 连续失败计数（跨公开调用累积，成功清零）
_last_call_ts = 0.0        # 上次公开调用时间戳（限速用）


def _throttle():
    """请求限速：距上次公开调用不足 MIN_CALL_INTERVAL 则 sleep 补齐。"""
    global _last_call_ts
    now = time.monotonic()
    gap = MIN_CALL_INTERVAL - (now - _last_call_ts)
    if gap > 0:
        time.sleep(gap)
    _last_call_ts = time.monotonic()


def _error_kind(e: Exception) -> str:
    """错误分级：account(账户级1302) | rate(请求级429) | empty(空内容) | timeout | other。"""
    s = str(e)
    if "1302" in s or "账户已达到速率限制" in s:
        return "account"
    if "429" in s or "RateLimit" in type(e).__name__ or "速率限制" in s:
        return "rate"
    if "空内容" in s:
        return "empty"
    if "超时" in s:
        return "timeout"
    return "other"


def _backoff(kind: str, attempt: int) -> float:
    """分级退避（秒）。attempt 从 0 起，超出序列取末位。"""
    table = {
        "account": (60, 180, 300),   # 账户级：窗口分钟级，长等
        "rate":    (5, 10, 15),      # 请求级：秒级窗口
        "empty":   (2, 4, 8),        # 高压空内容：短退避即时重试
        "timeout": (5, 15, 30),      # 单次调用超时：服务端假活/高压，中退避
        "other":   (1, 2, 4),        # 网络等：普通退避
    }
    seq = table[kind]
    return seq[min(attempt, len(seq) - 1)]


def _client():
    api_key = os.getenv("ZHIPU_API_KEY", "")
    if not api_key:
        raise RuntimeError(
            "缺少 ZHIPU_API_KEY：请复制 backend/.env.example 为 backend/.env 并填入智谱 API Key"
        )
    # 延迟 import：没装 openai 或不走真 LLM（测试用 Fake）时不影响其他模块
    from openai import OpenAI

    # timeout=120：防服务端慢响应把 agent 循环挂死（曾现 4.7-flash 限流期请求无限挂起）
    return OpenAI(api_key=api_key, base_url=LLM_BASE_URL, timeout=120)


def _call_once(client, model: str, messages: list[dict],
               temperature: float) -> tuple[str, dict]:
    """单次调用（不重试），返回 (text, usage)。空内容按错误抛。

    O6+ 看门狗：socket 层 timeout=120 在代理吞超时/服务端假活时管不住单次调用
    永久挂起（T4 第六次真机实证：一次 LLM 调用僵死 50 分钟拖垮整个 run）。
    这里套 daemon 线程 + join(CALL_TIMEOUT) 总超时：超时抛错 → 走 _chat_with_retry
    的分级退避重试。悬挂线程不 kill（无法 kill），由进程内残留，但每次调用新建
    client、线程 daemon 化，残留不阻塞后续调用。
    """
    q: queue.Queue = queue.Queue(maxsize=1)

    def _do():
        try:
            q.put(("ok", client.chat.completions.create(
                model=model, messages=messages, temperature=temperature)))
        except BaseException as e:  # noqa: BLE001 - 网络异常统一走重试链
            q.put(("err", e))

    t = threading.Thread(target=_do, daemon=True)
    t.start()
    t.join(CALL_TIMEOUT)
    if t.is_alive():
        raise RuntimeError(f"LLM 调用超时（>{CALL_TIMEOUT}s 未返回，可能服务端假活/代理吞超时）")
    status, payload = q.get()
    if status == "err":
        raise payload
    resp = payload
    text = (resp.choices[0].message.content or "").strip()
    if not text:
        raise RuntimeError("LLM 返回空内容")
    usage = {}
    u = getattr(resp, "usage", None)
    if u is not None:
        usage = {
            "prompt_tokens": getattr(u, "prompt_tokens", 0) or 0,
            "completion_tokens": getattr(u, "completion_tokens", 0) or 0,
            "total_tokens": getattr(u, "total_tokens", 0) or 0,
        }
    return text, usage


def _chat_with_retry(model: str, messages: list[dict],
                     temperature: float, max_retries: int) -> tuple[str, dict]:
    """带韧性层的调用：限速 → 尝试 → 分级退避 → 熔断。

    openai 旧版客户端无 max_retries 参数，故手动重试。失败按 _error_kind 分级退避；
    连续失败跨调用累积（_fail_streak），≥CIRCUIT_BREAK_AFTER 后熔断硬冷却。
    """
    global _fail_streak
    _throttle()
    client = _client()
    last_err: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            text, usage = _call_once(client, model, messages, temperature)
            _fail_streak = 0  # 成功清零连败计数
            return text, usage
        except Exception as e:  # noqa: BLE001 - 网络/限流/解析统一重试
            last_err = e
            _fail_streak += 1
            if attempt < max_retries:
                kind = _error_kind(e)
                delay = _backoff(kind, attempt)
                if _fail_streak >= CIRCUIT_BREAK_AFTER:
                    delay = max(delay, CIRCUIT_COOLDOWN)
                    print(f"    ⚠️ LLM 连续失败 {_fail_streak} 次，熔断冷却 {CIRCUIT_COOLDOWN}s…",
                          flush=True)
                time.sleep(delay)
    raise RuntimeError(f"LLM 调用失败（重试 {max_retries} 次后）: {last_err}")


def chat(messages: list[dict], model: str = LLM_MODEL,
         temperature: float = 0.2, max_retries: int = 2) -> str:
    """同步调一次模型，返回文本。失败抛 RuntimeError。"""
    text, _ = _chat_with_retry(model, messages, temperature, max_retries)
    return text


def chat_with_usage(messages: list[dict], model: str = LLM_MODEL,
                    temperature: float = 0.2,
                    max_retries: int = 2) -> tuple[str, dict]:
    """同步调一次模型，返回 (文本, usage)。usage 为 {prompt_tokens, completion_tokens, total_tokens}。"""
    return _chat_with_retry(model, messages, temperature, max_retries)
