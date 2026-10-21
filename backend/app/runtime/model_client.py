"""OpenAI-compatible transport; no retry that can conceal additional requests."""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from .settings import RunOptions, Settings


@dataclass(frozen=True)
class ModelReply:
    text: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None


def model_error_detail(error: Exception) -> str:
    """Return an allowlisted diagnosis; never stringify errors, bodies or headers."""
    from openai import APIConnectionError, APIResponseValidationError, APIStatusError, APITimeoutError

    if isinstance(error, (APITimeoutError, httpx.TimeoutException)):
        return "model_timeout"
    if isinstance(error, (APIConnectionError, httpx.RequestError)):
        return "model_connection_error"
    if isinstance(error, APIStatusError):
        status = error.status_code
        suffix = f" HTTP {status}" if type(status) is int and 100 <= status <= 599 else ""
        return "model_http_error" + suffix
    if isinstance(error, (APIResponseValidationError, ValueError, IndexError, AttributeError)):
        return "model_response_error"
    return "model_error"


class ModelClient:
    def __init__(self, settings: Settings, options: RunOptions, *, sdk_factory=None, http_client=None):
        settings.validate_mode(options)
        if options.mode == "offline_demo":
            raise ValueError("offline 模式不创建网络客户端")
        if sdk_factory is None:
            from openai import OpenAI

            sdk_factory = OpenAI
        self.options = options
        self._http = http_client or httpx.Client(trust_env=False, follow_redirects=False)
        self.sdk = sdk_factory(
            api_key=settings.api_key.get_secret_value() or "local-no-key",
            base_url=settings.base_url,
            timeout=settings.model_timeout,
            max_retries=0,
            http_client=self._http,
        )

    def chat(self, messages: list[dict], *, response_schema: dict | None = None) -> ModelReply:
        format_args = {}
        if response_schema is not None:
            if self.options.mode != "local" or not self.options.structured_output:
                raise ValueError("response_schema需要显式local structured_output选项")
            format_args["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "agent_decision", "strict": True, "schema": response_schema},
            }
        response = self.sdk.chat.completions.create(
            model=self.options.model, messages=messages, temperature=0.2, **format_args
        )
        text = response.choices[0].message.content or ""
        usage = response.usage
        if usage is None:
            return ModelReply(text)
        counts = [getattr(usage, k, None) for k in ("prompt_tokens", "completion_tokens", "total_tokens")]
        if any(type(v) is not int or v < 0 for v in counts):
            return ModelReply(text)
        return ModelReply(text, *counts)

    def close(self) -> None:
        self.sdk.close()
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
