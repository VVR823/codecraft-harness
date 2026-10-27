"""A truncated Python read must locate definitions through the real tool channel."""

import json
import re

import pytest

from app.runtime.loop import HarnessLoop
from app.runtime.protocol import parse_step
from app.store import db
from app.tools.registry import READ_FILE_CAP


def read(loop, path, step=1, **options):
    action = parse_step(
        json.dumps({"thought": "读取源码", "tool": "read_file", "args": {"path": path, **options}})
    )
    result = loop._execute(action, step)
    assert loop.messages[-1]["content"] == "[工具结果 read_file] " + result["output"]
    assert len(result["output"]) <= READ_FILE_CAP
    return result["output"]


def make_loop(tmp_path, content, name="worker.py"):
    (tmp_path / name).write_text(content, encoding="utf-8")
    loop = HarnessLoop(tmp_path, "查看定义", lambda _: "", run_id="python-outline")
    db.create_run(loop.run_id, "python-outline")
    return loop


def test_late_methods_are_locatable_and_the_directory_can_drive_a_precise_read(tmp_path):
    content = "# padding\n" * 700 + (
        "class Worker:\n"
        "    async def restore(self):\n"
        "        return 'fresh_read'\n\n"
        "def finish():\n"
        "    return True\n"
    )
    loop = make_loop(tmp_path, content)
    output = read(loop, "worker.py")
    assert "Worker.restore: 702-703" in output
    assert "finish: 705-706" in output
    assert "fresh_read" not in output
    assert "目录" in output and "offset" in output and "limit" in output
    start, end = map(int, re.search(r"Worker\.restore: (\d+)-(\d+)", output).groups())
    page = read(loop, "worker.py", step=2, offset=start, limit=end - start + 1)
    assert "async def restore(self):\n        return 'fresh_read'" in page
    assert "目录" not in page


def test_large_directory_is_bounded_and_reports_omitted_definitions(tmp_path):
    content = "".join(f"def operation_{i:04d}():\n    return {i}\n\n" for i in range(500))
    output = read(make_loop(tmp_path, content), "worker.py")
    assert "operation_0000: 1-2" in output
    assert "省略" in output
    assert "用 search_file" in output
    assert "def operation_0000():\n    return 0" in output
    assert output.index("def operation_0000():") <= 1200


@pytest.mark.parametrize("name", ["worker.txt", "broken.py"])
def test_non_python_and_invalid_python_keep_the_existing_prefix_and_guidance(tmp_path, name):
    content = "broken = (\n" + "# padding\n" * 700 + "def hidden():\n    pass\n"
    output = read(make_loop(tmp_path, content, name), name)
    assert output.startswith("broken = (\n")
    assert "目录" not in output
    assert "用 search_file" in output


def test_small_python_is_returned_unchanged(tmp_path):
    content = "def worker():\n    return 1\n"
    assert read(make_loop(tmp_path, content), "worker.py") == content


def test_extreme_identifier_does_not_consume_the_body_or_break_the_budget(tmp_path):
    content = "def " + "x" * 7000 + "():\n    pass\n"
    output = read(make_loop(tmp_path, content), "worker.py")
    assert "省略" in output
    assert "def " in output
    assert "用 search_file" in output


def test_directory_line_numbers_match_pages_when_python_contains_form_feeds(tmp_path):
    content = "# padding\n" * 700 + "\f\n" + "def located():\n    return 42\n"
    loop = make_loop(tmp_path, content)
    output = read(loop, "worker.py")
    assert "located: 703-704" in output
    page = read(loop, "worker.py", step=2, offset=703, limit=2)
    assert "def located():\n    return 42" in page
