"""Read navigation must survive the real loop's message budget."""

import json
import re

from app.runtime.loop import HarnessLoop
from app.runtime.protocol import parse_step
from app.store import db
from app.tools.registry import READ_FILE_CAP


def read_message(tmp_path, content, **options):
    (tmp_path / "large.txt").write_text(content, encoding="utf-8")
    loop = HarnessLoop(tmp_path, "读取源码", lambda _: "", run_id="read-budget")
    db.create_run(loop.run_id, "read-budget")
    action = parse_step(
        json.dumps({"thought": "读取", "tool": "read_file", "args": {"path": "large.txt", **options}})
    )
    result = loop._execute(action, 1)
    message = loop.messages[-1]["content"]
    assert message == "[工具结果 read_file] " + result["output"]
    assert len(result["output"]) <= READ_FILE_CAP
    return result["output"]


def test_long_default_read_keeps_search_guidance_in_model_message(tmp_path):
    content = "definition = 1\n" * 1000
    output = read_message(tmp_path, content)
    assert "用 search_file" in output
    assert "offset" in output or "分页精读" in output
    assert output.startswith("definition = 1\n")


def test_shrunken_page_keeps_complete_rows_and_next_offset_in_model_message(tmp_path):
    lines = [f"longline{i:04d} " + "x" * 90 for i in range(1, 101)]
    output = read_message(tmp_path, "\n".join(lines), offset=1, limit=100)
    end = int(re.search(r"\[行 1-(\d+) / 共 100 行\]", output)[1])
    assert 1 <= end < 100
    assert f"offset={end + 1}" in output
    assert output.split("\n（注：", 1)[0].splitlines()[1:] == lines[:end]


def test_oversized_single_line_does_not_claim_a_complete_page(tmp_path):
    output = read_message(tmp_path, "x" * (READ_FILE_CAP + 10), offset=1, limit=1)
    assert "单行" in output and "上限" in output
    assert "search_file" in output
    assert "完整行，无截断" not in output
    assert "[行 1-1" not in output
