"""CodeCraft Harness API 入口（FastAPI）。

M0 占位路由：
- POST /api/tasks           创建一次 run（真实执行链 M1 接入 agent loop）
- GET  /api/tasks/{run_id}  查 run 状态
- POST /api/tasks/{run_id}/resume  从最后 checkpoint 续跑（M0 已具备读取能力）
"""
import uuid

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from .store import db

app = FastAPI(title="CodeCraft Harness", version="0.1.0")


class TaskCreate(BaseModel):
    task_id: str   # 指向 backend/tasks/<task_id>/ 的任务包目录
    goal: str = ""  # 自然语言目标（M1 起使用）


@app.on_event("startup")
def _startup() -> None:
    db.init_db()


@app.get("/health")
def health() -> dict:
    return {"ok": True, "app": "codecraft-harness", "version": "0.1.0"}


@app.post("/api/tasks")
def create_task(body: TaskCreate) -> dict:
    run_id = uuid.uuid4().hex[:12]
    db.create_run(run_id, body.task_id)
    return {"run_id": run_id, "task_id": body.task_id, "status": "pending"}


@app.get("/api/tasks/{run_id}")
def get_task(run_id: str) -> dict:
    run = db.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run 不存在")
    return dict(run)


@app.post("/api/tasks/{run_id}/resume")
def resume_task(run_id: str) -> dict:
    """从最后 checkpoint 续跑（M0：先提供读取能力，M1 接真恢复）。"""
    run = db.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run 不存在")
    cp = db.last_checkpoint(run_id)
    return {
        "run_id": run_id,
        "status": run["status"],
        "last_checkpoint": dict(cp) if cp else None,
        "message": "resume stub: M1 接入真实续跑",
    }
