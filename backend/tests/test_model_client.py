import importlib
from types import SimpleNamespace

import pytest


def modules():
    return importlib.import_module("app.runtime.settings"), importlib.import_module(
        "app.runtime.model_client"
    )


def response(usage):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content='{"thought":"ok","done":true}'))],
        usage=usage,
    )


def test_offline_mode_cannot_construct_network_client(tmp_path):
    cfg, mod = modules()
    with pytest.raises(ValueError, match="offline"):
        mod.ModelClient(cfg.Settings(data_dir=tmp_path), cfg.RunOptions())


def test_client_parses_known_usage_and_disables_sdk_retry(tmp_path):
    cfg, mod = modules()
    calls = []

    class FakeSDK:
        def __init__(self, **kwargs):
            calls.append(kwargs)
            self.chat = SimpleNamespace(
                completions=SimpleNamespace(
                    create=lambda **kw: response(
                        SimpleNamespace(prompt_tokens=9, completion_tokens=1, total_tokens=10)
                    )
                )
            )

        def close(self):
            pass

    with mod.ModelClient(
        cfg.Settings(data_dir=tmp_path, base_url="http://127.0.0.1:1234/v1"),
        cfg.RunOptions(mode="local"),
        sdk_factory=FakeSDK,
    ) as client:
        reply = client.chat([{"role": "user", "content": "test"}])
    assert reply.total_tokens == 10
    assert calls[0]["max_retries"] == 0
    assert calls[0]["timeout"] == 120


def test_missing_usage_stays_unknown(tmp_path):
    cfg, mod = modules()

    class FakeSDK:
        def __init__(self, **kw):
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: response(None)))

        def close(self):
            pass

    with mod.ModelClient(
        cfg.Settings(data_dir=tmp_path, base_url="http://localhost:1234/v1"),
        cfg.RunOptions(mode="local"),
        sdk_factory=FakeSDK,
    ) as client:
        assert client.chat([]).total_tokens is None


def test_actual_sdk_with_in_memory_transport(tmp_path):
    import httpx

    cfg, mod = modules()

    def handle(request):
        assert request.url.host == "127.0.0.1"
        return httpx.Response(
            200,
            json={
                "id": "test-response",
                "object": "chat.completion",
                "created": 1,
                "model": "test",
                "choices": [
                    {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
            },
        )

    transport = httpx.Client(transport=httpx.MockTransport(handle))
    with mod.ModelClient(
        cfg.Settings(data_dir=tmp_path, base_url="http://127.0.0.1:1234/v1"),
        cfg.RunOptions(mode="local"),
        http_client=transport,
    ) as client:
        reply = client.chat([{"role": "user", "content": "test"}])
    assert (reply.text, reply.total_tokens) == ("ok", 7)
