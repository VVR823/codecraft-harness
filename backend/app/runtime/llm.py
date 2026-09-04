"""LLM 薄封装（Q4/Q5 决策）。

- 只做一件事：把 messages 发出去、把文本拿回来。不做规划、不做解析。
- OpenAI 兼容端点，默认智谱 GLM-4-Flash（免费起步，Q4）。
- API Key 从 backend/.env 的 ZHIPU_API_KEY 读（python-dotenv），缺失时抛清晰错误。
- 真实调用 + 测试 Fake 都走同一签名：llm_chat(messages) -> str。
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

from ..config import BASE_DIR, LLM_BASE_URL, LLM_MODEL

load_dotenv(BASE_DIR / ".env")


def chat(messages: list[dict], model: str = LLM_MODEL,
         temperature: float = 0.2, max_retries: int = 2) -> str:
    """同步调一次模型，返回文本。失败抛 RuntimeError。

    注：不用 openai 客户端的 max_retries（旧版不支持），改用本地重试循环。
    """
    api_key = os.getenv("ZHIPU_API_KEY", "")
    if not api_key:
        raise RuntimeError(
            "缺少 ZHIPU_API_KEY：请复制 backend/.env.example 为 backend/.env 并填入智谱 API Key"
        )
    # 延迟 import：没装 openai 或不走真 LLM（测试用 Fake）时不影响其他模块
    from openai import OpenAI

    client = OpenAI(api_key=api_key, base_url=LLM_BASE_URL)
    last_err: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature,
            )
            text = (resp.choices[0].message.content or "").strip()
            if not text:
                raise RuntimeError("LLM 返回空内容")
            return text
        except Exception as e:  # noqa: BLE001 - 网络/限流/解析统一重试
            last_err = e
            if attempt < max_retries:
                import time
                time.sleep(1.0 * (attempt + 1))  # 退避：1s, 2s
    raise RuntimeError(f"LLM 调用失败（重试 {max_retries} 次后）: {last_err}")
