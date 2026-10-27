import json

from app.runtime.context import build_view
from app.tools.registry import ToolError, ToolRegistry, ToolResult


def series(tmp_path, calls):
    reg = ToolRegistry()
    messages = [{"role": "system", "content": "system"}, {"role": "user", "content": "goal"}]
    actions = []
    for step, (tool, args) in enumerate(calls, 1):
        try:
            result = reg.execute(tool, tmp_path, args)
        except ToolError as error:
            result = ToolResult(f"[工具错误] {error}", ok=False)
        messages += [
            {
                "role": "assistant",
                "content": json.dumps({"thought": "call", "tool": tool, "args": args, "done": False}),
            },
            {"role": "user", "content": f"[工具结果 {tool}] {result.output}"},
        ]
        actions.append({"step": step, "tool": tool, "args": args, "ok": result.ok, "data": result.data})
    return messages, actions


def test_directory_only_read_does_not_discard_last_real_source_page(tmp_path):
    (tmp_path / "state.py").write_text(
        "# filler\n" * 900 + "def load_record():\n    return latest_record()\n", encoding="utf-8"
    )
    calls = [
        ("read_file", {"path": "state.py", "offset": 901, "limit": 2}),
        ("read_file", {"path": "state.py"}),
        ("search_file", {"path": "state.py", "pattern": "load_record"}),
    ]
    messages, actions = series(tmp_path, calls)
    facts = [
        m["content"]
        for m in build_view(messages, 1, actions=actions)
        if m["content"].startswith("最近源码读取")
    ]
    assert len(facts) == 1 and "return latest_record()" in facts[0]


def test_repeated_nonadjacent_reconnaissance_cycle_is_explained_in_model_view(tmp_path):
    (tmp_path / "state.py").write_text("def load_record():\n    return latest_record()\n", encoding="utf-8")
    calls = [
        ("read_file", {"path": "state.py"}),
        ("search_file", {"path": "state.py", "pattern": "load_record"}),
    ] * 3
    messages, actions = series(tmp_path, calls)
    before = json.loads(json.dumps(messages))
    facts = [
        m["content"] for m in build_view(messages, 1, actions=actions) if m["content"].startswith("重复侦察")
    ]
    assert len(facts) == 1 and len(facts[0]) <= 600
    assert "search_file" in facts[0] and "state.py" in facts[0]
    assert "调用" in facts[0] and "定义" in facts[0]
    assert messages == before


def test_repeated_read_failed_edit_cycle_redirects_before_third_failed_edit(tmp_path):
    (tmp_path / "state.py").write_text("def load_record():\n    return latest_record()\n", encoding="utf-8")
    calls = [
        ("read_file", {"path": "state.py", "offset": 1, "limit": 2}),
        ("edit_file", {"path": "state.py", "old": "missing", "new": "replacement"}),
    ] * 2
    messages, actions = series(tmp_path, calls)
    facts = [
        m["content"] for m in build_view(messages, 1, actions=actions) if m["content"].startswith("重复侦察")
    ]
    assert len(facts) == 1 and len(facts[0]) <= 600
    assert "修改失败" in facts[0] and "同一分页" in facts[0]
    assert "任务目标" in facts[0] and "失败测试" in facts[0]

    successful = [*actions[:-1], {**actions[-1], "ok": True}]
    assert not any(m["content"].startswith("重复侦察") for m in build_view(messages, 1, actions=successful))


def test_repeated_search_and_contained_page_redirects_before_a_third_loop(tmp_path):
    (tmp_path / "state.py").write_text("x = 1\n" * 80, encoding="utf-8")
    calls = [
        ("search_file", {"path": "state.py", "pattern": "load_record"}),
        ("read_file", {"path": "state.py", "offset": 10, "limit": 20}),
        ("search_file", {"path": "state.py", "pattern": "load_record"}),
        ("read_file", {"path": "state.py", "offset": 10, "limit": 10}),
    ]
    messages, actions = series(tmp_path, calls)
    facts = [
        m["content"] for m in build_view(messages, 1, actions=actions) if m["content"].startswith("重复侦察")
    ]
    assert len(facts) == 1 and "同一关键词" in facts[0] and "同一位置" in facts[0]
    assert "任务目标" in facts[0] and "测试文件" in facts[0]

    wider = [*actions[:-1], {**actions[-1], "args": {"path": "state.py", "offset": 10, "limit": 30}}]
    assert not any(m["content"].startswith("重复侦察") for m in build_view(messages, 1, actions=wider))


def test_distinct_read_pages_are_not_a_cycle_and_write_or_test_breaks_one(tmp_path):
    (tmp_path / "state.py").write_text("x=1\n" * 40, encoding="utf-8")
    messages, actions = series(
        tmp_path, [("read_file", {"path": "state.py", "offset": n, "limit": 1}) for n in range(1, 8)]
    )
    assert not any(m["content"].startswith("重复侦察") for m in build_view(messages, 1, actions=actions))
    repeated = [actions[0], actions[1]] * 3
    for tool in ("run_tests", "write_file", "edit_file"):
        reset = repeated + [{"step": 8, "tool": tool, "args": {}, "ok": False}]
        assert not any(m["content"].startswith("重复侦察") for m in build_view(messages, 1, actions=reset))


def test_directory_does_not_revive_source_after_any_attempted_write(tmp_path):
    (tmp_path / "state.py").write_text(
        "# filler\n" * 900 + "def load_record():\n    return latest_record()\n", encoding="utf-8"
    )
    calls = [
        ("read_file", {"path": "state.py", "offset": 901, "limit": 2}),
        ("edit_file", {"path": "state.py", "old": "missing-original", "new": "replacement"}),
        ("read_file", {"path": "state.py"}),
        ("search_file", {"path": "state.py", "pattern": "load_record"}),
    ]
    messages, actions = series(tmp_path, calls)
    assert not any(m["content"].startswith("最近源码读取") for m in build_view(messages, 1, actions=actions))


def test_alternating_implementation_and_test_reads_keep_both_source_pages(tmp_path):
    (tmp_path / "loop.py").write_text("def resume():\n    return latest_action()\n", encoding="utf-8")
    (tmp_path / "test_resume.py").write_text(
        "def test_resume():\n    assert resume() == expected\n", encoding="utf-8"
    )
    calls = [
        ("read_file", {"path": "loop.py", "offset": 1, "limit": 2}),
        ("read_file", {"path": "test_resume.py", "offset": 1, "limit": 2}),
    ] * 3
    messages, actions = series(tmp_path, calls)
    view = build_view(messages, 1, actions=actions)
    facts = [m["content"] for m in view if m["content"].startswith("最近源码读取")]
    assert len(facts) == 1  # latest test page is already present verbatim in the recent window
    combined = "\n".join(m["content"] for m in view)
    assert "loop.py" in combined and "return latest_action()" in combined
    assert "test_resume.py" in combined and "assert resume() == expected" in combined
    assert sum(map(len, facts)) <= 6000
