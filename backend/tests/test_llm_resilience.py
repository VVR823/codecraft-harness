"""llm.py 韧性层单测（O1，2026-09-05）。

覆盖：错误分级（1302/429/空内容）、分级退避、跨调用熔断、成功清零、请求限速。
不碰真网络——monkeypatch llm._call_once 抛/成功，llm.time.sleep 变 no-op（记参数）。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.runtime.llm as llm  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    """每个用例前重置模块级状态 + sleep 变记录器。"""
    llm._fail_streak = 0
    llm._last_call_ts = 0.0
    sleeps: list[float] = []

    def fake_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr(llm.time, "sleep", fake_sleep)
    monkeypatch.setattr(llm, "_client", lambda: object())
    return {"sleeps": sleeps}


# ---------- 错误分级 ----------

def test_error_kind_account_1302():
    e = RuntimeError("Error code: 429 - {'error': {'code': '1302', 'message': '您的账户已达到速率限制，请您控制请求频率'}}")
    assert llm._error_kind(e) == "account"


def test_error_kind_rate_429():
    e = RuntimeError("Error code: 429 - rate limit exceeded")
    assert llm._error_kind(e) == "rate"


def test_error_kind_empty():
    assert llm._error_kind(RuntimeError("LLM 返回空内容")) == "empty"


def test_error_kind_other():
    assert llm._error_kind(RuntimeError("connection reset")) == "other"


# ---------- 分级退避 ----------

def test_backoff_table():
    assert llm._backoff("account", 0) == 60
    assert llm._backoff("account", 1) == 180
    assert llm._backoff("account", 2) == 300
    assert llm._backoff("rate", 0) == 5
    assert llm._backoff("empty", 0) == 2
    assert llm._backoff("other", 0) == 1
    assert llm._backoff("rate", 5) == 15  # 超出序列取末位


# ---------- 重试与熔断 ----------

def test_retry_success_after_failures(_reset_state, monkeypatch):
    """429 失败 2 次后成功：sleep 按 rate 档 5s/10s，成功清零。"""
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise RuntimeError("Error code: 429 - rate limit")
        return "ok", {"total_tokens": 10}

    monkeypatch.setattr(llm, "_call_once", flaky)
    text, usage = llm._chat_with_retry("m", [{"role": "user", "content": "x"}], 0.2, 2)
    assert text == "ok"
    assert _reset_state["sleeps"] == [5, 10]  # rate 档 attempt0/1
    assert llm._fail_streak == 0              # 成功清零


def test_retry_exhausted_raises_with_streak(_reset_state, monkeypatch):
    """3 次全失败抛 RuntimeError，且 streak=3（为下次调用熔断做准备）。"""

    def always_fail(*a, **k):
        raise RuntimeError("Error code: 429 - {'error': {'code': '1302', 'message': '您的账户已达到速率限制'}}")

    monkeypatch.setattr(llm, "_call_once", always_fail)
    with pytest.raises(RuntimeError, match="重试 2 次后"):
        llm._chat_with_retry("m", [{"role": "user", "content": "x"}], 0.2, 2)
    assert _reset_state["sleeps"] == [60, 180]  # account 档
    assert llm._fail_streak == 3


def test_circuit_breaker_after_streak(_reset_state, monkeypatch):
    """上一轮已 3 连败（streak=3），本次第一次失败就熔断硬冷却 300s。"""
    llm._fail_streak = 3  # 模拟上次调用耗尽重试

    def always_fail(*a, **k):
        raise RuntimeError("Error code: 429 - rate limit")

    monkeypatch.setattr(llm, "_call_once", always_fail)
    with pytest.raises(RuntimeError):
        llm._chat_with_retry("m", [{"role": "user", "content": "x"}], 0.2, 2)
    # attempt0 失败：rate 档 5s 被熔断抬到 300s
    assert _reset_state["sleeps"][0] == 300


def test_success_resets_streak_across_calls(_reset_state, monkeypatch):
    """跨调用：上轮连败 streak=3，本轮第一次就成功 → streak 清零，不再熔断。"""
    llm._fail_streak = 3

    def ok(*a, **k):
        return "ok", {}

    monkeypatch.setattr(llm, "_call_once", ok)
    llm._chat_with_retry("m", [{"role": "user", "content": "x"}], 0.2, 2)
    assert llm._fail_streak == 0


# ---------- 请求限速 ----------

def test_throttle_sleeps_on_fast_calls(_reset_state, monkeypatch):
    """两次调用间隔 <1.5s 时 sleep 补齐（gap≈1.5）。"""
    llm._throttle()  # 第一次：_last_call_ts=0 → gap 大，不 sleep
    n_before = len(_reset_state["sleeps"])
    llm._throttle()  # 第二次：紧接上次 → 补 ~1.5s
    assert len(_reset_state["sleeps"]) == n_before + 1
    assert _reset_state["sleeps"][-1] > 1.0
