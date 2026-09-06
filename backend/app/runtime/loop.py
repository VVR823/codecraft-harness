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
import uuid
from pathlib import Path

from ..config import LLM_RETRY, MAX_STEPS
from ..store import db
from ..tools import approval
from ..tools.registry import (  # noqa: E402
    Perm,
    ToolError,
    describe_tools,
    get_tool,
    list_tool_names,
)
from .context import build_view
from .plan import PLAN_PROMPT, PLAN_RETRY, AgentPlan, parse_plan, plan_to_text
from .protocol import AgentStep, StepParseError, parse_step
from ..skills.skill_registry import match_skills

_SYSTEM_HEAD = """你是一个软件工程师 Agent，正在执行一个代码任务。
每次输出必须是一段 JSON（不要多余文字），格式：
{"thought": "这步在想什么", "tool": "read_file|edit_file|write_file|run_tests", "args": {"...": "..."}, "done": false}
- read_file: args={"path": "相对任务目录的路径"}。若输出尾部出现"超过单次读取上限"，说明文件很大只显示了开头，
  要用分页继续读：args={"path": "...", "offset": 起始行号(1开始), "limit": 行数}；大文件先 read_file 看全貌定位，
  再分页读目标区间，不要反复读同一段。
- search_file: args={"pattern": "正则或子串", "path": "可选，限定单文件"}——在大文件/真实库里定位定义与引用：
  先 search 拿"文件:行号"，再 read_file 分页精读目标区间；**不要从头到尾通读大文件**
- edit_file: args={"path": "...", "old": "要替换的原文片段(必须与文件完全一致且唯一)", "new": "新片段"}——改已有代码优先用它
- write_file: args={"path": "...", "content": "..."}，content 是文件全文
- run_tests: args={}（跑任务目录的 pytest）
- 当你确认任务已完成（测试全绿等），输出 {"thought": "...", "done": true}，不带 tool。
工作区 = 任务目录（git 管理）。
【工作方法】开工【第一步】先 run_tests——pytest 会告诉你哪个测试失败、期望值 vs 实际值、
失败在哪个文件第几行，这是定位 bug 的最高效入口。不要不跑测试就埋头通读大文件猜 bug；
拿到失败信息后再定位修复点：小文件 read_file 全文，大文件/真实库先 search_file 找相关
定义与引用（拿文件:行号），再 read_file 分页精读目标区间。修完代码必须 run_tests 验证，
红了就继续读、修、再测，直到全绿才 done。
【关键规则】本任务要修的文件都已存在：修改已有代码【必须】用 edit_file 只输出要改的片段
（old 逐字复制 read_file 拿到的原文，new 是新片段）；write_file 仅用于创建【新】文件，
绝不要用 write_file 整文件覆盖已有文件（长内容 JSON 容易出错，还会误删你没写进去的代码）。"""

SYSTEM_PROMPT = _SYSTEM_HEAD + "\n可用工具（注册表生成，与执行校验同源）:\n" + describe_tools()

# 工具结果截断上限（防上下文爆炸的 v0 防线，正式分层压缩在 context.py）
TOOL_OUTPUT_CAP = 3000

# ---- O4 空转雷达（2026-09-05）----
# 免费模型在长上下文/高压下会"原地打转"：连续只 read_file 不写不测（B6a 补测实证：
# T2 plan 档 step1~5 全读 test_module.py，空转 35k token 才被预算护栏截停）。
# 雷达 = 连续 STALL_LIMIT 个动作全是零进展工具 → 触发。纯观测默认开（只记 trace + 日志，
# 不改执行流 → 数字① 口径零影响）；stall_warning=True 时才把提醒注入上下文（长任务按需开）。
STALL_LIMIT = 3                              # 连续多少个零进展动作判空转
PROGRESS_TOOLS = ("edit_file", "write_file", "run_tests")  # 能推进/验证任务的工具
STALL_MSG = ("（系统提醒）你已连续 {n} 步只做侦察（{tools}）没有写文件或跑测试。"
             "如果已掌握足够信息，请直接 edit_file 修改目标文件或 run_tests 验证，别再只读。")


class LoopError(Exception):
    pass


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
                 run_id: str | None = None, task_id: str | None = None,
                 token_budget: int | None = None, meter: dict | None = None,
                 context_compress: bool = False,
                 keep_recent_steps: int = 3,
                 use_plan: bool = False,
                 stall_warning: bool = False,
                 use_skills: bool = False,
                 skill_dir: str | Path | None = None,
                 use_mcp: bool = False,
                 mcp_servers: list | None = None,
                 use_memory: bool = False):
        """token_budget: 单 run token 上限（None=不启用护栏，默认无护栏）。
        meter: 外部共享的 token 计量 dict（decider 包装层累加 meter["tokens"]，
        loop 只读判断是否超限）——计量与决策解耦，任何 decider 都能挂护栏。
        context_compress: M3 分层压缩开关（Q14）——开=旧步压成一行摘要只影响
        decider 视图，self.messages 仍全量存档（checkpoint/resume 不丢信息）。
        use_plan: M4 planner 开关——开=进入执行前先调一次 planner（同一 decider
        通道），计划注入初始 user 消息随 checkpoint 持久化；resume 不重规划。
        stall_warning: O4 空转雷达的主动提醒开关——默认 False（雷达纯观测：trace+
        日志，不改执行流）；True=空转时把提醒注入上下文（长任务按需开）。
        use_skills: M5 Skills 开关——开=按 goal 语义匹配 skills/ 目录里的 SKILL.md，
        把命中技能指令注入 system prompt 尾部（随 checkpoint 持久化，resume 一致）。
        skill_dir: skills 仓库根目录（目录/文件可复用，默认 skills/）。默认关→数字①口径不动。
        use_mcp: M5 MCP 开关——开=启动时连 MCP server(s)（自研 stdio client），
        tools/list 拉到的工具动态注册进注册表（mcp_<server>__<tool>），system prompt
        工具说明书自动带上。mcp_servers: [{"name": ..., "cmd": [...]}]；缺省用
        spawn_client 的默认 demo server。默认关→数字①口径不动。
        use_memory: M5 长期记忆开关——开=run 开始时注入该任务历史记忆（经验复用），
        run 结束沉淀新记忆（成功路径/失败教训）。默认关→数字①口径不动。
        """
        self.workspace = Path(task_dir)
        if not self.workspace.is_dir():
            raise LoopError(f"任务目录不存在: {self.workspace}")
        self.goal = goal
        self.decider = decider
        self.stall_warning = stall_warning
        self.use_skills = use_skills
        self._skill_dir = skill_dir
        self.use_mcp = use_mcp
        self._mcp_servers = mcp_servers or []
        self._mcp_clients: list = []      # 持有的 MCP client（run 结束统一 stop）
        self.use_memory = use_memory
        self.run_id = run_id or uuid.uuid4().hex[:12]
        self.task_id = task_id or self.workspace.name
        self.token_budget = token_budget
        self.meter = meter
        self.context_compress = context_compress
        self.keep_recent_steps = keep_recent_steps
        self.use_plan = use_plan
        self.messages: list[dict] = []
        self.done_actions: list[dict] = []
        # 强制验证护栏（Day6）：记录"最后一次写文件"与"最后一次全绿测试"的步号，
        # 模型在写码后未跑出全绿就宣称 done 时拒绝收尾（免费模型"写完就飘"的常见病）
        self._last_write_step = -1
        self._last_green_test_step = -1
        # 重复动作护栏：记录上一步 (tool, args)，写类工具连续提交完全相同动作时拦截
        self._prev_action: dict | None = None
        # 预算护栏：人工批准续跑后置 True，本轮不再因超限暂停（"人已放行"语义）
        self._budget_approved = False
        # O4 空转雷达：同一段空转只提醒一次（出现进展动作后重置）
        self._stall_armed = False

    def _rebuild_verification_state(self) -> None:
        """从 trace 重建写/测步号（resume 续跑时用），verdict==ok 即全绿。"""
        self._last_write_step = -1
        self._last_green_test_step = -1
        for t in db.list_traces(self.run_id):
            if t["tool"] in ("edit_file", "write_file"):
                self._last_write_step = t["step"]
            elif t["tool"] == "run_tests" and t["verdict"] == "ok":
                self._last_green_test_step = t["step"]
        # 上一步动作（防重复写）：取 trace 最后一条非空工具记录
        for t in reversed(db.list_traces(self.run_id)):
            if t["tool"]:
                self._prev_action = {"tool": t["tool"], "step": t["step"]}
                break

    # ---------- 对外入口 ----------
    # ---------- 长期记忆（M5-B3） ----------
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
        distill(self.run_id, self.task_id, actions,
                status, result.get("reason", ""))

    def run(self) -> dict:
        """从头执行一次 run。

        M4 planner：use_plan 且本 run 尚无计划 → 先进 plan phase（一次规划调用），
        计划注入初始 user 消息（goal 之后）→ 随 checkpoint 持久化。产出即 save_plan
        （原子，进程若被杀在 plan phase，resume 走 run() 时 get_plan 已有行 → 不重规划）。
        db 行已存在（resume 转发：从未执行过任何步 / 杀在 plan phase）→ 复用不重建。
        """
        if db.get_run(self.run_id) is None:
            db.create_run(self.run_id, self.task_id, self.goal)
        db.update_run_status(self.run_id, "running")
        self._setup_mcp()   # M5-B2：MCP server(s) 接入（幂等；失败降级不拖垮 run）
        try:
            self.messages = self._initial_messages(self.goal, fresh=True)
            if self.use_plan and db.get_plan(self.run_id) is None:
                plan = self._plan_once()
                if plan is not None:  # None = 重试耗尽降级，无计划照常执行
                    self.messages[1] = dict(self.messages[1])
                    self.messages[1]["content"] += "\n\n" + plan_to_text(plan)
            result = self._run_loop(start_step=1)
            self._maybe_distill(result)   # M5-B3：终态沉淀记忆
            return result
        finally:
            self._close_mcp()

    def resume(self, approve_budget: bool = False) -> dict:
        """从最后 checkpoint 续跑（MVP 底线 2/3/4 交汇点）。

        - 底线2：不重放已完成动作（done_actions 直接载入）
        - 底线3：checkpoint 的 ws_hash 与当前工作区不一致（漂移）→ 拒绝续跑
        - 底线4：run 处于 budget_paused（预算超限）→ 必须人工批准才续跑
        """
        run = db.get_run(self.run_id)
        if run is None:
            raise LoopError(f"run 不存在: {self.run_id}")
        # 预算暂停门（底线4）：超限暂停的 run 未获批准不得续跑
        if run["status"] == "budget_paused":
            if approve_budget:
                approval.approve_budget_continue(self.run_id)
            elif not approval.is_budget_approved(self.run_id):
                raise LoopError(
                    f"[预算护栏] run {self.run_id[:8]} 因超限暂停，需人工批准续跑："
                    "调用 resume(approve_budget=True)（审批记录落 approvals 表）")
            self._budget_approved = True
        db.update_run_status(self.run_id, "running")
        cp = db.last_checkpoint(self.run_id)
        if cp is None:
            return self.run()
        self._setup_mcp()   # M5-B2：resume 续跑同样需要 MCP 工具（幂等）
        try:
            # 漂移识别（底线3）：工作区被外部改动（git restore/手动编辑）→ 续跑上下文已过期
            cur_hash = _ws_hash(self.workspace)
            saved_hash = cp["ws_hash"] or ""
            if saved_hash and cur_hash != saved_hash:
                db.update_run_status(self.run_id, "failed")
                raise LoopError(
                    f"[工作区漂移] 拒绝续跑：checkpoint 存档 ws_hash={saved_hash[:12]}，"
                    f"当前工作区 {cur_hash[:12]}。任务包被外部改动过（如 git restore、手动编辑），"
                    "基于过期上下文续跑会出错。处理：git restore 还原任务包后重跑本任务，"
                    "或确认改动无害后删除该 run 重开。")
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
            # 预算计量恢复：checkpoint 记录的历史 token 回填 meter（护栏跨 resume 生效）
            if self.meter is not None:
                self.meter["tokens"] = int(cp["tokens_used"] or 0)
            start = int(cp["step"]) + 1
            self._rebuild_verification_state()
            print(f"[resume] 从 checkpoint step={cp['step']} 续跑，已完成 "
                  f"{len(self.done_actions)} 个动作（不重放）")
            result = self._run_loop(start_step=start)
            self._maybe_distill(result)   # M5-B3：resume 到终态同样沉淀
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
                system = system + ("\n\n以下是与本任务相关的技能说明（Skill），"
                                   "按其指引行事，与任务规则冲突时以任务目标为准：\n\n" + blocks)
        user = "任务目标：" + goal + chr(10) + tail
        if self.use_memory:
            from .memory import render_for_goal
            mem = render_for_goal(self.task_id)
            if mem:
                user += "\n\n" + mem
        return [{"role": "system", "content": system},
                {"role": "user", "content": user}]

    def _system_prompt(self) -> str:
        """system prompt：use_mcp 时动态重算（MCP 工具运行期才注册，静态常量不含）。"""
        if not self.use_mcp:
            return SYSTEM_PROMPT
        return _SYSTEM_HEAD + "\n可用工具（注册表生成，含 MCP 动态工具）:\n" + describe_tools()

    # ---------- MCP 接入（M5-B2） ----------
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
        from ..mcp.mcp_client import spawn_client
        from ..tools.registry import register_mcp_tools
        servers = self._mcp_servers or [{"name": "demo"}]  # 缺省 demo server
        for cfg in servers:
            name = cfg.get("name", "demo")
            try:
                client = spawn_client(name)
                client.start()
                tools = client.list_tools()
                n = register_mcp_tools(name, client, tools)
                self._mcp_clients.append(client)
                print(f"[mcp] server={name} 已连，注册 {n} 个工具: "
                      f"{[t.name for t in tools]}", flush=True)
            except Exception as e:  # noqa: BLE001 - MCP 起不来是增量失败，不拖垮 run
                print(f"[mcp] server={name} 接入失败（降级继续）: {e}", flush=True)

    def _close_mcp(self) -> None:
        for c in self._mcp_clients:
            try:
                c.stop()
            except Exception:  # noqa: BLE001
                pass
        self._mcp_clients = []

    # ---------- 规划阶段（M4 planner，P1/P3/P4） ----------
    def _plan_once(self) -> AgentPlan | None:
        """plan phase：调一次 planner（与执行共用同一 decider 通道，token 计量天然一致）。

        - 消息 = PLAN_PROMPT(system) + 任务目标(user)——与执行消息区分开，互不污染
        - 坏 plan JSON 重试 ≤PLAN_RETRY（P2）；仍失败 → 记 plans 行 failed 后降级返回
          None（advisory 语义：规划是前置 0 成本增强，失败不能拖垮执行）
        - planning tokens = meter 前后差值（decider 包装层负责累加，loop 只读两次）
        - 产出即 save_plan：进程杀在 plan phase → resume 不重规划、不重付 token
        """
        before = self._used_tokens()
        msgs = [{"role": "system", "content": PLAN_PROMPT},
                {"role": "user", "content": "任务目标：" + self.goal}]
        last_err = ""
        for attempt in range(PLAN_RETRY + 1):
            try:
                text = self.decider(msgs)
                plan = parse_plan(text)
                db.save_plan(self.run_id, plan.model_dump_json(),
                             self._used_tokens() - before, status="ok")
                print(f"[planner] 规划完成：{len(plan.steps)} 步，objective={plan.objective[:50]}")
                return plan
            except StepParseError as e:
                last_err = str(e)
                if attempt < PLAN_RETRY:
                    msgs.append({"role": "user",
                                 "content": f"你上一步输出不是合法计划 JSON（{e}）。"
                                            "请只输出合规的 plan JSON（含 objective 和 steps），不要解释。"})
        db.save_plan(self.run_id, "", self._used_tokens() - before, status="failed")
        print(f"[planner] 规划失败降级为无计划执行（{last_err[:100]}）")
        return None

    def _run_loop(self, start_step: int) -> dict:
        step = start_step - 1
        try:
            while step < MAX_STEPS:
                # 预算护栏（底线4）：meter 累计超限 → 暂停等人工批准（记审批请求）
                if self._over_budget():
                    approval.request_budget_continue(
                        self.run_id, self._used_tokens(), self.token_budget)
                    self._checkpoint(step)
                    db.update_run_status(self.run_id, "budget_paused")
                    return self._summary(
                        "budget_paused", steps=step,
                        reason=f"累计 token {self._used_tokens()} 超预算 {self.token_budget}")
                step += 1
                act = self._decide_once(step)
                if act.done:
                    block = self._verify_before_done()
                    if block:
                        # 拒绝收尾：把原因喂回模型，让它先跑测试验证（不算 done）
                        self.messages.append({"role": "user", "content": block})
                        continue
                    db.append_trace(self.run_id, step, "", "", "agent 认为任务已完成",
                                    0, "done")
                    db.update_run_status(self.run_id, "done")
                    self._checkpoint(step)
                    return self._summary("done", steps=step)
                dup = self._dup_action_block(act)
                if dup:
                    # 重复写操作拦截：上一步已成功执行过完全相同的动作，回放结果防空转
                    self.messages.append({"role": "user", "content": dup})
                    continue
                # HIGH 工具审批（M2）：模型请求高危工具 → 拒绝 + 审计记录，喂回提示继续
                spec = get_tool(act.tool_name) if act.tool_name else None
                if spec is not None and spec.perm >= Perm.HIGH:
                    deny = approval.deny_high_tool(self.run_id, act.tool_name or "")
                    self.messages.append({"role": "user", "content": deny})
                    continue
                self._execute(act, step)
                self._stall_radar()   # O4：连续零进展动作 → trace/日志（可选提醒）
                self._checkpoint(step)
            db.update_run_status(self.run_id, "paused")
            return self._summary("paused", steps=step)
        except StepParseError as e:
            db.update_run_status(self.run_id, "failed")
            return self._summary("failed", reason=str(e), steps=step)
        except Exception as e:  # noqa: BLE001 - decider 网络错误等：标 failed 后上抛
            db.update_run_status(self.run_id, "failed")
            raise

    # ---------- 决策（带 Q11 重试） ----------
    def _decide_once(self, step: int) -> AgentStep:
        example = ('{"thought": "读文件看现状", "tool": "read_file",'
                   ' "args": {"path": "utils.py"}, "done": false}')
        for attempt in range(LLM_RETRY + 1):
            try:
                # M3：开压缩时给 decider 的是"近 N 步全文 + 旧步摘要"视图（消息本体仍全量）
                if self.context_compress:
                    view = build_view(self.messages, self.keep_recent_steps)
                else:
                    view = list(self.messages)
                text = self.decider(view)
                return parse_step(text)
            except StepParseError as e:
                if attempt >= LLM_RETRY:
                    raise
                # 把错误喂回模型要求重出，并附合法格式示例（Q11：解析失败重试 ≤LLM_RETRY）
                # 若错误疑似 JSON 语法（Expecting...），附转义规则教学——长 content 里
                # 含代码引号/docstring 是免费模型的常见翻车点，光说"格式错"教不会
                esc_hint = ""
                if "Expecting" in str(e):
                    esc_hint = (" JSON 字符串必须用双引号包裹；字符串内部的每个双引号都要写成"
                                "\\\"（反斜杠+引号），换行要写成\\n；绝不能把整段内容用 Python 的"
                                "三引号(\"\"\")或单引号(')包裹。")
                self.messages.append({"role": "user",
                                      "content": f"你上一步输出不是合法 JSON 决策（{e}）。"
                                                 f"{esc_hint}\n合法格式示例: {example}"
                                                 "\n请只输出一段合规 JSON，不要解释。"})
        raise StepParseError("unreachable")  # pragma: no cover

    # ---------- 预算计量（M2） ----------
    def _used_tokens(self) -> int:
        return int((self.meter or {}).get("tokens", 0))

    def _over_budget(self) -> bool:
        """meter 累计 >= 预算即超限（无 meter 或无预算 → 不启用护栏）。

        人工批准续跑后（_budget_approved）本轮不再拦截——预算的语义是"暂停等人批准"，
        不是"到点就永久失败"。
        """
        if not self.token_budget or self.meter is None:
            return False
        return self._used_tokens() >= self.token_budget and not self._budget_approved

    # ---------- 强制验证 ----------
    def _verify_before_done(self) -> str:
        """模型宣称 done 前校验：改过代码就必须有"写之后的全绿测试"。

        返回空串 = 允许收尾；否则返回要喂回模型的拦截消息。
        防"写完就飘"（免费模型常见病：改完代码不 run_tests 就宣布完成）。
        """
        wrote = self._last_write_step >= 0
        verified_green_after_write = self._last_green_test_step > self._last_write_step
        if wrote and not verified_green_after_write:
            return ("你修改了代码，但最近一次全绿的测试发生在修改之前（或还没跑过测试）。"
                    "代码改了却没有验证通过，不能宣布完成。"
                    "请先调用 run_tests 确认全部通过（输出里显示所有测试 passed），"
                    "全绿之后再输出 done。")
        return ""

    def _dup_action_block(self, act: AgentStep) -> str:
        """重复写动作拦截：写类工具提交与上一步完全相同的 (tool, args) 时拒绝执行。

        免费模型常见病：上一步 edit/write 已成功，模型没吸收结果又原样提交一次
        （如 edit_file 成功后再次提交同一 old——此时 old 已被替换，必然失败）。
        返回空串 = 放行；否则返回喂回模型的拦截消息。
        """
        tool_name = act.tool_name
        if act.done:
            return ""
        prev = self._prev_action
        if not prev or prev.get("args") is None:
            return ""  # resume 场景 args 不可比时宁漏勿误
        if tool_name == "read_file" and prev["tool"] == "read_file":
            # 连续两次读同一文件同一区间 = 模型在空转（拿不到新信息还反复读）。
            # 免费模型大文件死循环实证：T4 真实库 test_regression.py 600 行超截断上限，
            # 模型 14 步重复 read_file 同一文件直到预算暂停。这里第一步就拦，引导转向。
            p1 = prev["args"].get("path")
            p2 = (act.args or {}).get("path")
            o1, o2 = prev["args"].get("offset"), (act.args or {}).get("offset")
            l1, l2 = prev["args"].get("limit"), (act.args or {}).get("limit")
            if p1 == p2 and o1 == o2 and l1 == l2:
                return (f"你已连续两次 read_file 同一文件同一区间（{p1}），没有拿到新信息。"
                        "请换动作：run_tests 看失败详情，或用 offset/limit 分页读文件的其他区间，"
                        "或直接 edit_file 修复。不要原地重复读。")
        if tool_name in ("edit_file", "write_file") and prev["tool"] == tool_name and prev["args"] == act.args:
            return (f"你上一步已成功执行过完全相同的 {tool_name}（参数一致），"
                    "不要重复提交同一操作。请 read_file 确认当前文件实际状态，"
                    "或 run_tests 验证进度，再决定下一步。")
        return ""

    # ---------- 工具执行 ----------
    def _execute(self, act: AgentStep, step: int) -> dict:
        tool = act.tool_name or ""
        args = act.args or {}
        # 1) 查注册表：工具必须存在，且权限未超 MED（HIGH 由 _run_loop 审批拦截，此处兜底）
        spec = get_tool(tool)
        if spec is None:
            raise LoopError(f"未知工具: {tool or '(空)'}。可用工具: {list_tool_names()}")
        if spec.perm >= Perm.HIGH:
            # 兜底：正常路径在 _run_loop 已被审批拦截，到不了这里
            raise LoopError(approval.deny_high_tool(self.run_id, tool or "(空)"))
        # 2) 执行 handler
        try:
            result = spec.handler(self.workspace, args)
        except ToolError as e:
            raise LoopError(str(e)) from e
        output = result.output
        verdict = "ok" if result.ok else "fail"
        # 强制验证状态：记录最后一次写文件 / 最后一次全绿测试的步号
        if tool in ("edit_file", "write_file"):
            self._last_write_step = step
        elif tool == "run_tests" and result.ok:
            self._last_green_test_step = step

        db.append_trace(self.run_id, step, tool,
                        json.dumps(args, ensure_ascii=False)[:500],
                        _truncate(output), 0, verdict)        # 工具结果摘要进入上下文（v0 直拼；正式分层压缩在 context.py）
        self.messages.append({"role": "assistant",
                              "content": f"调用 {tool} {json.dumps(args, ensure_ascii=False)}"})
        self.messages.append({"role": "user",
                              "content": f"[工具结果 {tool}] {_truncate(output, 2000)}"})
        self.done_actions.append({"step": step, "tool": tool,
                                  "args": args, "result_tail": _truncate(output, 300)})
        # 记录上一步成功动作（重复动作护栏用；handler 失败时不上记录→模型可重试同动作）
        self._prev_action = {"tool": tool, "args": args}
        return {"tool": tool, "output": output}

    # ---------- O4 空转雷达 ----------
    def _stall_radar(self) -> None:
        """连续 STALL_LIMIT 个动作全是零进展（无 edit/write/test）→ 判空转。

        纯观测（默认）：只记 trace（verdict=stall）+ 打日志，不改执行流、不碰 messages
        ——数字①/②/③ 口径零影响。stall_warning=True 时把提醒注入上下文拉模型回正轨。
        同一段空转只提醒一次：出现进展动作（_stall_armed 复位）后才可能再触发。
        """
        if len(self.done_actions) < STALL_LIMIT:
            return
        recent = self.done_actions[-STALL_LIMIT:]
        if any(a["tool"] in PROGRESS_TOOLS for a in recent):
            self._stall_armed = False  # 有进展 → 复位，允许下次空转再提醒
            return
        if self._stall_armed:
            return  # 同一段空转已提醒过，别刷屏
        self._stall_armed = True
        tools = ", ".join(a["tool"] for a in recent)
        msg = STALL_MSG.format(n=STALL_LIMIT, tools=tools)
        db.append_trace(self.run_id, len(self.done_actions), "", "",
                        _truncate(f"stall_warning: {msg}", 300), 0, "stall")
        print(f"[空转雷达] 连续 {STALL_LIMIT} 步零进展（{tools}）→ 已记录", flush=True)
        if self.stall_warning:
            self.messages.append({"role": "user", "content": msg})

    # ---------- 存档 ----------
    def _checkpoint(self, step: int) -> None:
        db.save_checkpoint(
            run_id=self.run_id,
            step=step,
            done_actions=self.done_actions,
            ctx_messages=self.messages,
            ctx_summary=json.dumps(self.messages, ensure_ascii=False)[-1500:],
            ws_hash=_ws_hash(self.workspace),
            tokens_used=self._used_tokens(),
        )

    def _summary(self, status: str, steps: int = 0, reason: str = "") -> dict:
        return {"run_id": self.run_id, "task_id": self.task_id, "status": status,
                "steps": steps, "reason": reason,
                "actions": list(self.done_actions)}
