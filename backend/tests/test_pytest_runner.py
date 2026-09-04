"""pytest_runner 结构化解析单元测试（真实跑三个任务包验证解析质量）。"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.verifier.pytest_runner import (  # noqa: E402
    format_for_llm,
    parse_pytest_output,
    run_pytest,
)

TASKS = Path(__file__).resolve().parent.parent / "tasks"


def _soup_fixture(red_n: int, green_n: int, first_failure_line: int | None,
                  nodeid_contains: str, msg_contains: str) -> str:
    """构造解析器期望看到的 pytest --tb=line 文本（红绿自拟）。"""
    dots = "." * green_n + "F" * red_n
    lines = [dots, "=" * 20, "FAILURES", "=" * 20]
    lines.append("E   " + msg_contains)
    lines.append(f"tests/sample.py:{first_failure_line}: " + msg_contains)
    lines.extend(["=" * 20, "short test summary info", "=" * 20])
    for i in range(red_n):
        lines.append(f"FAILED tests/sample.py::{nodeid_contains}_{i} - " + msg_contains)
    lines.append(f"{red_n} failed, {green_n} passed in 0.01s")
    return "\n".join(lines)


# ---------- parse_pytest_output：纯文本解析 ----------

def test_parse_green():
    out = "..\n2 passed in 0.02s"
    r = parse_pytest_output(out, exit_code=0)
    assert r["ok"] is True
    assert r["passed"] == 2 and r["failed"] == 0
    assert r["failures"] == []


def test_parse_red_one():
    out = _soup_fixture(1, 1, 7, "test_bad", "assert 5 == 6")
    r = parse_pytest_output(out, exit_code=1)
    assert r["ok"] is False and r["failed"] == 1 and r["passed"] == 1
    f = r["failures"][0]
    assert f["nodeid"] == "tests/sample.py::test_bad_0"
    assert f["line"] == 7
    assert "assert 5 == 6" in f["message"]


def test_parse_line_zip_off_by_one_keeps_ok():
    """行号区与失败数不匹配（异常输出）也不崩，line 可为 None。"""
    out = _soup_fixture(2, 0, 3, "x", "boom")
    r = parse_pytest_output(out, exit_code=1)
    assert len(r["failures"]) == 2
    assert r["failed"] == 2


def test_parse_timed_out():
    r = parse_pytest_output("", exit_code=None, timed_out=True)
    assert r["timed_out"] is True
    assert "超时" in format_for_llm(r)


def test_format_for_llm_red():
    out = _soup_fixture(1, 1, 7, "test_bad", "assert 5 == 6")
    r = parse_pytest_output(out, exit_code=1)
    txt = format_for_llm(r)
    assert "1 failed, 1 passed" in txt
    assert "sample.py:7" in txt
    assert "test_bad_0" in txt


# ---------- run_pytest：真实沙箱跑三个任务包 ----------

@pytest.mark.parametrize("task_name,exp_red,exp_green", [
    ("t1_single_fix", 1, 1),
    ("t2_missing_fn", 4, 0),   # 全红：函数体被清空，所有用例炸
    ("t3_cross_file", 3, 2),
])
def test_run_pytest_on_real_tasks(task_name, exp_red, exp_green):
    """bug 态任务包：沙箱实跑，解析出的红绿数必须与设计一致。"""
    r = run_pytest(TASKS / task_name)
    assert r["ok"] is False
    assert r["failed"] == exp_red, f"{task_name} 红数不符: {r['failures']}"
    assert r["passed"] == exp_green
    # 每个失败都该有 file + 至少一个带行号
    for f in r["failures"]:
        assert f["file"].endswith(".py")
    assert any(f["line"] is not None for f in r["failures"])
    # 给 LLM 的文本是紧凑多行的，不该是 100 行原始日志
    txt = format_for_llm(r)
    assert len(txt.splitlines()) <= exp_red + 2


def test_run_pytest_ok_after_fix_in_copy():
    """在副本里修好 T1 再跑 → 全绿分支的格式。"""
    import shutil
    import tempfile

    src = TASKS / "t1_single_fix"
    with tempfile.TemporaryDirectory() as td:
        work = Path(td) / "t1"
        shutil.copytree(src, work)
        (work / "utils.py").write_text(
            'def trim_whitespace(text: str) -> str:\n    return text.strip()\n',
            encoding="utf-8")
        r = run_pytest(work)
        assert r["ok"] is True and r["passed"] == 2
        assert "全绿" in format_for_llm(r)
