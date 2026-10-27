"""Critical test evidence must survive reads, edits and checkpoint recovery."""

import json

import httpx
import pytest

from app.runtime.model_client import ModelClient
from app.runtime.run_service import RunService
from app.runtime.settings import RunOptions, Settings
from app.store import db


def fact(messages):
    facts = [m["content"] for m in messages if m["content"].startswith("上次测试事实")]
    assert len(facts) == 1
    assert len(facts[0]) <= 600
    return facts[0]


def service_for(tmp_path, steps, captured, *, test_body=None):
    source = tmp_path / "source"
    source.mkdir()
    (source / "worker.py").write_text("def clean(value):\n    return value\n", encoding="utf-8")
    (source / "test_worker.py").write_text(
        test_body
        or "from worker import clean\n\n\ndef test_spaces():\n"
        "    assert clean(' ok ') == 'ok', 'specific_failure_after_read'\n",
        encoding="utf-8",
    )
    decisions = iter(steps)

    def handle(request):
        captured.append(json.loads(request.content)["messages"])
        text = json.dumps({"thought": "test evidence retention", "done": False, **next(decisions)})
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
    return RunService(settings, model_factory=factory), source, factory


@pytest.mark.parametrize("restart", [False, True])
def test_latest_test_evidence_survives_reads_and_restart_but_is_replaced_after_green(tmp_path, restart):
    captured = []
    steps = [
        {"tool": "run_tests", "args": {}},
        {"tool": "read_file", "args": {"path": "worker.py"}},
        {
            "tool": "edit_file",
            "args": {"path": "worker.py", "old": "return value", "new": "return value.strip()"},
        },
        {"tool": "read_file", "args": {"path": "worker.py"}},
        {"tool": "run_tests", "args": {}},
        {"tool": "read_file", "args": {"path": "worker.py"}},
        {"tool": None, "args": {}, "done": True},
    ]
    service, source, factory = service_for(tmp_path, steps, captured)
    run = service.create(
        "t1_single_fix",
        "修复clean",
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
    after_read = fact(captured[2])
    assert "test_worker.py:5" in after_read and "specific_failure_after_read" in after_read
    assert "尚无成功修改" in after_read
    assert "尚未重测" in fact(captured[3]) and "最近修改之前" in fact(captured[4])
    after_green = fact(captured[6])
    assert "当时通过" in after_green and "1 passed" in after_green
    assert "specific_failure_after_read" not in "\n".join(m["content"] for m in captured[6])
    with service.database():
        raw = json.loads(db.last_checkpoint(run["run_id"])["ctx_messages"])
    assert not any(m["content"].startswith("上次测试事实") for m in raw)
    assert "specific_failure_after_read" in "\n".join(m["content"] for m in raw)
    assert (source / "worker.py").read_text(encoding="utf-8").endswith("return value\n")


def test_many_long_failures_keep_bounded_locations_and_explicit_omission(tmp_path, monkeypatch):
    monkeypatch.setattr("app.runtime.loop.MAX_STEPS", 3)
    captured = []
    service, source, _ = service_for(
        tmp_path,
        [
            {"tool": "run_tests", "args": {}},
            {"tool": "read_file", "args": {"path": "worker.py"}},
            {"done": True},
        ],
        captured,
        test_body="import pytest\n@pytest.mark.parametrize('value', range(8))\n"
        "def test_expected(value):\n    assert value == 10, 'failure_detail ' + 'x' * 400\n",
    )
    run = service.create(
        "t1_single_fix",
        "定位失败",
        RunOptions(mode="local", context_compress=True, keep_recent_steps=1),
        source=source,
    )
    result = service.execute(run["run_id"])
    assert not result["verified"]
    evidence = fact(captured[2])
    assert "8 failed" in evidence and "test_worker.py:4" in evidence
    assert "failure_detail" in evidence and "其余" in evidence


def test_old_green_result_after_an_edit_is_labeled_stale_and_cannot_finish(tmp_path, monkeypatch):
    monkeypatch.setattr("app.runtime.loop.MAX_STEPS", 4)
    captured = []
    service, source, _ = service_for(
        tmp_path,
        [
            {"tool": "run_tests", "args": {}},
            {
                "tool": "edit_file",
                "args": {"path": "worker.py", "old": "return value", "new": "return value.strip()"},
            },
            {"tool": "read_file", "args": {"path": "worker.py"}},
            {"done": True},
        ],
        captured,
        test_body="from worker import clean\ndef test_identity():\n    assert clean('plain') == 'plain'\n",
    )
    run = service.create(
        "t1_single_fix",
        "修改后必须重测",
        RunOptions(mode="local", context_compress=True, keep_recent_steps=1),
        source=source,
    )
    result = service.execute(run["run_id"])
    assert result["status"] == "paused" and not result["verified"]
    previous_green = fact(captured[3])
    assert "当时通过" in previous_green and "尚未重测" in previous_green
