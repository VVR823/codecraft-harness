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

# summary 区每行一个失败：FAILED <nodeid> - <message>
FAILED_LINE = re.compile(r"^FAILED (\S+?) - (.*)$", re.M)
# 统计行里的计数（全绿 / 有红 / 有 error 措辞不同，分开抓）
STAT_PASSED = re.compile(r"(\d+) passed")
STAT_FAILED = re.compile(r"(\d+) failed")
STAT_ERROR = re.compile(r"(\d+) error")
# FAILURES 区的"文件:行号:"定位行（Windows 盘符 C:\\ 前缀也要吃下）
LOC_LINE = re.compile(r"^([A-Za-z]:[^:\n]*\.py|[^:\n]*\.py):(\d+):", re.M)


def parse_pytest_output(stdout: str, exit_code: int | None = None,
                        timed_out: bool = False) -> dict:
    """把 pytest -q --tb=line 的 stdout 解析成结构化结果。

    返回 {ok, exit_code, passed, failed, failures, summary, timed_out}。
    failures 每项 {nodeid, file, line, message}；计数解析不到时为 None。
    """
    out = stdout or ""
    # 1) 失败列表：summary 区 FAILED 行（nodeid + 一行 message）
    failures = []
    for m in FAILED_LINE.finditer(out):
        nodeid = m.group(1).strip()
        message = m.group(2).strip()
        file = nodeid.split("::")[0] if "::" in nodeid else nodeid
        failures.append({"nodeid": nodeid, "file": file, "line": None,
                         "message": message})
    # 2) 行号：FAILURES 区"文件:行号:"顺序与 summary 一致，按序 zip 补上
    locs = [(m.group(1), int(m.group(2))) for m in LOC_LINE.finditer(out)]
    for i, (fpath, lineno) in enumerate(locs):
        if i < len(failures):
            failures[i]["line"] = lineno
            failures[i]["file"] = Path(fpath).name  # 归一为纯文件名
    # 3) 计数与统计行
    summary = out.strip().splitlines()[-1] if out.strip() else ""
    m_p = STAT_PASSED.search(out)
    m_f = STAT_FAILED.search(out)
    m_e = STAT_ERROR.search(out)
    passed = int(m_p.group(1)) if m_p else (0 if m_f or m_e else None)
    failed = int(m_f.group(1)) if m_f else (int(m_e.group(1)) if m_e else 0)
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
        return f"run_tests 超时(>{parsed.get('timeout_s','?')}s 已强杀)。建议检查是否有死循环或卡死测试。"
    if parsed["exit_code"] is None:
        return f"run_tests 执行异常: {parsed.get('summary','')}"
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


def run_pytest(task_dir: str | Path) -> dict:
    """沙箱里跑 pytest 并返回结构化结果（便捷入口，loop 的 run_tests 用它）。"""
    import sys

    from ..tools.sandbox_exec import run_in_sandbox

    # --tb=line：失败信息压成一行式，解析器依赖这个格式
    cmd = [sys.executable, "-m", "pytest", "-q", "--tb=line"]
    r = run_in_sandbox(task_dir, cmd=cmd)
    parsed = parse_pytest_output(r["stdout"], r["exit_code"], r["timed_out"])
    parsed["duration_s"] = r["duration_s"]
    return parsed
