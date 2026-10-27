"""The repair agent must receive actual failure details, even with long pytest IDs."""

import pytest

from app.verifier.pytest_runner import format_for_llm, parse_pytest_output, run_pytest


@pytest.mark.parametrize(
    "parameter_id",
    ["a - b", "a - b " + "long_parameter_" * 20, "a] - b " + "long_parameter_" * 20],
)
def test_real_parameter_id_separator_is_not_mistaken_for_error_message(tmp_path, parameter_id):
    (tmp_path / "test_parameter.py").write_text(
        "import pytest\n"
        f"@pytest.mark.parametrize('value', [1], ids=[{parameter_id!r}])\n"
        "def test_case(value):\n    assert False, 'actual failure - detail'\n",
        encoding="utf-8",
    )
    result = run_pytest(tmp_path)
    failure = result["failures"][0]
    assert failure["nodeid"] == f"test_parameter.py::test_case[{parameter_id}]"
    assert failure["line"] == 4
    assert "actual failure - detail" in failure["message"]


def test_real_long_pytest_name_keeps_location_and_assertion(tmp_path):
    name = "test_resume_reads_current_file_after_the_last_failed_repair_" + "without_stale_state_" * 8
    (tmp_path / "test_report.py").write_text(
        f"def {name}():\n    assert False, 'fresh read required after failed edit'\n", encoding="utf-8"
    )
    result = run_pytest(tmp_path)
    assert result["failed"] == 1 and result["passed"] == 0 and not result["verified"]
    assert len(result["failures"]) == 1
    failure = result["failures"][0]
    assert failure["nodeid"] == f"test_report.py::{name}"
    assert failure["line"] == 2 and failure["file"] == "test_report.py"
    feedback = format_for_llm(result)
    assert name in feedback and "test_report.py:2" in feedback
    assert "fresh read required after failed edit" in feedback


def test_real_failure_includes_bounded_read_only_test_source_excerpt(tmp_path):
    (tmp_path / "test_resume.py").write_text(
        "def helper():\n"
        "    return 'stale'\n\n"
        "def test_resume_after_failed_edit():\n"
        "    observed = helper()\n"
        "    assert observed == 'fresh', 'resume must read current state'\n",
        encoding="utf-8",
    )
    result = run_pytest(tmp_path)
    failure = result["failures"][0]
    excerpt = failure["test_source_excerpt"]
    assert "def test_resume_after_failed_edit" in excerpt
    assert "assert observed == 'fresh'" in excerpt
    assert len(excerpt) <= 700
    feedback = format_for_llm(result)
    assert "测试源码节选（只读）" in feedback
    assert "def test_resume_after_failed_edit" in feedback
    assert "assert observed == 'fresh'" in feedback
    assert "resume/restore/checkpoint/rebuild" in feedback
    assert "不要先改拦截器或错误文案" in feedback


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_summary_without_message_keeps_parameter_spaces_and_does_not_invent_details(newline):
    nodeid = "tests/test_recovery.py::test_reads_again[label with spaces]"
    result = parse_pytest_output(f"FAILED {nodeid}{newline}1 failed in 0.01s{newline}", exit_code=1)
    assert result["failures"] == [
        {"nodeid": nodeid, "file": "tests/test_recovery.py", "line": None, "message": ""}
    ]
    assert nodeid in format_for_llm(result)


def test_two_failures_in_one_test_file_keep_their_actual_traceback_order():
    output = (
        "src/calc.py:5: ValueError: broken implementation\n"
        "tests/test_calc.py:20: AssertionError: wrong value\n"
        "FAILED tests/test_calc.py::test_implementation\n"
        "FAILED tests/test_calc.py::test_assertion\n"
        "2 failed in 0.01s\n"
    )
    result = parse_pytest_output(output, exit_code=1)
    assert [(f["file"], f["line"]) for f in result["failures"]] == [("calc.py", 5), ("test_calc.py", 20)]
    assert "broken implementation" in result["failures"][0]["message"]
    assert "wrong value" in result["failures"][1]["message"]


def test_mixed_failed_and_error_results_are_both_visible_and_counted():
    output = (
        "FAILED tests/test_one.py::test_one - AssertionError: wrong result\n"
        "ERROR tests/test_two.py::test_two - RuntimeError: fixture setup failed\n"
        "1 failed, 2 passed, 1 error in 0.01s\n"
    )
    result = parse_pytest_output(output, exit_code=1)
    assert result["failed"] == 2 and result["passed"] == 2
    assert [f["nodeid"] for f in result["failures"]] == [
        "tests/test_one.py::test_one",
        "tests/test_two.py::test_two",
    ]
    assert "fixture setup failed" in format_for_llm(result)


def test_counts_come_from_final_statistics_not_assertion_text():
    output = (
        "tests/test_one.py:4: AssertionError: previous attempt had 99 failed and 88 passed\n"
        "FAILED tests/test_one.py::test_one - AssertionError: prior counts differ\n"
        "1 failed, 2 passed in 0.01s\n"
    )
    result = parse_pytest_output(output, exit_code=1)
    assert result["failed"] == 1 and result["passed"] == 2


def test_collection_error_without_message_is_not_an_empty_failure_list():
    result = parse_pytest_output("ERROR tests/test_import.py\n1 error in 0.01s\n", exit_code=2)
    assert result["failed"] == 1 and result["passed"] == 0
    assert result["failures"][0]["file"] == "tests/test_import.py"
    assert "tests/test_import.py" in format_for_llm(result)


def test_real_setup_error_and_failure_keep_their_own_traceback_details(tmp_path):
    error_name = "test_setup_error_" + "long_name_" * 10
    failed_name = "test_assertion_failure_" + "long_name_" * 10
    (tmp_path / "test_mixed.py").write_text(
        "import pytest\n@pytest.fixture\ndef broken_fixture():\n"
        "    raise RuntimeError('setup unavailable')\n"
        f"def {error_name}(broken_fixture):\n    pass\n"
        f"def {failed_name}():\n    assert False, 'actual assertion failed'\n",
        encoding="utf-8",
    )
    result = run_pytest(tmp_path)
    assert result["failed"] == 2 and result["passed"] == 0 and not result["verified"]
    by_name = {f["nodeid"].split("::")[-1]: f for f in result["failures"]}
    # pytest --tb=line omits the setup location: retain its message without inventing a line number.
    assert by_name[error_name]["line"] is None and "setup unavailable" in by_name[error_name]["message"]
    assert by_name[failed_name]["line"] == 8 and "actual assertion failed" in by_name[failed_name]["message"]


def test_warning_location_is_not_reported_as_a_setup_error_location():
    output = (
        "================ ERRORS ================\n"
        "_ ERROR at setup of test_example _\n"
        "E   RuntimeError: setup unavailable\n"
        "================ warnings summary ================\n"
        "tests/warnings.py:99: UserWarning: a separate warning\n"
        "================ short test summary info ================\n"
        "ERROR tests/test_example.py::test_example\n"
        "1 warning, 1 error in 0.01s\n"
    )
    failure = parse_pytest_output(output, exit_code=1)["failures"][0]
    assert failure["line"] is None and failure["file"] == "tests/test_example.py"
    assert failure["message"] == "RuntimeError: setup unavailable"
