import sys

from app.tools.sandbox_exec import run_in_sandbox


def test_child_does_not_inherit_model_or_service_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("ZHIPU_API_KEY", "must-not-inherit")
    monkeypatch.setenv("CODECRAFT_API_TOKEN", "must-not-inherit")
    result = run_in_sandbox(
        tmp_path,
        cmd=[
            sys.executable,
            "-c",
            "import os; assert 'ZHIPU_API_KEY' not in os.environ; assert 'CODECRAFT_API_TOKEN' not in os.environ",
        ],
    )
    assert result["exit_code"] == 0, result["stderr"]


def test_child_output_is_bounded(tmp_path):
    result = run_in_sandbox(tmp_path, cmd=[sys.executable, "-c", "print('x'*100000)"])
    assert result["exit_code"] == 0
    assert len(result["stdout"]) < 70_000


def test_large_output_is_drained_but_only_bounded_prefix_retained(tmp_path):
    import sys
    from app.tools.sandbox_exec import run_in_sandbox

    result = run_in_sandbox(
        tmp_path, cmd=[sys.executable, "-c", "import sys;sys.stdout.write('x'*8_000_000)"]
    )
    assert result["exit_code"] == 0
    assert len(result["stdout"]) == 65536
    assert result["output_truncated"] is True
