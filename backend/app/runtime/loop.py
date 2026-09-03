"""自研 agent loop v0（Q11/Q13 落点）。

主循环（全自研，不依赖 LangGraph 状态）：
    1. 喂给决策器：系统提示 + 任务目标 + 历史步骤摘要（上下文由本文件维护）
    2. 决策器返回 AgentStep（真 LLM 或注入的 Fake，同一签名）
    3. done=True → 收尾；否则执行工具（read_file / write_file / run_tests）
    4. 每步：工具结果落 trace（摘要），状态落 checkpoint（Q9 六字段）——断点续跑的基础
    5. 超 MAX_STEPS → paused（等人）；决策连续失败 → failed

工具权限（v0 简化）：
- read_file / write_file 只允许落在 task_dir 内（路径穿越拦截），write 走任务包工作副本（git 管理，可还原）
- run_tests 走轻量沙箱（复制执行，Q8）
"""
from __future__ import annotations

import hashlib
import json
import sys
import uuid
from pathlib import Path

from ..config import LLM_RETRY, MAX_STEPS
from ..store import db
from ..tools.sandbox_exec import run_in_sandbox
from .protocol import AgentStep, StepParseError, parse_step

SYSTEM_PROMPT = """你是一个软件工程师 Agent，正在执行一个代码任务。
每次输出必须是一段 JSON（不要多余文字），格式：
{"thought": "这步在想什么", "tool": "read_file|write_file|run_tests", "args": {"...": "..."}, "done": false}
- read_file: args={"path": "相对任务目录的路径"}
- write_file: args={"path": "...", "content": "..."}，content 是文件全文
- run_tests: args={}（跑任务目录的 pytest）
- 当你确认任务已完成（测试全绿等），输出 {"thought": "...", "done": true}，不带 tool。
工作区 = 任务目录（git 管理）。写完代码后必须 run_tests 验证，红了就继续读文件、修、再测。"""

# 工具结果截断上限（防上下文爆炸的 v0 防线，正式分层压缩在 context.py）
TOOL_OUTPUT_CAP = 3000
READ_FILE_CAP = 6000


class LoopError(Exception):
    pass


def _path_in_workspace(workspace: Path, rel: str) -> Path:
    """解析并校验路径必须落在 workspace 内（路径穿越拦截）。"""
    p = (workspace / rel).resolve()
    if not p.is_relative_to(workspace.resolve()):
        raise LoopError(f"路径越界，拒绝: {rel}")
    return p


def _truncate(text: str, cap: int = TOOL_OUTPUT_CAP) -> str:
    text = text or ""
    return text if len(text) <= cap else text[:cap] + f"\n...[截断 {len(text)-cap} 字符]"


def _ws_hash(workspace: Path) -> str:
    """工作区文件哈希快照（v0 简版；M2 漂移识别正式用）。"""
    h = hashlib.sha256()
    for f in sorted(workspace.rglob("*")):
        if f.is_file() and "__pycache__" not in str(f):
            h.update(f.name.encode())
            h.update(f.read_bytes())
    return h.hexdigest()[:16]


class HarnessLoop:
    """一次 run 的执行主体。decider: (messages: list[dict]) -> AgentStep。"""

    def __init__(self, task_dir: str | Path, goal: str, decider,
                 run_id: str | None = None, task_id: str | None = None):
        self.workspace = Path(task_dir)
        if not self.workspace.is_dir():
            raise LoopError(f"任务目录不存在: {self.workspace}")
        self.goal = goal
        self.decider = decider
        self.run_id = run_id or uuid.uuid4().hex[:12]
        self.task_id = task_id or self.workspace.name
        self.messages: list[dict] = []
        self.done_actions: list[dict] = []

    # ---------- 对外入口 ----------
    def run(self) -> dict:
        """从头执行一次 run。"""
        db.create_run(self.run_id, self.task_id, self.goal)
        db.update_run_status(self.run_id, "running")
        self.messages = self._initial_messages(self.goal, fresh=True)
        return self._run_loop(start_step=1)

    def resume(self) -> dict:
        """从最后 checkpoint 续跑（MVP 底线 2：进程被杀后不重放已完成动作）。"""
        run = db.get_run(self.run_id)
        if run is None:
            raise LoopError(f"run 不存在: {self.run_id}")
        db.update_run_status(self.run_id, "running")
        cp = db.last_checkpoint(self.run_id)
        if cp is None:
            return self.run()
        try:
            self.done_actions = json.loads(cp["done_actions"] or "[]")
        except json.JSONDecodeError:
            self.done_actions = []
        try:
            self.messages = json.loads(cp["ctx_messages"] or "[]")
        except json.JSONDecodeError:
            self.messages = []
        if not self.messages:
            self.messages = self._initial_messages(run["goal"] or self.goal, fresh=False)
        start = int(cp["step"]) + 1
        print(f"[resume] 从 checkpoint step={cp['step']} 续跑，已完成 "
              f"{len(self.done_actions)} 个动作（不重放）")
        return self._run_loop(start_step=start)

    def _initial_messages(self, goal: str, fresh: bool) -> list:
        tail = "工作区已就绪，请开始。" if fresh else "工作区已就绪，请继续你上次未完成的修复。"
        return [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": "任务目标：" + goal + chr(10) + tail}]

    def _run_loop(self, start_step: int) -> dict:
        step = start_step - 1
        try:
            while step < MAX_STEPS:
                step += 1
                act = self._decide_once(step)
                if act.done:
                    db.append_trace(self.run_id, step, "", "", "agent 认为任务已完成",
                                    0, "done")
                    db.update_run_status(self.run_id, "done")
                    self._checkpoint(step)
                    return self._summary("done", steps=step)
                self._execute(act, step)
                self._checkpoint(step)
            db.update_run_status(self.run_id, "paused")
            return self._summary("paused", steps=step)
        except StepParseError as e:
            db.update_run_status(self.run_id, "failed")
            return self._summary("failed", reason=str(e), steps=step)

    # ---------- 决策（带 Q11 重试） ----------
    def _decide_once(self, step: int) -> AgentStep:
        for attempt in range(LLM_RETRY + 1):
            try:
                text = self.decider(list(self.messages))
                return parse_step(text)
            except StepParseError as e:
                if attempt >= LLM_RETRY:
                    raise
                # 把错误喂回模型要求重出（Q11：解析失败重试 ≤LLM_RETRY）
                self.messages.append({"role": "user",
                                      "content": f"你上一步输出不是合法 JSON 决策（{e}）。请只输出一段合规 JSON。"})
        raise StepParseError("unreachable")  # pragma: no cover

    # ---------- 工具执行 ----------
    def _execute(self, act: AgentStep, step: int) -> dict:
        tool = act.tool.value if act.tool else ""
        args = act.args or {}
        verdict = "ok"
        if act.tool is None or act.tool.value == "read_file":
            path = _path_in_workspace(self.workspace, str(args.get("path", "")))
            if not path.is_file():
                raise LoopError(f"文件不存在: {path}")
            content = path.read_text(encoding="utf-8", errors="replace")
            output = _truncate(content, READ_FILE_CAP)
        elif act.tool.value == "write_file":
            path = _path_in_workspace(self.workspace, str(args.get("path", "")))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(str(args.get("content", "")), encoding="utf-8")
            output = f"已写入 {path.relative_to(self.workspace)}（{len(str(args.get('content','')))} 字符）"
        elif act.tool.value == "run_tests":
            r = run_in_sandbox(self.workspace)
            out = (r["stdout"] or "") + ("\n[stderr]\n" + r["stderr"] if r["stderr"] else "")
            tail = "\n".join(out.splitlines()[-25:])  # 只留尾部 summary 区
            output = (f"exit_code={r['exit_code']} timed_out={r['timed_out']} duration={r['duration_s']}s\n"
                      + tail)
            verdict = "fail" if r["exit_code"] not in (0, None) else "ok"
        else:
            raise LoopError(f"未知工具: {tool}")

        db.append_trace(self.run_id, step, tool,
                        json.dumps(args, ensure_ascii=False)[:500],
                        _truncate(output), 0, verdict)
        # 工具结果摘要进入上下文（v0 直拼；正式分层压缩在 context.py）
        self.messages.append({"role": "assistant",
                              "content": f"调用 {tool or '(none)'} {json.dumps(args, ensure_ascii=False)}"})
        self.messages.append({"role": "user",
                              "content": f"[工具结果 {tool}] {_truncate(output, 2000)}"})
        self.done_actions.append({"step": step, "tool": tool,
                                  "args": args, "result_tail": _truncate(output, 300)})
        return {"tool": tool, "output": output}

    # ---------- 存档 ----------
    def _checkpoint(self, step: int) -> None:
        db.save_checkpoint(
            run_id=self.run_id,
            step=step,
            done_actions=self.done_actions,
            ctx_messages=self.messages,
            ctx_summary=json.dumps(self.messages, ensure_ascii=False)[-1500:],
            ws_hash=_ws_hash(self.workspace),
        )

    def _summary(self, status: str, steps: int = 0, reason: str = "") -> dict:
        return {"run_id": self.run_id, "task_id": self.task_id, "status": status,
                "steps": steps, "reason": reason,
                "actions": list(self.done_actions)}
