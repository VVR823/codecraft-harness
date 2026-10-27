"""单 Agent 代码修复执行循环。

主循环（全自研，不依赖 LangGraph 状态）：
    1. 喂给决策器：系统提示 + 任务目标 + 历史步骤摘要（上下文由本文件维护）
    2. 决策器返回 AgentStep（真 LLM 或注入的 Fake，同一签名）
    3. done=True → 收尾；否则执行工具（read_file / write_file / run_tests）
    4. 每步将工具结果写入 trace，并保存可恢复的 checkpoint
    5. 超 MAX_STEPS → paused（等人）；决策连续失败 → failed

工具权限：
- 文件工具只能访问任务工作区，写入发生在可恢复的副本中
- run_tests 在复制出的临时目录执行
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

from ..config import LLM_RETRY, MAX_STEPS
from ..store import db
from ..logging_config import log_event
from ..tools import approval
from ..tools.registry import (  # noqa: E402
    READ_FILE_CAP,
    Perm,
    ToolError,
    ToolResult,
    describe_tools,
    list_tool_names,
    ToolRegistry,
)
from .context import build_view
from .plan import PLAN_PROMPT, PLAN_RETRY, AgentPlan, parse_plan, plan_to_text
from .protocol import AgentStep, StepParseError, parse_step
from ..skills.skill_registry import match_skills

_SYSTEM_HEAD = """你是一个软件工程师 Agent，正在执行一个代码任务。
【修复边界】只修改业务实现文件。原测试与测试配置只读：不得修改测试断言、删除测试或改配置绕过失败。
每次输出必须是一段 JSON（不要多余文字），格式：
{"thought": "这步在想什么", "tool": "read_file|edit_file|write_file|run_tests", "args": {"...": "..."}, "done": false}
- 当你确认任务已完成（测试全绿等），输出 {"thought": "...", "tool": null, "args": {}, "done": true}，不再调用工具。
【工作方法】第一步 run_tests。根据失败信息，read_file 读取被测函数的实现文件（路径相对任务目录）。
测试可读、只读；修实现，不改预期值。小文件读全文，大文件 search_file 定位后用 offset/limit 分页精读，避免重复通读。
恢复/重启/断点类失败应先沿失败测试的调用链检查状态重建（resume/restore/checkpoint），不要先改下游拦截器或错误文案。
【关键规则】已完整读取、无截断且不超过100行的小实现文件优先用 write_file，content 必须是修改后的文件全文。
完整写回必须保留未修改的 import、函数与文档，不得省略内容；大文件或未完整读取的文件仍用局部 edit_file。
edit_file 的 old 必须逐字复制原文且唯一；new 只替换 old 的范围，保留每行缩进，不要用完整函数替换函数体的一行。
若精确匹配失败或候选语法错误，先 read_file 核对，再按上述文件大小规则选择写入方式，不要重复失败的片段。
原测试与配置始终只读；任何写入后都必须 run_tests 验证，全绿才能 done。"""

SYSTEM_PROMPT = _SYSTEM_HEAD + "\n可用工具（注册表生成，与执行校验同源）:\n" + describe_tools()

# 单条工具结果限长；跨步骤压缩由 context.py 处理。
TOOL_OUTPUT_CAP = 3000

# ---- GitHub 交付模式 ----
# 显式开启后才向模型注入交付顺序；工具执行还受注册表权限和环境门闩约束。
GITHUB_SYSTEM_EXTRA = """【GitHub 交付流程】（本任务已开启 --github 交付模式）
代码修复到 run_tests 全绿后，按顺序完成交付，把改动变成 GitHub 上的一个 PR：
1. git_branch {"branch": "<描述性分支名，如 fix/split-csv-quoting>"} —— 从当前 HEAD 新建分支并切换
2. （修复代码在上文已完成且 run_tests 全绿，则跳过改码；若还没修完，先 edit_file → run_tests 全绿）
3. git_commit {"message": "<约定式提交信息，如 fix: 支持引号内逗号>"} —— 提交当前分支上的全部改动
4. git_push {} —— 推送到远端 origin
5. gh_create_pr {"title": "...", "body": "..."} —— 给当前分支向 main 开 PR
PR 创建成功（输出含 github.com 链接）后，输出 done 收尾。交付动作本身就是任务产出，
不需要在交付后再跑 run_tests（最后一次写文件后的全绿已验证过）。
若某一步失败，工具会返回具体原因，先 read_file / git 状态确认再重试，不要盲目重复同一步。"""

# ---- 空转检测 ----
# 连续侦察但没有写入或测试时记录 trace；只有显式开启 stall_warning 才注入提醒。
STALL_LIMIT = 3  # 连续多少个零进展动作判空转
PROGRESS_TOOLS = (
    "edit_file",
    "write_file",
    "run_tests",
    "git_branch",
    "git_commit",
    "git_push",
    "gh_create_pr",
)  # 能推进或验证任务的动作，包含完整 GitHub 交付链
STALL_MSG = (
    "（系统提醒）你已连续 {n} 步只做侦察（{tools}）没有写文件或跑测试。"
    "如果已掌握足够信息，请按文件大小选择 write_file/edit_file 修改实现或 run_tests 验证，别再只读。"
)

# ---- 顺序翻页护栏 ----
# 连续逐页读取大文件会快速挤占上下文。该规则与重复读取拦截互补：一个限制
# 顺序翻页，另一个限制完全相同的读取。
PAGE_WALK_LIMIT = 3  # 同文件连续顺序翻页 ≥3 页（仍无写/测）→ 拦截一次
PAGE_WALK_MSG = (
    "检测到你正在逐页顺序通读 {path}（已连续第 {n} 页仍是纯读取，"
    "没有写文件或跑测试）。如果是在找某个测试/函数/报错位置，"
    "请调用 search_file 用关键词一步定位到 文件:行号，"
    "再 read_file offset 精读目标区间——不要从头逐页翻。"
    "只有当你确实需要把整个文件内容装进上下文（如通读实现源码理解逻辑）"
    "才继续翻页。请重新决策。"
)


class LoopError(Exception):
    pass


def _truncate(text: str, cap: int = TOOL_OUTPUT_CAP) -> str:
    text = text or ""
    return text if len(text) <= cap else text[:cap] + f"\n...[截断 {len(text) - cap} 字符]"


def _ws_hash(workspace: Path) -> str:
    from .workspace import workspace_hash

    return workspace_hash(workspace)


class HarnessLoop:
    """一次 run 的执行主体。decider: (messages: list[dict]) -> AgentStep。"""

    def __init__(
        self,
        task_dir: str | Path,
        goal: str,
        decider,
        run_id: str | None = None,
        task_id: str | None = None,
        token_budget: int | None = None,
        meter=None,
        context_compress: bool = False,
        keep_recent_steps: int = 3,
        use_plan: bool = False,
        stall_warning: bool = False,
        use_skills: bool = False,
        skill_dir: str | Path | None = None,
        use_mcp: bool = False,
        mcp_servers: list | None = None,
        use_memory: bool = False,
        use_github: bool = False,
        registry: ToolRegistry | None = None,
    ):
        """token_budget: 单 run token 上限（None=不启用护栏）。
        meter: 外部共享的 token 计量 dict（decider 包装层累加 meter["tokens"]，
        loop 只读判断是否超限），计量与决策保持解耦。
        context_compress: 分层压缩开关。旧步骤压成摘要只影响
        decider 视图，self.messages 仍全量存档（checkpoint/resume 不丢信息）。
        use_plan: planner 开关。执行前通过同一 decider 通道规划一次，计划注入
        初始 user 消息并随 checkpoint 持久化；resume 不重规划。
        stall_warning: 空转主动提醒开关。默认只记录 trace 和日志；开启后注入提醒。
        use_skills: 按 goal 匹配 skills/ 目录中的 SKILL.md，并将命中内容注入
        system prompt 尾部（随 checkpoint 持久化，resume 一致）。
        skill_dir: skills 根目录（默认 skills/）。
        use_mcp: 启动时连接 MCP server，并将 tools/list 返回的工具动态注册进
        注册表（mcp_<server>__<tool>）；system prompt 自动包含工具说明。
        mcp_servers: [{"name": ..., "cmd": [...]}]；缺省用
        spawn_client 的默认 demo server。
        use_memory: run 开始时注入任务历史记忆，结束时沉淀成功路径或失败经验。
        use_github: 注入 branch→commit→push→PR 的交付流程；工具执行仍由
        registry 中的 HARNESS_GITHUB 门闩控制。
        """
        self.workspace = Path(task_dir)
        self.registry = registry or ToolRegistry()
        if not self.workspace.is_dir():
            raise LoopError(f"任务目录不存在: {self.workspace}")
        self.goal = goal
        self.decider = decider
        self.stall_warning = stall_warning
        self.use_skills = use_skills
        self._skill_dir = skill_dir
        self.use_mcp = use_mcp
        self._mcp_servers = mcp_servers or []
        self._mcp_clients: list = []  # 持有的 MCP client（run 结束统一 stop）
        self.use_memory = use_memory
        self.use_github = use_github
        self.run_id = run_id or uuid.uuid4().hex[:12]
        self.task_id = task_id or self.workspace.name
        self.token_budget = token_budget
        self.meter = meter
        self.context_compress = context_compress
        self.keep_recent_steps = keep_recent_steps
        self.use_plan = use_plan
        self.messages: list[dict] = []
        self.done_actions: list[dict] = []
        # 记录最后一次写入和最后一次通过验证的步骤，拒绝未经复测的完成声明。
        self._last_write_step = -1
        self._last_green_test_step = -1
        # 重复动作护栏：记录上一步 (tool, args)，写类工具连续提交完全相同动作时拦截
        self._prev_action: dict | None = None
        # 当前执行段获准扩额后，不再用旧预算重复暂停。
        self._budget_approved = False
        # 记录上次空转提醒位置；出现进展后复位，持续空转则周期性提醒。
        self._stall_warned_at = 0
        # path -> {"consec": 连续顺序翻页数, "end": 上次页末行}
        self._page_walk: dict[str, dict] = {}
        self._pending_action = None

    def _rebuild_verification_state(self) -> None:
        """从 trace 重建写/测步号（resume 续跑时用），verdict==ok 即全绿。"""
        self._last_write_step = -1
        self._last_green_test_step = -1
        for t in db.committed_actions(self.run_id):
            result = t["result"]
            if t["tool"] == "run_tests":
                self._last_green_test_step = (
                    t["step"] if result.get("ok") and result.get("data", {}).get("verified") else -1
                )
                continue
            if not result.get("ok"):
                continue
            if (
                t["tool"] in ("edit_file", "write_file")
                and result.get("data", {}).get("changed") is not False
            ):
                self._last_write_step = t["step"]
        # 与实时执行一致：最后一步失败就清空去重状态，允许恢复后重新读取。
        self._prev_action = None
        for t in db.committed_actions(self.run_id)[-1:]:
            if t["result"].get("ok"):
                self._prev_action = {"tool": t["tool"], "args": t["args"], "step": t["step"]}

    # ---------- 对外入口与长期记忆 ----------
    def _maybe_distill(self, result: dict) -> None:
        """run 终态（done/failed）时沉淀长期记忆；paused/budget_paused 可续跑不沉淀。"""
        if not self.use_memory:
            return
        status = result.get("status", "")
        if status not in ("done", "failed"):
            return
        from .memory import distill

        # 用 done_actions（含真实 tool/args）而非 trace（只有摘要）——沉淀需要 args 细节
        actions = [dict(a) for a in self.done_actions]
        distill(self.run_id, self.task_id, actions, status, result.get("reason", ""))

    def run(self) -> dict:
        """从头执行一次 run。

        use_plan 且本 run 尚无计划时先执行一次规划调用。计划注入初始 user 消息并
        随 checkpoint 持久化；若规划完成后进程中断，恢复时复用已有计划。
        db 行已存在（resume 转发：从未执行过任何步 / 杀在 plan phase）→ 复用不重建。
        """
        if db.get_run(self.run_id) is None:
            db.create_run(self.run_id, self.task_id, self.goal)
        db.update_run_status(self.run_id, "running")
        self._setup_mcp()  # 幂等连接 MCP；单个服务失败不会拖垮运行
        try:
            self.messages = self._initial_messages(self.goal, fresh=True)
            if self.use_plan:
                saved = db.get_plan(self.run_id)
                plan = (
                    AgentPlan.model_validate_json(saved["plan_json"])
                    if saved and saved["status"] == "ok"
                    else self._plan_once()
                    if saved is None
                    else None
                )
                if plan is not None:  # None = 重试耗尽降级，无计划照常执行
                    self.messages[1] = dict(self.messages[1])
                    self.messages[1]["content"] += "\n\n" + plan_to_text(plan)
            result = self._run_loop(start_step=1)
            self._maybe_distill(result)  # 终态沉淀记忆
            return result
        finally:
            self._close_mcp()

    def resume(self, approve_budget: bool = False) -> dict:
        """从最后一个 checkpoint 恢复执行。

        - 直接载入 done_actions，不重放已完成动作
        - checkpoint 的 ws_hash 与当前工作区不一致时拒绝恢复
        - budget_paused 状态需要明确批准或提高有限预算
        """
        run = db.get_run(self.run_id)
        if run is None:
            raise LoopError(f"run 不存在: {self.run_id}")
        recovery = db.inspect_recovery(self.run_id)
        if not recovery["can_auto_resume"]:
            db.set_error(self.run_id, "action_unknown", recovery["reason"], "interrupted")
            raise LoopError(recovery["reason"])
        # 超限暂停的运行未获批准时不得恢复。
        if run["status"] == "budget_paused":
            if approve_budget:
                from .usage import extend_budget

                self.token_budget = extend_budget(self.run_id)
            elif not approval.is_budget_approved(self.run_id):
                raise LoopError(
                    f"[预算护栏] run {self.run_id[:8]} 因超限暂停，需人工批准续跑："
                    "调用 resume(approve_budget=True)（审批记录落 approvals 表）"
                )
            self._budget_approved = False
        db.update_run_status(self.run_id, "running")
        cp = db.last_checkpoint(self.run_id)
        if cp is None:
            return self.run()
        self._setup_mcp()  # 恢复时重新建立 MCP 工具表
        try:
            # 工作区被外部修改后，已保存的执行上下文不再可信。
            cur_hash = _ws_hash(self.workspace)
            saved_hash = cp["ws_hash"] or ""
            if saved_hash and cur_hash != saved_hash:
                db.update_run_status(self.run_id, "failed")
                raise LoopError(
                    f"[工作区漂移] 拒绝续跑：checkpoint 存档 ws_hash={saved_hash[:12]}，"
                    f"当前工作区 {cur_hash[:12]}。任务包被外部改动过（如 git restore、手动编辑），"
                    "基于过期上下文续跑会出错。处理：git restore 还原任务包后重跑本任务，"
                    "或确认改动无害后删除该 run 重开。"
                )
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
            # 将 checkpoint 中的累计 token 回填 meter，使预算跨恢复生效。
            if isinstance(self.meter, dict):
                self.meter["tokens"] = int(cp["tokens_used"] or 0)
                if "total_tokens" in self.meter:
                    self.meter["total_tokens"] = self.meter["tokens"]
            start = int(cp["step"]) + 1
            self._rebuild_verification_state()
            log_event("run_resumed", run_id=self.run_id, step=cp["step"])
            result = self._run_loop(start_step=start)
            self._maybe_distill(result)  # 恢复后进入终态也沉淀记忆
            return result
        finally:
            self._close_mcp()

    def _initial_messages(self, goal: str, fresh: bool) -> list:
        tail = "工作区已就绪，请开始。" if fresh else "工作区已就绪，请继续你上次未完成的修复。"
        system = self._system_prompt()
        if self.use_skills:
            matched = match_skills(self._skill_dir, goal)
            if matched:
                blocks = "\n\n".join(s.render() for s in matched)
                system = system + (
                    "\n\n以下是与本任务相关的技能说明（Skill），"
                    "按其指引行事，与任务规则冲突时以任务目标为准：\n\n" + blocks
                )
        user = "任务目标：" + goal + chr(10) + tail
        if self.use_memory:
            from .memory import render_for_goal

            mem = render_for_goal(self.task_id)
            if mem:
                user += "\n\n" + mem
        return [{"role": "system", "content": system}, {"role": "user", "content": user}]

    def _system_prompt(self) -> str:
        """按当前工具表构造 system prompt，并按运行选项附加交付流程。"""
        base = (
            _SYSTEM_HEAD
            + "\n可用工具（当前运行注册表）:\n"
            + self.registry.describe(github=self.use_github, include_high=False)
        )
        if self.use_github:
            base = base + "\n" + GITHUB_SYSTEM_EXTRA
        return base

    # ---------- MCP 接入 ----------
    def _setup_mcp(self) -> None:
        """启动 MCP server(s) 并注册其工具（幂等；重复调用不重复 spawn）。

        - use_mcp=False / 已有 client 在跑 → 直接返回
        - 每个 server：spawn client → start（initialize 握手）→ tools/list →
          register_mcp_tools 动态注册进注册表（system prompt 由 describe_tools
          重新生成时自动带上，_rebuild_system 见下）
        - server 起不来/工具拉空 → 记日志降级（不拖垮 run：MCP 是增量能力）
        """
        if not self.use_mcp or self._mcp_clients:
            return
        from ..mcp.mcp_client import spawn_client, MCPClient

        servers = self._mcp_servers or [{"name": "demo"}]  # 缺省 demo server
        for cfg in servers:
            name = cfg.get("name", "demo")
            client = None
            try:
                client = MCPClient(name, cfg["cmd"]) if cfg.get("cmd") else spawn_client(name)
                client.start()
                tools = client.list_tools()
                self.registry.register_mcp(
                    name,
                    client,
                    tools,
                    readonly=cfg.get("readonly", ["sqlite_query"] if name == "demo" else []),
                )
                self._mcp_clients.append(client)
                log_event("mcp_connected", run_id=self.run_id)
            except Exception:  # noqa: BLE001 - MCP 起不来是增量失败，不拖垮 run
                if client:
                    client.stop()
                log_event("mcp_unavailable", run_id=self.run_id)

    def _close_mcp(self) -> None:
        self.registry.close()
        for c in self._mcp_clients:
            try:
                c.stop()
            except Exception:  # noqa: BLE001
                pass
        self._mcp_clients = []

    # ---------- 规划阶段 ----------
    def _plan_once(self) -> AgentPlan | None:
        """调用一次 planner；规划和执行共用 decider 与 token 计量。

        规划消息与执行消息相互隔离。无效 JSON 只做有限重试；耗尽后记录失败并
        返回 None，执行循环仍可继续。
        - planning tokens = meter 前后差值（decider 包装层负责累加，loop 只读两次）
        - 产出即 save_plan：进程杀在 plan phase → resume 不重规划、不重付 token
        """
        before = self._used_tokens()
        msgs = [
            {"role": "system", "content": PLAN_PROMPT},
            {"role": "user", "content": "任务目标：" + self.goal},
        ]
        for attempt in range(PLAN_RETRY + 1):
            try:
                text = self.decider(msgs)
                plan = parse_plan(text)
                db.save_plan(self.run_id, plan.model_dump_json(), self._used_tokens() - before, status="ok")
                log_event("plan_saved", run_id=self.run_id)
                return plan
            except StepParseError as e:
                if attempt < PLAN_RETRY:
                    msgs.append(
                        {
                            "role": "user",
                            "content": f"你上一步输出不是合法计划 JSON（{e}）。"
                            "请只输出合规的 plan JSON（含 objective 和 steps），不要解释。",
                        }
                    )
        db.save_plan(self.run_id, "", self._used_tokens() - before, status="failed")
        log_event("plan_invalid", run_id=self.run_id)
        return None

    def _run_loop(self, start_step: int) -> dict:
        # 步数是"段"语义而非整 run 绝对上限：每段（run()/每次 resume()）最多
        # MAX_STEPS 是单个执行段的上限；恢复会开启新的有限执行段，而不是立即
        # 因累计步数再次暂停。
        segment_end = start_step + MAX_STEPS - 1
        step = start_step - 1
        self._repair_segment_start = len(self.done_actions)
        try:
            while step < segment_end:
                if getattr(self.meter, "unknown_calls", 0):
                    self._checkpoint(step)
                    db.set_error(self.run_id, "usage_unknown", "模型请求用量未知，需核查后续跑", "paused")
                    return self._summary("paused", steps=step, reason="usage_unknown")
                # 累计用量超限时暂停，并记录审批请求。
                if self._over_budget():
                    approval.request_budget_continue(self.run_id, self._used_tokens(), self.token_budget)
                    self._checkpoint(step)
                    db.update_run_status(self.run_id, "budget_paused")
                    return self._summary(
                        "budget_paused",
                        steps=step,
                        reason=f"累计 token {self._used_tokens()} 超预算 {self.token_budget}",
                    )
                step += 1
                act = self._decide_once(step)
                if act.done:
                    block = self._verify_before_done()
                    if block:
                        # 拒绝收尾：把原因喂回模型，让它先跑测试验证（不算 done）
                        self.messages.append({"role": "user", "content": block})
                        continue
                    db.append_trace(self.run_id, step, "", "", "agent 认为任务已完成", 0, "done")
                    db.update_run_status(self.run_id, "done")
                    self._checkpoint(step)
                    return self._summary("done", steps=step)
                dup = self._dup_action_block(act)
                if dup:
                    # 重复写操作拦截：上一步已成功执行过完全相同的动作，回放结果防空转
                    self.messages.append({"role": "user", "content": dup})
                    continue
                pwalk = self._page_walk_block(act)
                if pwalk:
                    # 顺序翻页拦截：同文件连续翻页无进展 → 引导 search_file（软墙）
                    self.messages.append({"role": "user", "content": pwalk})
                    continue
                # HIGH 工具在执行前拒绝并记录审计结果。
                spec = self.registry.get(act.tool_name) if act.tool_name else None
                if spec is not None and spec.perm >= Perm.HIGH:
                    deny = approval.deny_high_tool(self.run_id, act.tool_name or "")
                    self.messages.append({"role": "user", "content": deny})
                    continue
                self._execute(act, step)
                repair_pause = self._repair_failure_guard()
                self._stall_radar()  # 连续零进展动作写入 trace/日志，可选主动提醒
                self._checkpoint(step)
                if repair_pause:
                    db.set_error(self.run_id, "repair_stalled", repair_pause, "paused")
                    return self._summary("paused", steps=step, reason=repair_pause)
            db.update_run_status(self.run_id, "paused")
            return self._summary("paused", steps=step)
        except StepParseError as e:
            db.update_run_status(self.run_id, "failed")
            return self._summary("failed", reason=str(e), steps=step)
        except Exception:  # noqa: BLE001 - decider 网络错误等：标 failed 后上抛
            db.update_run_status(self.run_id, "failed")
            raise

    # ---------- 决策与有限重试 ----------
    def _decide_once(self, step: int) -> AgentStep:
        example = (
            '{"thought": "读文件看现状", "tool": "read_file", "args": {"path": "utils.py"}, "done": false}'
        )
        for attempt in range(LLM_RETRY + 1):
            try:
                # 压缩只改变 decider 视图；持久化消息仍保留全量内容。
                if self.context_compress:
                    latest_test = next(
                        (action for action in reversed(self.done_actions) if action["tool"] == "run_tests"),
                        None,
                    )
                    view = build_view(
                        self.messages,
                        self.keep_recent_steps,
                        latest_test=latest_test,
                        last_write_step=self._last_write_step,
                        actions=self.done_actions,
                    )
                else:
                    view = list(self.messages)
                text = self.decider(view)
                return parse_step(text)
            except StepParseError as e:
                if attempt >= LLM_RETRY:
                    raise
                # 将解析错误和合法示例反馈给模型，最多重试 LLM_RETRY 次。
                # 若错误疑似 JSON 语法（Expecting...），附转义规则教学——长 content 里
                # 含代码引号/docstring 是免费模型的常见翻车点，光说"格式错"教不会
                esc_hint = ""
                if "Expecting" in str(e):
                    esc_hint = (
                        " JSON 字符串必须用双引号包裹；字符串内部的每个双引号都要写成"
                        '\\"（反斜杠+引号），换行要写成\\n；绝不能把整段内容用 Python 的'
                        '三引号(""")或单引号(\')包裹。'
                    )
                self.messages.append(
                    {
                        "role": "user",
                        "content": f"你上一步输出不是合法 JSON 决策（{e}）。"
                        f"{esc_hint}\n合法格式示例: {example}"
                        "\n请只输出一段合规 JSON，不要解释。",
                    }
                )
        raise StepParseError("unreachable")  # pragma: no cover

    # ---------- 预算计量 ----------
    def _used_tokens(self) -> int:
        return int((self.meter or {}).get("tokens", 0))

    def _over_budget(self) -> bool:
        """meter 累计 >= 预算即超限（无 meter 或无预算 → 不启用护栏）。

        显式提高有限预算后，当前执行段不再被旧上限拦截。
        """
        if not self.token_budget or self.meter is None:
            return False
        return self._used_tokens() >= self.token_budget and not self._budget_approved

    # ---------- 强制验证 ----------
    def _verify_before_done(self) -> str:
        """模型宣称 done 前校验：改过代码就必须有"写之后的全绿测试"。

        返回空串表示允许收尾；否则返回需要反馈给模型的拦截消息。
        """
        verified_green_after_write = self._last_green_test_step > self._last_write_step
        if not verified_green_after_write:
            return (
                "你修改了代码，但最近一次全绿的测试发生在修改之前（或还没跑过测试）。"
                "代码改了却没有验证通过，不能宣布完成。"
                "请先调用 run_tests 确认全部通过（输出里显示所有测试 passed），"
                "全绿之后再输出 done。"
            )
        if self.use_github and not any(a.get("data", {}).get("pr_url") for a in self.done_actions):
            return "交付任务尚未创建真实PR，不能标记完成。"
        return ""

    def _dup_action_block(self, act: AgentStep) -> str:
        """重复写动作拦截：写类工具提交与上一步完全相同的 (tool, args) 时拒绝执行。

        上一步 edit/write 成功后，相同参数再次执行不会产生有效进展。
        返回空串 = 放行；否则返回喂回模型的拦截消息。
        """
        tool_name = act.tool_name
        if act.done:
            return ""
        prev = self._prev_action
        if not prev or prev.get("args") is None:
            return ""  # resume 场景 args 不可比时宁漏勿误
        if tool_name == "read_file" and prev["tool"] == "read_file":
            # 连续读取同一文件同一区间不会获得新信息，立即引导到其他动作。
            p1 = prev["args"].get("path")
            p2 = (act.args or {}).get("path")
            o1, o2 = prev["args"].get("offset"), (act.args or {}).get("offset")
            l1, l2 = prev["args"].get("limit"), (act.args or {}).get("limit")
            if p1 == p2 and o1 == o2 and l1 == l2:
                return (
                    f"你已连续两次 read_file 同一文件同一区间（{p1}），没有拿到新信息。"
                    "请换动作：run_tests 看失败详情，或用 offset/limit 分页读文件的其他区间，"
                    "或按文件大小选择 write_file/edit_file 修复。不要原地重复读。"
                )
        if (
            tool_name == "search_file"
            and prev["tool"] == "search_file"
            and (act.args or {}) == (prev["args"] or {})
        ):
            # 相同搜索参数不会获得新结果，要求提供具体关键词或切换动作。
            return (
                f"你已连续两次 search_file 完全相同参数（{prev['args']}），不会拿到新结果。"
                "请换具体 pattern（函数名/变量名/报错关键词），或 read_file 精读"
                "已定位的行号区间，或直接 edit_file/run_tests。不要重复同一搜索。"
            )
        if (
            tool_name in ("edit_file", "write_file")
            and prev["tool"] == tool_name
            and prev["args"] == act.args
        ):
            return (
                f"你上一步已成功执行过完全相同的 {tool_name}（参数一致），"
                "不要重复提交同一操作。请 read_file 确认当前文件实际状态，"
                "或 run_tests 验证进度，再决定下一步。"
            )
        return ""

    def _page_walk_block(self, act: AgentStep) -> str:
        """顺序翻页护栏：read_file 带 limit 时，若区间接续上页末尾逐页前进则计数。

        同文件连续顺序翻到第 PAGE_WALK_LIMIT 页（默认 3）仍只读不写不测时拦截，
        提示用 search_file 定位。拦截后该路径计数清零，确有需要时仍可继续翻页。
        返回空串 = 放行；否则返回喂回模型的拦截消息。
        """
        if act.done or act.tool_name != "read_file":
            return ""
        args = act.args or {}
        path, offset, limit = args.get("path"), args.get("offset", 1), args.get("limit", 0)
        if not path or not limit:  # 只针对分页读；全文读（无 limit）不管
            return ""
        try:
            offset, limit = int(offset), int(limit)
        except (TypeError, ValueError):
            return ""
        if offset < 1:
            return ""  # 交给 read_file 的 clamp/提示，不计数
        end = offset + limit - 1
        prev = self._page_walk.get(path)
        if prev and offset == prev["end"] + 1:
            consec = prev["consec"] + 1
        else:
            consec = 1  # 非接续（跳读/换区间起点）→ 新一段，从 1 计
        self._page_walk[path] = {"consec": consec, "end": end}
        if consec < PAGE_WALK_LIMIT:
            return ""
        self._page_walk[path] = {"consec": 0, "end": end}  # 软墙：拦一次后清零可续翻
        return PAGE_WALK_MSG.format(path=path, n=PAGE_WALK_LIMIT)

    # ---------- 工具执行 ----------
    def _repair_failure_guard(self) -> str:
        """Bound failed repairs per file and explicit execution segment; reads are not progress."""
        actions = self.done_actions[self._repair_segment_start :]
        latest = actions[-1]
        if latest["tool"] not in ("edit_file", "write_file") or latest["ok"]:
            return ""

        def target(action):
            path = action.get("args", {}).get("path")
            return (
                os.path.normcase(os.path.abspath(self.workspace / path))
                if isinstance(path, str) and path
                else None
            )

        path = target(latest)
        if path is None:
            return ""
        failures = 0
        for action in reversed(actions):
            if action["tool"] not in ("edit_file", "write_file"):
                continue
            if action["ok"] and action.get("data", {}).get("changed") is False:
                continue
            if action["ok"] or target(action) != path:
                break
            failures += 1
        guidance = (
            f"\n[修复恢复] 对 {latest['args']['path']} 连续{failures}次修改失败。先 read_file 核对当前文件，再调整编辑方式。"
            "少量修改可用 edit_file，old 复制完整、唯一的原始代码行并保留缩进；"
            "全文 write_file 必须保留文档字符串成对的三引号、import和其他定义。不要重复相同的无效内容，原测试与校验规则保持不变。"
        )
        pause = (
            "同一文件连续3次修改失败，已暂停；请人工检查后显式恢复，不会自动续跑。" if failures >= 3 else ""
        )
        self.messages[-1]["content"] += guidance + ("\n" + pause if pause else "")
        return pause

    def _execute(self, act: AgentStep, step: int) -> dict:
        tool = act.tool_name or ""
        args = act.args or {}
        # 1) 查注册表：工具必须存在，且权限未超 MED（HIGH 由 _run_loop 审批拦截，此处兜底）
        spec = self.registry.get(tool)
        if spec is None:
            raise LoopError(f"未知工具: {tool or '(空)'}。可用工具: {list_tool_names()}")
        if spec.perm >= Perm.HIGH:
            # 兜底：正常路径在 _run_loop 已被审批拦截，到不了这里
            raise LoopError(approval.deny_high_tool(self.run_id, tool or "(空)"))
        # 2) 执行 handler
        try:
            action_id = db.begin_action(self.run_id, step, tool, args)
            result = self.registry.execute(tool, self.workspace, args)
        except ToolError as e:
            # 可恢复的工具错误转成 ok=False 反馈，让模型修正参数而不终止整个运行。
            result = ToolResult(f"[工具错误] {e}", ok=False)
        output = result.output
        # 强制验证状态：记录最后一次写文件 / 最后一次全绿测试的步号
        if tool in ("edit_file", "write_file") and result.ok and result.data.get("changed") is not False:
            self._last_write_step = step
        elif tool == "run_tests":
            self._last_green_test_step = step if result.ok and result.data.get("verified") else -1

        self._pending_action = (action_id, {"output": output, "ok": result.ok, "data": result.data})
        self.messages.append(
            {
                "role": "assistant",
                "content": json.dumps(
                    {"thought": f"调用 {tool}", "tool": tool, "args": args, "done": False},
                    ensure_ascii=False,
                ),
            }
        )
        self.messages.append(
            {"role": "user", "content": f"[工具结果 {tool}] {_truncate(output, READ_FILE_CAP)}"}
        )
        # 消息层上限必须与读取工具一致，避免已按完整行限长的源码被二次截断。
        self.done_actions.append(
            {
                "step": step,
                "tool": tool,
                "args": args,
                "result_tail": _truncate(output, 300),
                "ok": result.ok,
                "data": result.data,
            }
        )
        # 只比较紧邻的成功动作。失败后清空旧记录，允许重新读取以纠正错误。
        # 否则 read → 编辑失败 → read 会一直误命中旧 read，阻断恢复。
        self._prev_action = {"tool": tool, "args": args} if result.ok else None
        return {"tool": tool, "output": output}

    # ---------- 空转检测 ----------
    def _stall_radar(self) -> None:
        """连续 STALL_LIMIT 个动作全是零进展（无 edit/write/test）→ 判空转。

        默认只记录 trace（verdict=stall）和日志，不改变执行流。stall_warning=True
        时将提醒注入上下文。持续空转会周期性提醒，出现进展动作后重新计数。
        """
        n = len(self.done_actions)
        if n < STALL_LIMIT:
            return
        recent = self.done_actions[-STALL_LIMIT:]
        if any(a["tool"] in PROGRESS_TOOLS for a in recent):
            self._stall_warned_at = n  # 有进展 → 复位（从当前位置重新计空转）
            return
        if n - self._stall_warned_at < STALL_LIMIT:
            return  # 距上次提醒不足 STALL_LIMIT 步，不刷屏
        self._stall_warned_at = n
        tools = ", ".join(a["tool"] for a in recent)
        msg = STALL_MSG.format(n=STALL_LIMIT, tools=tools)
        db.append_trace(self.run_id, n, "", "", _truncate(f"stall_warning: {msg}", 300), 0, "stall")
        log_event("stall_detected", run_id=self.run_id, step=n)
        if self.stall_warning:
            self.messages.append({"role": "user", "content": msg})

    # ---------- 存档 ----------
    def _checkpoint(self, step: int) -> None:
        values = dict(
            run_id=self.run_id,
            step=step,
            done_actions=self.done_actions,
            ctx_messages=self.messages,
            ctx_summary=json.dumps(self.messages, ensure_ascii=False)[-1500:],
            ws_hash=_ws_hash(self.workspace),
            tokens_used=self._used_tokens(),
        )
        if self._pending_action:
            action_id, result = self._pending_action
            db.commit_action_checkpoint(action_id, result, values)
            self._pending_action = None
        else:
            db.save_checkpoint(**values)

    def _summary(self, status: str, steps: int = 0, reason: str = "") -> dict:
        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "status": status,
            "verified": self._last_green_test_step > self._last_write_step,
            "steps": steps,
            "reason": reason,
            "actions": list(self.done_actions),
        }
