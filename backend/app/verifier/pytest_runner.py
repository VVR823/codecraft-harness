"""pytest 输出结构化解析（Q14/执行计划 §4 verifier/pytest_runner.py）。

问题：原始 pytest 输出是给人类看的日志，喂给 LLM 又占 token 又难定位。
方案：把 pytest 输出解析成结构化失败信息——"哪个测试挂了、哪个文件第几行、
断言说了什么"，LLM 一眼看到病灶。

依赖沙箱跑出来的文本格式（pytest -q --tb=line）：
    .F
    ================================== FAILURES ===================================
    E   assert 5 == 6
         +  where 5 = add(2, 3)
    C:\\...\\test_calc.py:7: assert 5 == 6
    =========================== short test summary info ===========================
    FAILED test_calc.py::test_bad - assert 5 == 6
    1 failed, 1 passed in 0.01s

本模块只做纯文本解析（好测），执行仍走 sandbox_exec.run_in_sandbox。
"""

from __future__ import annotations

import re
from pathlib import Path

# 长nodeid的摘要可能只有名称，没有" - 错误"；参数ID也可能含空格。
FAILED_LINE = re.compile(r"^(FAILED|ERROR) ([^\r\n]+)\r?$", re.M)
# 统计行里的计数（全绿 / 有红 / 有 error 措辞不同，分开抓）
STAT_PASSED = re.compile(r"(\d+) passed")
STAT_FAILED = re.compile(r"(\d+) failed")
STAT_ERROR = re.compile(r"(\d+) error")
# FAILURES 区的"文件:行号:"定位行（Windows 盘符 C:\\ 前缀也要吃下）
LOC_LINE = re.compile(r"^([A-Za-z]:[^\r\n]*?\.py|[^\r\n]*?\.py):(\d+):[ \t]*(.*)$", re.M)


def parse_pytest_output(stdout: str, exit_code: int | None = None, timed_out: bool = False) -> dict:
    """把 pytest -q --tb=line 的 stdout 解析成结构化结果。

    返回 {ok, exit_code, passed, failed, failures, summary, timed_out}。
    failures 每项 {nodeid, file, line, message}；计数解析不到时为 None。
    """
    out = stdout or ""
    # 1) 保留无错误文字的摘要；详细异常可从 --tb=line 的定位行补全。
    failures = []
    kinds = []
    for m in FAILED_LINE.finditer(out):
        payload = m.group(2)
        failures.append({"nodeid": payload, "file": payload.split("::")[0], "line": None, "message": ""})
        kinds.append(m.group(1))
    # 2) pytest的ERRORS与FAILURES分区分别保留原顺序；同文件的异常也可能来自实现文件。
    locations: dict[str | None, list] = {None: [], "ERROR": [], "FAILED": []}
    error_messages: list[list[str]] = []
    section = None
    for line in out.splitlines():
        heading = re.fullmatch(r"=+\s+(.+?)\s+=+", line)
        if heading:
            section = {"ERRORS": "ERROR", "FAILURES": "FAILED"}.get(heading.group(1), "IGNORE")
        if section == "IGNORE":
            continue
        if section == "ERROR" and re.match(r"^_+ ERROR\b", line):
            error_messages.append([])
        location = LOC_LINE.match(line)
        if location:
            locations[section].append(
                (location.group(1).replace("\\", "/"), int(location.group(2)), location.group(3))
            )
        if section == "ERROR" and line.startswith("E "):
            if not error_messages:
                error_messages.append([])
            error_messages[-1].append(line[2:].strip())
    for failure, kind in zip(failures, kinds):
        detail = ""
        available = locations[kind] or locations[None]
        if available:
            path, line, detail = available.pop(0)
            failure["line"] = line
            failure["file"] = path.rsplit("/", 1)[-1]
        if kind == "ERROR" and error_messages:
            error_detail = "\n".join(error_messages.pop(0))
            detail = detail or error_detail
        payload = failure["nodeid"]
        # 参数ID允许空格、分隔符和未配对括号。优先用真实异常识别消息边界，
        # 不把参数里的“ - ”当错误；短摘要的省略消息仍使用完整traceback。
        separator = None
        for match in re.finditer(" - ", payload):
            message = payload[match.end() :]
            if detail and (
                message == detail or (message.endswith("...") and detail.startswith(message[:-3]))
            ):
                separator = match.start()
                break
        if separator is None:
            separator = payload.find(" - ", payload.rfind("]") + 1)
        failure["nodeid"] = (payload if separator < 0 else payload[:separator]).strip()
        failure["message"] = detail.strip() or ("" if separator < 0 else payload[separator + 3 :].strip())
        if failure["line"] is None:
            failure["file"] = failure["nodeid"].split("::")[0]
    # 3) 计数与统计行
    summary = out.strip().splitlines()[-1] if out.strip() else ""
    m_p = STAT_PASSED.search(summary)
    m_f = STAT_FAILED.search(summary)
    m_e = STAT_ERROR.search(summary)
    passed = int(m_p.group(1)) if m_p else (0 if m_f or m_e else None)
    failed = (int(m_f.group(1)) if m_f else 0) + (int(m_e.group(1)) if m_e else 0)
    return {
        "ok": exit_code == 0,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "passed": passed,
        "failed": failed,
        "failures": failures,
        "summary": summary,
    }


def format_for_llm(parsed: dict, max_failures: int = 6) -> str:
    """把结构化结果压成给 LLM 的紧凑文本（几行内说清病灶）。"""
    if parsed.get("timed_out"):
        return f"run_tests 超时(>{parsed.get('timeout_s', '?')}s 已强杀)。建议检查是否有死循环或卡死测试。"
    if parsed["exit_code"] is None:
        return f"run_tests 执行异常: {parsed.get('summary', '')}"
    ok = parsed["ok"]
    passed = parsed["passed"]
    failed = parsed["failed"]
    if ok:
        return f"run_tests 全绿: {passed} passed" if passed is not None else "run_tests 全绿"
    lines = [f"run_tests 有红: {failed} failed, {passed if passed is not None else '?'} passed"]
    for f in parsed["failures"][:max_failures]:
        loc = f"{f['file']}:{f['line']}" if f["line"] else f["file"]
        msg = (f["message"] or "")[:200]
        lines.append(f"- {f['nodeid']} ({loc}): {msg}")
    n = len(parsed["failures"])
    if n > max_failures:
        lines.append(f"... 另有 {n - max_failures} 个失败未列出")
    return "\n".join(lines)


def run_pytest(task_dir: str | Path, *, baseline_dir=None, timeout_s=120) -> dict:
    """沙箱里跑 pytest 并返回结构化结果（便捷入口，loop 的 run_tests 用它）。"""
    import sys

    from ..tools.sandbox_exec import run_in_sandbox

    # --tb=line：失败信息压成一行式，解析器依赖这个格式
    cmd = [sys.executable, "-m", "pytest", "-q", "--tb=line"]
    from ..runtime.workspace import load_snapshot, verify_baseline

    task_dir = Path(task_dir)
    managed = (task_dir.parent / "manifest.json").is_file()
    if managed:
        try:
            snapshot = load_snapshot(task_dir)
            verify_baseline(snapshot)
            baseline_dir = snapshot.baseline_dir
        except ValueError as exc:
            return {
                "ok": False,
                "verified": False,
                "baseline_ok": False,
                "exit_code": None,
                "timed_out": False,
                "passed": 0,
                "failed": 0,
                "failures": [],
                "summary": str(exc),
            }
    r = run_in_sandbox(task_dir, cmd=cmd, baseline_dir=baseline_dir, timeout=timeout_s)
    parsed = parse_pytest_output(r["stdout"], r["exit_code"], r["timed_out"])
    parsed["duration_s"] = r["duration_s"]
    parsed["baseline_ok"] = True
    parsed["verified"] = bool(parsed["ok"] and not parsed["timed_out"] and (parsed["passed"] or 0) > 0)
    parsed["ok"] = parsed["verified"]
    return parsed
