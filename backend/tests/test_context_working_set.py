"""A search must not erase the source page needed for a subsequent exact edit."""

import json

import httpx
import pytest

from app.runtime.context import build_view


def pair(tool, args, output):
    return [
        {
            "role": "assistant",
            "content": json.dumps({"thought": f"调用 {tool}", "tool": tool, "args": args, "done": False}),
        },
        {"role": "user", "content": f"[工具结果 {tool}] {output}"},
    ]


def history():
    path = "backend/app/handlers/state_transition.py"
    code = "# context\n" * 12 + "def recover_state():\n    return load_latest_action()\n"
    messages = [{"role": "system", "content": "system"}, {"role": "user", "content": "fix state"}]
    messages += pair("read_file", {"path": path, "offset": 120, "limit": 18}, code)
    messages += pair("search_file", {"path": path, "pattern": "load_latest_action"}, "location 165")
    return messages, path, code


def source_fact(view):
    facts = [m["content"] for m in view if m["content"].startswith("最近源码读取")]
    assert len(facts) == 1
    assert len(facts[0]) <= 6000
    return facts[0]


def test_source_survives_intervening_search_with_exact_location_and_content():
    messages, path, code = history()
    original = json.loads(json.dumps(messages))
    fact = source_fact(build_view(messages, 1))
    assert path in fact and "offset=120" in fact and "limit=18" in fact
    assert code in fact
    assert messages == original  # transient view, checkpoint is the unchanged full history


def test_recent_source_not_duplicated_and_any_write_invalidates_cached_source():
    messages, _, _ = history()
    assert not any(m["content"].startswith("最近源码读取") for m in build_view(messages[:-2], 1))
    for tool in ["edit_file", "write_file"]:
        changed = messages + pair(tool, {"path": "elsewhere.py"}, "[工具错误] rejected")
        assert not any(m["content"].startswith("最近源码读取") for m in build_view(changed, 1))


def test_distinct_source_pages_are_bounded_and_oversized_page_has_honest_truncation():
    messages, _, _ = history()
    huge = "def latest():\n" + "    x = 1\n" * 700
    messages += pair("read_file", {"path": "other.py", "offset": 31, "limit": 701}, huge)
    messages += pair("search_file", {"path": "other.py", "pattern": "latest"}, "found")
    facts = [m["content"] for m in build_view(messages, 1) if m["content"].startswith("最近源码读取")]
    assert len(facts) == 2 and sum(map(len, facts)) <= 6000
    combined = "\n".join(facts)
    assert "other.py" in combined and "def latest():" in combined
    assert "recover_state" in combined and "节选" in combined
    assert "重新读取" in combined


def test_search_history_keeps_query_and_path_without_unbounded_growth():
    messages, path, _ = history()
    for i in range(120):
        messages += pair("search_file", {"path": path, "pattern": f"missing_{i}"}, "not found")
    view = build_view(messages, 1)
    summary = next(m["content"] for m in view if "更早步骤摘要" in m["content"])
    assert len(summary) <= 1600
    assert path in summary and "missing_118" in summary
    assert "省略" in summary


def test_unsuccessful_read_is_never_presented_as_cached_source():
    messages, path, _ = history()
    messages += pair("read_file", {"path": path}, "line exceeds read limit; content not returned")
    messages += pair("search_file", {"path": path, "pattern": "x"}, "not found")
    actions = [{"step": 3, "tool": "read_file", "args": {"path": path}, "ok": False}]
    assert not any(m["content"].startswith("最近源码读取") for m in build_view(messages, 1, actions=actions))


def test_legacy_read_without_source_kind_is_not_guessed_to_be_source():
    messages, path, _ = history()
    actions = [{"step": 1, "tool": "read_file", "args": {"path": path}, "ok": True, "data": {}}]
    assert not any(m["content"].startswith("最近源码读取") for m in build_view(messages, 1, actions=actions))


@pytest.mark.parametrize("content,offset", [("value = 1\n", 999), ("", 1)])
def test_out_of_range_or_empty_read_is_not_returned_source(tmp_path, content, offset):
    from app.tools.registry import ToolRegistry

    (tmp_path / "worker.py").write_text(content, encoding="utf-8")
    args = {"path": "worker.py", "offset": offset, "limit": 10}
    result = ToolRegistry().execute("read_file", tmp_path, args)
    assert result.data.get("source_returned") is False
    messages = [{"role": "system", "content": "system"}, {"role": "user", "content": "goal"}]
    messages += pair("read_file", args, result.output)
    messages += pair("search_file", {"path": "worker.py", "pattern": "value"}, "not found")
    actions = [{"step": 1, "tool": "read_file", "args": args, "ok": result.ok, "data": result.data}]
    assert not any(m["content"].startswith("最近源码读取") for m in build_view(messages, 1, actions=actions))


@pytest.mark.parametrize("restart", [False, True])
def test_sdk_view_keeps_source_after_search_and_restart_until_write(tmp_path, restart):
    from app.runtime.model_client import ModelClient
    from app.runtime.run_service import RunService
    from app.runtime.settings import RunOptions, Settings
    from app.store import db

    source = tmp_path / "source"
    source.mkdir()
    code = "def clean(value):\n    return value\n"
    (source / "worker.py").write_text(code, encoding="utf-8")
    (source / "test_worker.py").write_text(
        "from worker import clean\ndef test_clean():\n    assert clean(' ok ') == 'ok'\n", encoding="utf-8"
    )
    captured = []
    decisions = iter(
        [
            {"tool": "read_file", "args": {"path": "worker.py"}},
            {"tool": "search_file", "args": {"path": "worker.py", "pattern": "clean"}},
            {
                "tool": "edit_file",
                "args": {"path": "worker.py", "old": "return value", "new": "return value.strip()"},
            },
            {"tool": "run_tests", "args": {}},
            {"tool": None, "args": {}, "done": True},
        ]
    )

    def handle(request):
        captured.append(json.loads(request.content)["messages"])
        content = json.dumps({"thought": "working set regression", "done": False, **next(decisions)})
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"role": "assistant", "content": content}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    def factory(settings, options):
        return ModelClient(settings, options, http_client=httpx.Client(transport=httpx.MockTransport(handle)))

    settings = Settings(data_dir=tmp_path / "data", base_url="http://127.0.0.1:9999/v1")
    service = RunService(settings, model_factory=factory)
    run = service.create(
        "t1_single_fix",
        "fix spaces",
        RunOptions(
            mode="local", context_compress=True, keep_recent_steps=1, token_budget=4 if restart else 40
        ),
        source=source,
    )
    result = service.execute(run["run_id"])
    if restart:
        assert result["status"] == "budget_paused"
        service = RunService(settings, model_factory=factory)
        result = service.resume(run["run_id"], new_budget=40)
    assert result["verified"] and result["status"] == "done"
    assert code in source_fact(captured[2]) and "worker.py" in source_fact(captured[2])
    assert not any(m["content"].startswith("最近源码读取") for m in captured[3])
    with service.database():
        history_saved = json.loads(db.last_checkpoint(run["run_id"])["ctx_messages"])
    assert not any(m["content"].startswith("最近源码读取") for m in history_saved)
    assert (source / "worker.py").read_text(encoding="utf-8") == code
