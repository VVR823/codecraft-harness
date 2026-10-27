"""Retain real pytest evaluation diagnostics, not just static assertion messages."""

import json

import httpx

from app.runtime.model_client import ModelClient
from app.runtime.run_service import RunService
from app.runtime.settings import RunOptions, Settings
from app.verifier.pytest_runner import format_for_llm, parse_pytest_output, run_pytest


def test_real_evaluated_values_survive_formatting_in_original_failure_order(tmp_path):
    (tmp_path / "test_values.py").write_text(
        "def test_first():\n"
        "    actual = 'first_observed_rejection'\n"
        "    assert actual.startswith('expected'), 'same static assertion'\n"
        "def test_second():\n"
        "    actual = 'second_observed_rejection'\n"
        "    assert actual.startswith('expected'), 'same static assertion'\n",
        encoding="utf-8",
    )
    result = run_pytest(tmp_path)
    assert result["failed"] == 2 and result["passed"] == 0 and not result["verified"]
    first, second = result["failures"]
    assert "first_observed_rejection" in first.get("assertion_context", "")
    assert "second_observed_rejection" in second.get("assertion_context", "")
    assert "second_observed_rejection" not in first["assertion_context"]
    assert "same static assertion" in first["message"]
    feedback = format_for_llm(result)
    assert "first_observed_rejection" in feedback and "second_observed_rejection" in feedback
    assert len(feedback.splitlines()) <= 4


def test_large_assertion_context_is_bounded_and_warnings_do_not_supply_context():
    output = (
        "=== FAILURES ===\n"
        "E   AssertionError: static assertion\n"
        " + where actual = '" + "very_long_value_" * 500 + "'\n"
        "test_values.py:3: AssertionError: static assertion\n"
        "=== warnings summary ===\n"
        " + where actual = 'unrelated_warning'\n"
        "warning.py:4: warning value\n"
        "=== short test summary info ===\n"
        "FAILED test_values.py::test_value - AssertionError: static assertion\n"
        "1 failed in 0.01s\n"
    )
    result = parse_pytest_output(output, exit_code=1)
    context = result["failures"][0].get("assertion_context", "")
    assert "very_long_value" in context and "截断" in context
    assert len(context) <= 600
    assert "unrelated_warning" not in format_for_llm(result)
    assert len(format_for_llm(result)) < 1000


def test_captured_stdout_cannot_become_the_next_failures_assertion_context(tmp_path):
    (tmp_path / "test_capture.py").write_text(
        "def test_first():\n"
        "    print('+ where actual = captured_foreign_value')\n"
        "    assert False, 'first assertion'\n"
        "def test_second():\n"
        "    raise RuntimeError('second error without assertion')\n",
        encoding="utf-8",
    )
    result = run_pytest(tmp_path)
    assert result["failed"] == 2
    second = result["failures"][1]
    assert "second error without assertion" in second["message"]
    assert not second.get("assertion_context")
    feedback = format_for_llm(result)
    second_failure = next(
        line for line in feedback.splitlines() if line.startswith("- test_capture.py::test_second")
    )
    assert "captured_foreign_value" not in second_failure


def test_assertion_value_reaches_real_sdk_after_test_result_is_compressed(tmp_path, monkeypatch):
    monkeypatch.setattr("app.runtime.loop.MAX_STEPS", 3)
    source = tmp_path / "source"
    source.mkdir()
    (source / "worker.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    (source / "test_worker.py").write_text(
        "def test_context():\n"
        "    observed = 'diagnostic_survives_read'\n"
        "    assert observed.startswith('expected'), 'same static assertion'\n",
        encoding="utf-8",
    )
    steps = iter(
        [
            {"tool": "run_tests", "args": {}},
            {"tool": "read_file", "args": {"path": "worker.py"}},
            {"tool": None, "args": {}, "done": True},
        ]
    )
    requests = []

    def handle(request):
        requests.append(json.loads(request.content)["messages"])
        content = json.dumps({"thought": "保留报错现场", "done": False, **next(steps)})
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"role": "assistant", "content": content}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    def factory(settings, options):
        return ModelClient(settings, options, http_client=httpx.Client(transport=httpx.MockTransport(handle)))

    service = RunService(
        Settings(data_dir=tmp_path / "data", base_url="http://127.0.0.1:9999/v1"), model_factory=factory
    )
    run = service.create(
        "t1_single_fix",
        "保留失败证据",
        RunOptions(mode="local", context_compress=True, keep_recent_steps=1),
        source=source,
    )
    result = service.execute(run["run_id"])
    assert not result["verified"]
    facts = [m["content"] for m in requests[2] if m["content"].startswith("上次测试事实")]
    assert len(facts) == 1 and len(facts[0]) <= 600
    assert "diagnostic_survives_read" in facts[0] and "same static assertion" in facts[0]
