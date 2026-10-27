"""One durable execution path shared by CLI and HTTP. No automatic resumption."""

from contextlib import ExitStack
import json
from pathlib import Path
import re
import threading
import sys
import uuid

from ..config import BASE_DIR
from ..store import db
from ..logging_config import log_event
from ..tools.registry import ToolRegistry
from .demo import DemoClient, PATCHES
from .execution_lock import ExecutionBusy, execution_lock
from .loop import HarnessLoop
from .model_client import ModelClient, model_error_detail
from .plan import PLAN_PROMPT
from .protocol import decision_schema
from .settings import RunOptions, Settings
from .usage import UsageMeter, extend_budget
from .workspace import load_snapshot, prepare_workspace, resolve_pack, verify_baseline, workspace_hash


class CapacityError(ValueError):
    pass


class UsageUnknown(Exception):
    pass


class BudgetExhausted(Exception):
    pass


class RunService:
    def __init__(self, settings: Settings, model_factory=None):
        self.settings = settings
        self.model_factory = model_factory or ModelClient
        self._threads: dict[str, threading.Thread] = {}
        self._guard = threading.RLock()
        with self.database():
            db.init_db()

    def database(self):
        return db.use_database(self.settings.data_dir / "harness.db")

    def create(self, task_id, goal, options, source=None):
        self.settings.validate_mode(options)
        pack = resolve_pack(task_id, BASE_DIR / "tasks")
        original_goal = (pack / "README.md").read_text(encoding="utf-8")
        if options.mode == "offline_demo" and (
            task_id not in PATCHES
            or source is not None
            or (goal and goal != original_goal)
            or options.use_github
        ):
            raise ValueError("离线演示仅支持原始T1/T2/T3固定目标；自带源码或目标请显式使用local模式")
        if options.use_github and source is None:
            raise ValueError("GitHub模式需要显式的独立源码仓库")
        goal = goal or original_goal
        if len(goal) > 20000:
            raise ValueError("goal最多20000字符")
        run_id = uuid.uuid4().hex[:12]
        snapshot = prepare_workspace(
            Path(source).absolute() if source else pack,
            run_id,
            self.settings.data_dir,
            github=options.use_github,
        )
        saved = {
            **options.model_dump(),
            "_version": 1,
            "_endpoint": self.settings.base_url if options.mode != "offline_demo" else "",
            "_model_timeout": self.settings.model_timeout,
            "_sandbox_timeout": self.settings.sandbox_timeout,
        }
        with self.database():
            db.create_run(run_id, task_id, goal)
            db.configure_run(run_id, saved, snapshot.root, snapshot.baseline_dir)
        return self.get(run_id)

    def _load(self, run_id):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", run_id):
            raise KeyError("run不存在")
        row = db.get_run(run_id)
        if row is None:
            raise KeyError("run不存在")
        return row

    def _options(self, row):
        saved = json.loads(row["options_json"] or "{}")
        if saved.get("_version") != 1 or not row["workspace_path"] or not row["baseline_path"]:
            db.set_error(
                row["run_id"], "legacy_run", "历史run缺少配置/工作区基线；请从同任务新建run", "interrupted"
            )
            raise ValueError("legacy run仅允许查看")
        options = RunOptions(**{k: v for k, v in saved.items() if not k.startswith("_")})
        self.settings.validate_mode(options)
        if options.mode != "offline_demo" and saved["_endpoint"] != self.settings.base_url:
            db.set_error(row["run_id"], "config_changed", "模型端点与保存配置不一致", "interrupted")
            raise ValueError("模型端点发生变化")
        return options, saved

    def get(self, run_id):
        with self.database():
            row = self._load(run_id)
            cp = db.last_checkpoint(run_id)
            actions = db.committed_actions(run_id)
            last_write = max(
                (
                    a["step"]
                    for a in actions
                    if a["tool"] in ("edit_file", "write_file")
                    and a["result"].get("ok")
                    and a["result"].get("data", {}).get("changed") is not False
                ),
                default=-1,
            )
            latest_test = next((a for a in reversed(actions) if a["tool"] == "run_tests"), None)
            green = (
                latest_test["step"]
                if latest_test
                and latest_test["result"].get("ok")
                and latest_test["result"].get("data", {}).get("verified")
                else -1
            )
            options = json.loads(row["options_json"] or "{}")
            traces = db.list_traces(run_id)
            usage = db.get_usage(run_id)
            totals = db.call_totals(run_id)
            legacy = options.get("_version") != 1
            return {
                "run_id": run_id,
                "task_id": row["task_id"],
                "goal": row["goal"],
                "status": row["status"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "mode": options.get("mode", "legacy"),
                "legacy": legacy,
                "evidence_kind": options.get("mode", "legacy"),
                "options": {k: v for k, v in options.items() if not k.startswith("_")},
                "verified": green > last_write and not legacy,
                "steps": int(cp["step"]) if cp else 0,
                "step": int(cp["step"]) if cp else 0,
                "tokens": int(usage["total_tokens"]) if usage else 0,
                "known_tokens": int(usage["total_tokens"]) if usage else 0,
                "unknown_calls": totals["unknown_calls"],
                "model_calls": totals["calls"],
                "error_code": row["error_code"],
                "error_message": row["error_message"],
                "error": row["error_message"] or None,
                "workspace_path": row["workspace_path"],
                "approvals": [dict(a) for a in db.list_approvals(run_id)],
                "recent_traces": [dict(t) for t in traces[-12:]],
                "last_action": traces[-1]["tool"] if traces else None,
                "active": run_id in self._threads,
            }

    def list(self, limit=30):
        with self.database():
            return [self.get(r["run_id"]) for r in db.list_runs(limit)]

    def traces(self, run_id, limit=50, offset=0):
        with self.database():
            self._load(run_id)
            rows = db.list_traces(run_id)
            return {"traces": [dict(t) for t in rows[offset : offset + limit]], "total": len(rows)}

    def execute(self, run_id):
        return self._execute(run_id, resume=False)

    def resume(self, run_id, *, new_budget=None):
        return self._execute(run_id, resume=True, new_budget=new_budget)

    def _execute(self, run_id, *, resume, new_budget=None, _owned=False):
        with self.database(), ExitStack() as locks:
            if not _owned:
                locks.enter_context(execution_lock(self.settings.data_dir, run_id))
            for slot in range(2):
                try:
                    locks.enter_context(execution_lock(self.settings.data_dir, f"_slot{slot}"))
                    break
                except ExecutionBusy:
                    if slot == 1:
                        raise CapacityError("最多同时执行2个run") from None
            row = self._load(run_id)
            options, saved = self._options(row)
            if row["status"] == "done" or (not resume and row["status"] != "pending"):
                raise ValueError("当前状态不允许执行")
            recovery = db.inspect_recovery(run_id)
            if not recovery["can_auto_resume"]:
                db.set_error(run_id, "action_unknown", recovery["reason"], "interrupted")
                raise ValueError(recovery["reason"])
            if db.call_totals(run_id)["unknown_calls"]:
                message = (
                    row["error_message"] if row["error_code"] == "usage_unknown" else None
                ) or "模型用量未知，需核查后手动处理"
                db.set_error(run_id, "usage_unknown", message, "paused")
                raise ValueError("模型用量未知，不能继续请求")
            if row["status"] == "budget_paused" and new_budget is None:
                raise ValueError("需要设置新的有限预算")
            if new_budget is not None:
                limit = extend_budget(run_id, new_budget)
                options = options.model_copy(update={"token_budget": limit})
            try:
                snapshot = load_snapshot(Path(row["workspace_path"]))
                verify_baseline(snapshot)
                cp = db.last_checkpoint(run_id)
                if cp and cp["ws_hash"] != workspace_hash(snapshot.root):
                    raise ValueError("工作区与最后断点不一致")
            except (ValueError, OSError):
                db.set_error(
                    run_id, "workspace_drift", "工作区或测试基线变化；请保留现场并核查", "interrupted"
                )
                raise ValueError("工作区发生漂移") from None
            db.claim_run(run_id, [row["status"]])
            log_event("run_started", run_id=run_id)
            client = None
            try:
                runtime_settings = self.settings.model_copy(
                    update={
                        "model_timeout": saved["_model_timeout"],
                        "sandbox_timeout": saved["_sandbox_timeout"],
                    }
                )
                client = (
                    DemoClient(row["task_id"], run_id)
                    if options.mode == "offline_demo"
                    else self.model_factory(runtime_settings, options)
                )

                def decide(messages):
                    meter = UsageMeter(run_id)
                    if meter.unknown_calls:
                        raise UsageUnknown()
                    if meter.total_tokens >= options.token_budget:
                        raise BudgetExhausted()
                    phase = "plan" if messages[0]["content"] == PLAN_PROMPT else "execute"
                    schema = (
                        decision_schema(registry)
                        if options.structured_output and phase == "execute"
                        else None
                    )
                    request_id = uuid.uuid4().hex
                    db.begin_model_call(run_id, request_id, phase)
                    log_event("model_request", run_id=run_id, request_id=request_id)
                    try:
                        if options.structured_output and phase == "execute":
                            reply = client.chat(messages, response_schema=schema)
                        else:
                            reply = client.chat(messages)
                    except Exception as exc:
                        raise UsageUnknown(model_error_detail(exc)) from None
                    db.finish_model_call(request_id, reply)
                    log_event("model_received", run_id=run_id, request_id=request_id)
                    if UsageMeter(run_id).unknown_calls:
                        raise UsageUnknown()
                    return reply.text

                registry = ToolRegistry(
                    github=options.use_github, sandbox_timeout=runtime_settings.sandbox_timeout
                )
                loop = HarnessLoop(
                    snapshot.root,
                    row["goal"],
                    decide,
                    run_id=run_id,
                    task_id=row["task_id"],
                    token_budget=options.token_budget,
                    meter=UsageMeter(run_id),
                    context_compress=options.context_compress,
                    keep_recent_steps=options.keep_recent_steps,
                    use_plan=options.use_plan,
                    use_skills=options.use_skills,
                    use_mcp=options.use_mcp,
                    mcp_servers=[
                        {
                            "name": "demo",
                            "cmd": [
                                sys.executable,
                                str(BASE_DIR / "app/mcp/demo_server.py"),
                                "--db-dir",
                                str(self.settings.data_dir.resolve()),
                            ],
                            "readonly": ["sqlite_query"],
                        }
                    ]
                    if options.use_mcp
                    else None,
                    use_memory=options.use_memory,
                    use_github=options.use_github,
                    registry=registry,
                )
                result = loop.resume() if resume else loop.run()
                verify_baseline(snapshot)
                if result["status"] == "failed":
                    db.set_error(run_id, "decision_invalid", "决策未满足协议，请检查trace后新建或人工恢复")
            except BudgetExhausted:
                from ..tools.approval import request_budget_continue

                request_budget_continue(run_id, UsageMeter(run_id).total_tokens, options.token_budget)
                db.set_error(
                    run_id, "budget_exhausted", "累计用量达到预算；请设置新的有限上限", "budget_paused"
                )
            except UsageUnknown as exc:
                detail = str(exc) or "model_usage_unknown"
                db.set_error(
                    run_id,
                    "usage_unknown",
                    f"{detail}；模型请求结果或用量未知，已暂停；不会自动重试",
                    "paused",
                )
            except Exception:
                pending = db.inspect_recovery(run_id)
                db.set_error(
                    run_id,
                    "action_unknown" if not pending["can_auto_resume"] else "execution_failed",
                    "执行中断，请检查工作区与动作记录",
                    "interrupted" if not pending["can_auto_resume"] else "failed",
                )
            finally:
                if client is not None:
                    try:
                        client.close()
                    except Exception:
                        pass
            return self.get(run_id)

    def _validate_dispatch(self, run_id, resume, new_budget):
        with self.database():
            row = self._load(run_id)
            self._options(row)
            if row["status"] == "done" or (not resume and row["status"] != "pending"):
                raise ValueError("当前状态不允许执行")
            if row["status"] == "budget_paused" and new_budget is None:
                raise ValueError("需要新的有限预算")
            if not db.inspect_recovery(run_id)["can_auto_resume"]:
                db.set_error(run_id, "action_unknown", "动作结果未知，不自动重放", "interrupted")
                raise ValueError("动作结果未知，不自动重放")
            used = db.call_totals(run_id)
            if used["unknown_calls"]:
                message = (
                    row["error_message"] if row["error_code"] == "usage_unknown" else None
                ) or "模型请求用量未知，需核查"
                db.set_error(run_id, "usage_unknown", message, "paused")
                raise ValueError("模型用量未知，不能续跑")
            if new_budget is not None and (
                type(new_budget) is not int or not used["tokens"] < new_budget <= 1000000
            ):
                raise ValueError("新预算必须大于已用量且不超过1000000")

    def start(self, run_id, *, resume=False, new_budget=None):
        with self._guard:
            if run_id in self._threads:
                raise ExecutionBusy("该run已在执行")
            if len(self._threads) >= 2:
                raise CapacityError("最多同时执行2个run")
            lease = execution_lock(self.settings.data_dir, run_id)
            lease.__enter__()
            try:
                self._validate_dispatch(run_id, resume, new_budget)
            except BaseException:
                lease.__exit__(None, None, None)
                raise

            def worker():
                try:
                    self._execute(run_id, resume=resume, new_budget=new_budget, _owned=True)
                except Exception:
                    with self.database():
                        db.set_error(
                            run_id, "dispatch_failed", "执行权或配置校验失败，请核查后手动重试", "interrupted"
                        )
                finally:
                    lease.__exit__(None, None, None)
                    with self._guard:
                        self._threads.pop(run_id, None)

            thread = threading.Thread(target=worker, daemon=True, name=f"run-{run_id}")
            self._threads[run_id] = thread
            try:
                thread.start()
            except BaseException:
                self._threads.pop(run_id, None)
                lease.__exit__(None, None, None)
                raise

    def submit(self, task_id, goal, options):
        with self._guard:
            if len(self._threads) >= 2:
                raise CapacityError("最多同时执行2个run")
            run = self.create(task_id, goal, options)
            self.start(run["run_id"])
            return self.get(run["run_id"])

    def wait(self, run_id, timeout=30):
        with self._guard:
            thread = self._threads.get(run_id)
        if thread:
            thread.join(timeout)
        return self.get(run_id)
