import json

import pytest

from app.runtime.loop import HarnessLoop
from app.runtime.workspace import prepare_workspace, verify_baseline


def test_repair_prompt_is_compact_and_excludes_disabled_delivery_tools(tmp_path):
    loop = HarnessLoop(tmp_path, "修复业务实现", lambda _: "")
    prompt = loop._system_prompt()
    assert len(prompt) < 2200
    assert "git_push" not in prompt and "install_package" not in prompt
    assert "write_file" in prompt and "edit_file" in prompt
    assert "恢复/重启/断点" in prompt and "状态重建" in prompt
    assert "不要先改下游" in prompt
    delivery = HarnessLoop(tmp_path, "交付", lambda _: "", use_github=True)._system_prompt()
    assert "git_push" in delivery and "gh_create_pr" in delivery


@pytest.mark.parametrize("failed_tool", ["edit_file", "run_tests"])
def test_failed_action_allows_rereading_source_for_recovery(tmp_path, failed_tool):
    source = tmp_path / "source"
    source.mkdir()
    original = "def clean(text):\n    return text\n"
    fixed = "def clean(text):\n    return text.strip()\n"
    (source / "worker.py").write_text(original, encoding="utf-8")
    (source / "test_worker.py").write_text(
        "from worker import clean\n\ndef test_clean():\n    assert clean(' ok ') == 'ok'\n",
        encoding="utf-8",
    )
    snapshot = prepare_workspace(source, "failed-action-read", tmp_path / "data")
    failure = (
        {"tool": "edit_file", "args": {"path": "worker.py", "old": "return text", "new": "def broken(:"}}
        if failed_tool == "edit_file"
        else {"tool": "run_tests", "args": {}}
    )
    steps = iter(
        [
            {"tool": "read_file", "args": {"path": "worker.py"}},
            failure,
            {"tool": "read_file", "args": {"path": "worker.py"}},
            {"tool": "write_file", "args": {"path": "worker.py", "content": fixed}},
            {"tool": "run_tests", "args": {}},
            {"done": True},
        ]
    )
    calls = 0

    def decide(messages):
        nonlocal calls
        calls += 1
        if calls == 4:
            assert "[工具结果 read_file]" in messages[-1]["content"]
            assert original in messages[-1]["content"]
        return json.dumps({"thought": "recover after failed action", **next(steps)})

    result = HarnessLoop(snapshot.root, "修复clean", decide).run()
    assert result["status"] == "done" and result["verified"]
    assert not result["actions"][1]["ok"]
    assert result["actions"][2]["tool"] == "read_file"
    assert result["actions"][-1]["data"]["passed"] == 1
    verify_baseline(snapshot)


@pytest.mark.parametrize(
    "original,fixed",
    [
        (
            'def clean(text):\n    raise NotImplementedError("todo")\n',
            "def clean(text):\n    return text.strip()\n",
        ),
        (
            'def clean(text):\n    """return text"""\n    return text\n',
            'def clean(text):\n    """return text"""\n    return text.strip()\n',
        ),
    ],
)
def test_small_file_guidance_selects_full_write_without_failed_fragment_attempt(tmp_path, original, fixed):
    source = tmp_path / "source"
    source.mkdir()
    (source / "worker.py").write_text(original, encoding="utf-8")
    test = "from worker import clean\n\ndef test_clean():\n    assert clean(' ok ') == 'ok'\n"
    (source / "test_worker.py").write_text(test, encoding="utf-8")
    snapshot = prepare_workspace(source, "small-file", tmp_path / "data")
    calls = 0

    def decide(messages):
        nonlocal calls
        calls += 1
        prompt = messages[0]["content"]
        assert "小实现文件优先用 write_file" in prompt
        assert "改已有代码优先" not in prompt
        if calls == 1:
            step = {"tool": "run_tests", "args": {}}
        elif calls == 2:
            step = {"tool": "read_file", "args": {"path": "worker.py"}}
        elif calls == 3:
            assert original in messages[-1]["content"]
            step = {"tool": "write_file", "args": {"path": "worker.py", "content": fixed}}
        elif calls == 4:
            assert "改其中几行" not in messages[-1]["content"]
            step = {"tool": "run_tests", "args": {}}
        else:
            step = {"done": True}
        return json.dumps({"thought": "small implementation repair", **step})

    result = HarnessLoop(snapshot.root, "修复clean，原测试只读", decide).run()
    assert result["status"] == "done" and result["verified"]
    assert [a["tool"] for a in result["actions"]] == ["run_tests", "read_file", "write_file", "run_tests"]
    assert result["actions"][-1]["data"]["passed"] == 1
    assert (snapshot.root / "worker.py").read_text(encoding="utf-8") == fixed
    assert (source / "worker.py").read_text(encoding="utf-8") == original
    assert (snapshot.root / "test_worker.py").read_text(encoding="utf-8") == test
    verify_baseline(snapshot)


def test_prompt_allows_full_write_after_exact_edit_failure_and_original_tests_verify(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    original = 'def normalize(text):\n    return text.strip(" ")\n'
    fixed = "def normalize(text):\n    return text.strip()\n"
    (source / "worker.py").write_text(original, encoding="utf-8")
    test = (
        "from worker import normalize\n\ndef test_normalize():\n    assert normalize('\\t ok \\n') == 'ok'\n"
    )
    (source / "test_worker.py").write_text(test, encoding="utf-8")
    snapshot = prepare_workspace(source, "full-write", tmp_path / "data")
    steps = iter(
        [
            {"tool": "run_tests", "args": {}},
            {"tool": "read_file", "args": {"path": "worker.py"}},
            {
                "tool": "edit_file",
                "args": {"path": "worker.py", "old": "return text.strip(' ')", "new": "return text.strip()"},
            },
            {"tool": "read_file", "args": {"path": "worker.py"}},
            {"tool": "write_file", "args": {"path": "worker.py", "content": fixed}},
            {"tool": "run_tests", "args": {}},
            {"done": True},
        ]
    )
    seen = []

    def decide(messages):
        seen.append(messages[0]["content"])
        return json.dumps({"thought": "exercise full-write recovery", **next(steps)})

    result = HarnessLoop(snapshot.root, "修复normalize，保留原测试", decide).run()
    assert "完整读取" in seen[0] and "精确匹配失败" in seen[0]
    assert "绝不要用 write_file 整文件覆盖已有文件" not in seen[0]
    assert result["status"] == "done" and result["verified"]
    assert not result["actions"][2]["ok"]
    assert result["actions"][4]["ok"]
    assert result["actions"][-1]["data"]["passed"] == 1
    assert (snapshot.root / "test_worker.py").read_text(encoding="utf-8") == test
    assert (source / "worker.py").read_text(encoding="utf-8") == original
    verify_baseline(snapshot)


def test_first_decision_receives_explicit_read_only_test_boundary(tmp_path, monkeypatch):
    monkeypatch.setattr("app.runtime.loop.MAX_STEPS", 1)
    seen = []

    def decide(messages):
        seen.append(messages[0]["content"])
        return '{"thought":"done","done":true}'

    HarnessLoop(tmp_path, "修复业务实现", decide).run()
    assert "原测试与测试配置只读" in seen[0]
    assert "实现文件" in seen[0]


@pytest.mark.parametrize("tool", ["edit_file", "write_file"])
def test_protected_test_denial_redirects_to_implementation_and_keeps_baseline(tmp_path, tool):
    source = tmp_path / "source"
    source.mkdir()
    (source / "worker.py").write_text("def normalize(text):\n    return text\n", encoding="utf-8")
    test = "from worker import normalize\n\ndef test_normalize():\n    assert normalize(' ok ') == 'ok'\n"
    (source / "test_worker.py").write_text(test, encoding="utf-8")
    snapshot = prepare_workspace(source, "boundary", tmp_path / "data")
    calls = 0

    def decide(messages):
        nonlocal calls
        calls += 1
        if calls == 1:
            args = (
                {"path": "test_worker.py", "old": "== 'ok'", "new": "== ' ok '"}
                if tool == "edit_file"
                else {"path": "test_worker.py", "content": "def test_bypass():\n    assert True\n"}
            )
            return json.dumps({"thought": "try changing test", "tool": tool, "args": args})
        if calls == 2:
            feedback = messages[-1]["content"]
            assert "实现文件" in feedback and "read_file" in feedback
            return json.dumps(
                {"thought": "inspect implementation", "tool": "read_file", "args": {"path": "worker.py"}}
            )
        if calls == 3:
            return json.dumps(
                {
                    "thought": "fix implementation",
                    "tool": "edit_file",
                    "args": {"path": "worker.py", "old": "return text", "new": "return text.strip()"},
                }
            )
        if calls == 4:
            return '{"thought":"verify","tool":"run_tests","args":{}}'
        return '{"thought":"done","done":true}'

    result = HarnessLoop(snapshot.root, "修复normalize，原测试只读", decide).run()
    assert result["status"] == "done" and result["verified"]
    assert not result["actions"][0]["ok"]
    assert result["actions"][-1]["data"]["passed"] == 1
    assert (snapshot.root / "test_worker.py").read_text(encoding="utf-8") == test
    assert (source / "worker.py").read_text(encoding="utf-8").endswith("return text\n")
    verify_baseline(snapshot)
