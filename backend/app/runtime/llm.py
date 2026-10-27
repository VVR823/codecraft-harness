"""OpenAI 兼容模型客户端的传输与重试层。

本模块只负责发送消息、返回文本和用量，不参与规划或决策解析。运行配置由
Settings 提供；限速、分级退避、调用超时和连续失败冷却在此统一处理。
"""

from __future__ import annotations

import queue
import threading
import time

from ..config import LLM_MODEL

# 传输韧性参数
MIN_CALL_INTERVAL = 1.5  # 调用最小间隔（秒），避免突发请求触发限流
CIRCUIT_BREAK_AFTER = 3  # 连续失败 ≥N 次（跨调用累积）触发熔断
CIRCUIT_COOLDOWN = 300  # 熔断硬冷却（秒）
CALL_TIMEOUT = 120  # 单次调用总超时（秒）：看门狗，防代理吞超时/服务端假活挂死

_fail_streak = 0  # 连续失败计数（跨调用累积，成功清零）
_last_call_ts = 0.0  # 上次调用时间戳（限速用）


def _throttle():
    """请求限速：距上次调用不足 MIN_CALL_INTERVAL 时补齐等待时间。"""
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
        "account": (60, 180, 300),  # 账户级：窗口分钟级，长等
        "rate": (5, 10, 15),  # 请求级：秒级窗口
        "empty": (2, 4, 8),  # 高压空内容：短退避即时重试
        "timeout": (5, 15, 30),  # 单次调用超时：服务端假活/高压，中退避
        "other": (1, 2, 4),  # 网络等：普通退避
    }
    seq = table[kind]
    return seq[min(attempt, len(seq) - 1)]


def _client():
    from .settings import Settings

    settings = Settings.from_env()
    options = settings.options()
    settings.validate_mode(options)
    if options.mode == "offline_demo":
        raise RuntimeError("offline 模式不允许模型请求，请使用离线演示入口")
    from openai import OpenAI

    return OpenAI(
        api_key=settings.api_key.get_secret_value() or "local-no-key",
        base_url=settings.base_url,
        timeout=settings.model_timeout,
        max_retries=0,
    )


def _call_once(client, model: str, messages: list[dict], temperature: float) -> tuple[str, dict]:
    """单次调用（不重试），返回 (text, usage)。空内容按错误抛。

    某些代理或服务端异常不会及时触发 socket 超时，因此再用 daemon 线程和
    join(CALL_TIMEOUT) 限制整个调用。超时进入统一重试链；无法安全终止的线程
    不阻塞进程退出。
    """
    q: queue.Queue = queue.Queue(maxsize=1)

    def _do():
        try:
            q.put(
                (
                    "ok",
                    client.chat.completions.create(model=model, messages=messages, temperature=temperature),
                )
            )
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


def _chat_with_retry(
    model: str, messages: list[dict], temperature: float, max_retries: int
) -> tuple[str, dict]:
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
            _fail_streak = 0  # 成功后清零连续失败计数
            return text, usage
        except Exception as e:  # noqa: BLE001 - 网络/限流/解析统一重试
            last_err = e
            _fail_streak += 1
            if attempt < max_retries:
                kind = _error_kind(e)
                delay = _backoff(kind, attempt)
                if _fail_streak >= CIRCUIT_BREAK_AFTER:
                    delay = max(delay, CIRCUIT_COOLDOWN)
                    print(f"    ⚠️ LLM 连续失败 {_fail_streak} 次，熔断冷却 {CIRCUIT_COOLDOWN}s…", flush=True)
                time.sleep(delay)
    raise RuntimeError(f"LLM 调用失败（重试 {max_retries} 次后）: {last_err}")


def chat(messages: list[dict], model: str = LLM_MODEL, temperature: float = 0.2, max_retries: int = 2) -> str:
    """同步调一次模型，返回文本。失败抛 RuntimeError。"""
    text, _ = _chat_with_retry(model, messages, temperature, max_retries)
    return text


def chat_with_usage(
    messages: list[dict], model: str = LLM_MODEL, temperature: float = 0.2, max_retries: int = 2
) -> tuple[str, dict]:
    """同步调一次模型，返回 (文本, usage)。usage 为 {prompt_tokens, completion_tokens, total_tokens}。"""
    return _chat_with_retry(model, messages, temperature, max_retries)
