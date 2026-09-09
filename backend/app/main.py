"""CodeCraft Harness API 入口（FastAPI）。

M4 B4：API 与 CLI 行为对齐（补 M0 半成品）——
- POST /api/tasks 真驱动 HarnessLoop（后台线程执行），不再是只建 pending 行的壳；
  goal 缺省自动读任务包 README.md（与 drive_task/run_all 语义一致）
- GET  /api/tasks/{run_id} 是 progress 轮询点：run 状态 + 已执行步数 + token +
  最近 trace + 审批记录（供最简 UI / curl 轮询）
- POST /api/tasks/{run_id}/resume 暴露 approve_budget（预算暂停 run 的人工批准
  路径），且构造 loop 时复刻 CLI 的 token_budget/meter 护栏语义——经 API 的
  run 与 CLI 同护栏（B4 前 resume 不传 meter，护栏形同虚设）
- lifespan 替换已弃用的 @app.on_event("startup")

执行模型（与 scripts/run_all.py 对齐）：
- 任务包是同一份工作区（backend/tasks/<id>/，git 管理）→ 同任务并发 run 会互相
  污染 → 409 互斥（不同 task_id 目录不同，天然隔离）
- 起点还原 + 终态还原：全新 run 起跑前 git restore 回"带 bug 考卷"态、结束后
  （done/failed）还原；非终态（paused/budget_paused）保留现场——ws_hash 一致性
  是 resume 的前提，乱还原会让 resume 漂移拒绝
"""
import subprocess
import threading
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import BASE_DIR, DEFAULT_TOKEN_BUDGET, LLM_MODEL
from .store import db


def _restore_task(task_name: str) -> None:
    """把任务包还原到"带 bug 考卷"态（与 scripts/run_all.py 的 _restore_task 同源）。

    restore 还原 tracked 改动 + clean 删 AI 新建的 untracked 文件——两者都要，
    否则下一局 pytest 收集会被残留文件污染。幂等，重复调用安全。
    """
    repo_root = BASE_DIR.parent
    for cmd in (
        ["git", "-C", str(repo_root), "restore", "--", f"backend/tasks/{task_name}"],
        ["git", "-C", str(repo_root), "clean", "-fd", "--", f"backend/tasks/{task_name}"],
    ):
        try:
            subprocess.run(cmd, check=True, capture_output=True)
        except subprocess.CalledProcessError:
            pass


def _make_decider(model: str):
    """构造 decider + meter（与 run_all.run_once 同构）。

    meter["tokens"] 必须与 total_tokens 同步——API usage 只有 prompt/completion/
    total_tokens 三个键，护栏（loop 只读 meter["tokens"]）靠这个同步才生效。
    """
    usage_acc = {"prompt_tokens": 0, "completion_tokens": 0,
                 "total_tokens": 0, "tokens": 0}

    def decider(messages):
        from .runtime.llm import chat_with_usage
        text, usage = chat_with_usage(messages, model=model)
        usage_acc["prompt_tokens"] += usage.get("prompt_tokens", 0)
        usage_acc["completion_tokens"] += usage.get("completion_tokens", 0)
        usage_acc["total_tokens"] += usage.get("total_tokens", 0)
        usage_acc["tokens"] = usage_acc["total_tokens"]
        return text

    return decider, usage_acc


# run_id -> {"thread": Thread, "task_id": str, "error": str|None}
# 后台执行注册表（进程内存态；重启即清——run 持久状态在 SQLite，可 resume）
_ACTIVE: dict[str, dict] = {}
_ACTIVE_LOCK = threading.Lock()


def _active_count(task_id: str) -> int:
    with _ACTIVE_LOCK:
        return sum(1 for r in _ACTIVE.values() if r.get("task_id") == task_id)


def _start_thread(run_id: str, task_id: str, task_dir, goal: str, model: str,
                  use_plan: bool, token_budget: int, resume: bool,
                  approve_budget: bool) -> None:
    """后台线程执行 loop.run() 或 loop.resume()。绝不阻塞 HTTP 请求。"""

    def worker() -> None:
        try:
            from .runtime.loop import HarnessLoop
            if not resume:
                _restore_task(task_id)  # 全新 run：起点回到干净 bug 态
            decider, meter = _make_decider(model)
            loop = HarnessLoop(task_dir, goal, decider=decider, run_id=run_id,
                               task_id=task_id, token_budget=token_budget,
                               meter=meter, use_plan=use_plan)
            if resume:
                loop.resume(approve_budget=approve_budget)
            else:
                loop.run()
        except Exception as e:  # noqa: BLE001 - 后台线程任何异常都不能静默
            with _ACTIVE_LOCK:
                if run_id in _ACTIVE:
                    _ACTIVE[run_id]["error"] = f"{type(e).__name__}: {e}"
            cur = db.get_run(run_id)
            # 状态守卫：预算门拒绝保持 budget_paused、漂移拒绝已标 failed、done
            # 已收尾——都不覆盖；只有"真异常"才置 failed
            if cur is not None and cur["status"] not in ("done", "failed",
                                                         "budget_paused"):
                db.update_run_status(run_id, "failed")
        finally:
            cur = db.get_run(run_id)
            if cur is not None and cur["status"] in ("done", "failed"):
                _restore_task(task_id)  # 终态还原考卷（与 run_all try/finally 同标准）
            with _ACTIVE_LOCK:
                _ACTIVE.pop(run_id, None)

    t = threading.Thread(target=worker, daemon=True, name=f"run-{run_id}")
    with _ACTIVE_LOCK:
        _ACTIVE[run_id] = {"thread": t, "task_id": task_id, "error": None}
    t.start()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="CodeCraft Harness", version="0.1.0", lifespan=lifespan)

# W13 最简 Web 控制台：纯静态单页（原生 JS 轮询 /api/*，不引前端框架）
# 页面挂 /console，API 路径不动——控制台只是现有 API 的另一种消费方
_UI_DIR = BASE_DIR / "ui"
if _UI_DIR.is_dir():
    app.mount("/console", StaticFiles(directory=str(_UI_DIR), html=True), name="console")


class TaskCreate(BaseModel):
    task_id: str                    # 指向 backend/tasks/<task_id>/ 的任务包目录
    goal: str = ""                  # 自然语言目标；空则自动读任务包 README.md
    model: str | None = None        # 默认 config.LLM_MODEL（与 CLI 一致）
    use_plan: bool = False          # M4 planner 开关（P5 定稿默认关）
    token_budget: int | None = None  # None → config.DEFAULT_TOKEN_BUDGET（与 CLI 一致）


class ResumeBody(BaseModel):
    approve_budget: bool = False    # budget_paused run 的人工批准续跑
    model: str | None = None
    token_budget: int | None = None


@app.get("/health")
def health() -> dict:
    return {"ok": True, "app": "codecraft-harness", "version": "0.1.0"}


@app.get("/api/packs")
def list_packs() -> dict:
    """任务包清单（UI 下拉）：tasks/ 下含 README.md 的子目录 + 首行摘要。"""
    packs = []
    for d in sorted((BASE_DIR / "tasks").iterdir()):
        if not d.is_dir():
            continue
        readme = d / "README.md"
        if not readme.exists():
            continue
        first = readme.read_text(encoding="utf-8").strip().splitlines()
        desc = first[0].strip() if first else ""
        packs.append({"task_id": d.name, "desc": desc[:120]})
    return {"packs": packs}


@app.get("/api/runs")
def list_runs_api(limit: int = 30) -> dict:
    """run 历史列表（UI 侧栏）：概要字段，详情走 GET /api/tasks/{run_id}。"""
    return {"runs": [dict(r) for r in db.list_runs(limit=limit)]}


@app.post("/api/tasks")
def create_task(body: TaskCreate) -> dict:
    task_dir = BASE_DIR / "tasks" / body.task_id
    if not task_dir.is_dir():
        raise HTTPException(status_code=400,
                            detail=f"任务目录不存在: {task_dir}")
    if _active_count(body.task_id):
        raise HTTPException(
            status_code=409,
            detail=f"任务 {body.task_id} 已有 run 在执行中——任务包是同一份工作区，"
                   "不支持并发（等它结束，或先 resume/处理暂停的 run）")
    goal = body.goal or ""
    if not goal:
        readme = task_dir / "README.md"
        if readme.exists():
            goal = readme.read_text(encoding="utf-8")
    run_id = uuid.uuid4().hex[:12]
    db.create_run(run_id, body.task_id, goal)
    model = body.model or LLM_MODEL
    budget = (body.token_budget if body.token_budget is not None
              else DEFAULT_TOKEN_BUDGET)
    _start_thread(run_id, body.task_id, task_dir, goal, model,
                  body.use_plan, budget, resume=False, approve_budget=False)
    return {"run_id": run_id, "task_id": body.task_id, "status": "running",
            "model": model, "token_budget": budget,
            "progress_url": f"/api/tasks/{run_id}"}


@app.get("/api/tasks/{run_id}")
def get_task(run_id: str) -> dict:
    """progress 轮询点：run 状态 + 已执行步数 + token + 最近动作 + 审批记录。"""
    run = db.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run 不存在")
    with _ACTIVE_LOCK:
        active = run_id in _ACTIVE
        error = _ACTIVE.get(run_id, {}).get("error")
    cp = db.last_checkpoint(run_id)
    usage = db.get_usage(run_id)
    plan = db.get_plan(run_id)
    approvals = db.list_approvals(run_id)
    traces = db.list_traces(run_id)
    last = traces[-1] if traces else None
    return {
        "run_id": run["run_id"], "task_id": run["task_id"],
        "goal": run["goal"], "status": run["status"],
        "created_at": run["created_at"], "updated_at": run["updated_at"],
        "active": active, "error": error,
        "steps": int(cp["step"]) if cp else 0,
        "tokens": (int(usage["total_tokens"]) if usage
                   else (int(cp["tokens_used"]) if cp else 0)),
        "last_action": (last["tool"] or "agent") if last else None,
        "last_trace_step": int(last["step"]) if last else None,
        "plan_status": plan["status"] if plan else None,
        "approvals": [dict(a) for a in approvals[-5:]],
        "recent_traces": [{"step": int(t["step"]), "tool": t["tool"],
                           "ts": t["ts"], "verdict": t["verdict"]}
                          for t in traces[-12:]],   # UI 动作时间线（最近 12 步）
    }


@app.post("/api/tasks/{run_id}/resume")
def resume_task(run_id: str, body: ResumeBody) -> dict:
    """从最后 checkpoint 真续跑（后台线程）。

    approve_budget=true：批准预算超限暂停（budget_paused）的 run 续跑——审批记录
    落 approvals 表（与 CLI demo_budget 同一语义）。
    """
    run = db.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run 不存在")
    with _ACTIVE_LOCK:
        if run_id in _ACTIVE:
            raise HTTPException(status_code=409, detail="该 run 正在执行中")
    if _active_count(run["task_id"]):
        raise HTTPException(status_code=409,
                            detail="同任务已有 run 在执行中（工作区互斥）")
    # 预算门前置到请求线程同步拒绝（不让后台线程抛异常——错误随注册表 pop 丢失，
    # curl 只能靠返回文案猜）。approve=true 才启动线程；worker 的 LoopError 兜底仍在防竞态
    if run["status"] == "budget_paused" and not body.approve_budget:
        raise HTTPException(
            status_code=409,
            detail="run 因超限暂停（budget_paused），需 approve_budget=true 才可续跑"
                   "（审批记录落 approvals 表）")
    task_dir = BASE_DIR / "tasks" / run["task_id"]
    if not task_dir.is_dir():
        raise HTTPException(status_code=400,
                            detail=f"任务目录不存在: {task_dir}")
    model = body.model or LLM_MODEL
    budget = (body.token_budget if body.token_budget is not None
              else DEFAULT_TOKEN_BUDGET)
    # use_plan=False：resume 语义里规划要么已注入 checkpoint 消息（有 plan 行），
    # 要么无需规划——只有"0 步且从未规划"的极端边界会走到 run() 重新规划，API 场景
    # （进程内存活的后台线程）几乎不会发生，不为此扩 runs 表 schema
    _start_thread(run_id, run["task_id"], task_dir, run["goal"] or "",
                  model, False, budget, resume=True,
                  approve_budget=body.approve_budget)
    return {"run_id": run_id, "status": "running",
            "note": ("预算批准已生效，续跑中"
                     if body.approve_budget else
                     "未批准预算：run 若为 budget_paused 将保持暂停，"
                     "需 approve_budget=true 才续跑")}
