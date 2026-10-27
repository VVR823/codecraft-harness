"""File locations must survive paging and resume, but never survive a write."""

import json

import httpx
import pytest

from app.runtime.model_client import ModelClient
from app.runtime.run_service import RunService
from app.runtime.settings import RunOptions, Settings
from app.store import db


def navigation(messages):
    facts = [m["content"] for m in messages if m["content"].startswith("最近文件导航")]
    assert len(facts) == 1
    assert len(facts[0]) <= 1400
    return facts[0]


def service_for(tmp_path, steps, captured):
    def handle(request):
        captured.append(json.loads(request.content)["messages"])
        text = json.dumps({"thought": "导航回归", "done": False, **next(steps)})
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"role": "assistant", "content": text}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    def factory(settings, options):
        return ModelClient(settings, options, http_client=httpx.Client(transport=httpx.MockTransport(handle)))

    settings = Settings(data_dir=tmp_path / "data", base_url="http://127.0.0.1:9999/v1")
    return RunService(settings, model_factory=factory), factory


@pytest.mark.parametrize("restart", [False, True])
def test_directory_survives_paging_and_resume_and_is_invalidated_until_fresh_read(tmp_path, restart):
    source = tmp_path / "source"
    source.mkdir()
    content = "# filler\n" * 700 + "def clean(value):\n    return value\n"
    (source / "worker.py").write_text(content, encoding="utf-8")
    (source / "test_worker.py").write_text(
        "from worker import clean\ndef test_clean():\n    assert clean(' ok ') == 'ok'\n", encoding="utf-8"
    )
    captured = []
    steps = iter(
        [
            {"tool": "read_file", "args": {"path": "worker.py"}},
            {"tool": "read_file", "args": {"path": "worker.py", "offset": 701, "limit": 2}},
            {
                "tool": "edit_file",
                "args": {"path": "worker.py", "old": "return value", "new": "return value.strip()"},
            },
            {"tool": "read_file", "args": {"path": "worker.py", "offset": 701, "limit": 2}},
            {"tool": "read_file", "args": {"path": "worker.py"}},
            {"tool": "read_file", "args": {"path": "worker.py", "offset": 701, "limit": 2}},
            {"tool": "run_tests", "args": {}},
            {"tool": None, "args": {}, "done": True},
        ]
    )
    service, factory = service_for(tmp_path, steps, captured)
    run = service.create(
        "t1_single_fix",
        "修复空格",
        RunOptions(
            mode="local", context_compress=True, keep_recent_steps=1, token_budget=4 if restart else 40
        ),
        source=source,
    )
    result = service.execute(run["run_id"])
    if restart:
        assert result["status"] == "budget_paused"
        service = RunService(service.settings, model_factory=factory)
        result = service.resume(run["run_id"], new_budget=40)
    assert result["status"] == "done" and result["verified"]
    fact = navigation(captured[2])
    assert "worker.py" in fact and "clean: 701-702" in fact
    assert "return value" not in fact
    assert not any(m["content"].startswith("最近文件导航") for m in captured[1])
    for i in [3, 4, 5]:
        assert not any(m["content"].startswith("最近文件导航") for m in captured[i])
    assert "clean: 701-702" in navigation(captured[6])
    with service.database():
        raw = json.loads(db.last_checkpoint(run["run_id"])["ctx_messages"])
    assert not any(m["content"].startswith("最近文件导航") for m in raw)
    assert (source / "worker.py").read_text(encoding="utf-8") == content


def test_default_python_read_uses_a_short_prefix_and_keeps_explicit_paging(tmp_path):
    from app.tools.registry import _read_file

    content = "# filler\n" * 700 + "def last():\n    return 42\n"
    (tmp_path / "worker.py").write_text(content, encoding="utf-8")
    result = _read_file(tmp_path, {"path": "worker.py"})
    assert len(result.output) <= 3000
    assert "last: 701-702" in result.output
    assert "当前仅显示开头片段" in result.output
    assert "用 search_file" in result.output
    assert "return 42" not in result.output
    page = _read_file(tmp_path, {"path": "worker.py", "offset": 701, "limit": 2})
    assert page.output.startswith("[行 701-702 / 共 702 行]")
    assert "def last():\n    return 42" in page.output


def test_latest_directory_replaces_the_previous_file_in_the_real_sdk_view(tmp_path, monkeypatch):
    monkeypatch.setattr("app.runtime.loop.MAX_STEPS", 4)
    source = tmp_path / "source"
    source.mkdir()
    for file, function in [("first.py", "alpha"), ("second.py", "beta")]:
        (source / file).write_text(
            "# filler\n" * 700 + f"def {function}():\n    return 42\n", encoding="utf-8"
        )
    captured = []
    steps = iter(
        [
            {"tool": "read_file", "args": {"path": "first.py"}},
            {"tool": "read_file", "args": {"path": "second.py"}},
            {"tool": "read_file", "args": {"path": "second.py", "offset": 701, "limit": 2}},
            {"tool": "run_tests", "args": {}},
        ]
    )
    service, _ = service_for(tmp_path, steps, captured)
    run = service.create(
        "t1_single_fix",
        "读取定义",
        RunOptions(mode="local", context_compress=True, keep_recent_steps=1),
        source=source,
    )
    service.execute(run["run_id"])
    fact = navigation(captured[3])
    assert "second.py" in fact and "beta: 701-702" in fact
    assert "alpha" not in fact and "first.py" not in fact
