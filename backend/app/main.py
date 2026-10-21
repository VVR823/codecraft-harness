"""Single-worker local API backed by the same RunService as the CLI."""

from contextlib import asynccontextmanager
import sqlite3
import secrets
import time
import uuid
from fastapi import FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from .config import BASE_DIR
from .runtime.execution_lock import ExecutionBusy
from .runtime.run_service import RunService, CapacityError
from .runtime.settings import Settings, Mode
from .api_security import RateLimiter
from .logging_config import configure_logging, log_event
from .store.migrations import MigrationRequired


class TaskCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str = Field(min_length=1, max_length=100)
    goal: str = Field(default="", max_length=20000)
    mode: Mode | None = None
    model: str | None = Field(default=None, min_length=1, max_length=128)
    token_budget: int | None = Field(default=None, ge=1, le=1000000, strict=True)
    use_plan: bool = False
    context_compress: bool = False
    structured_output: bool = False
    use_skills: bool = False
    use_mcp: bool = False
    use_memory: bool = False


class ResumeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approve_budget: bool = False
    new_budget: int | None = Field(default=None, ge=1, le=1000000, strict=True)


def create_app(settings=None, service=None, clock=time.monotonic):
    config = settings or (service.settings if service else Settings.from_env())

    @asynccontextmanager
    async def lifespan(app):
        configure_logging(config.log_format)
        try:
            get_service()
        except (ValueError, OSError, sqlite3.Error, MigrationRequired):
            app.state.storage_error = True
        yield

    app = FastAPI(title="CodeCraft Harness", version="0.2.0", lifespan=lifespan)
    app.state.service = service
    app.state.storage_error = False
    limiter = RateLimiter(limit=config.rate_limit)

    @app.middleware("http")
    async def boundaries(request, call_next):
        request_id = uuid.uuid4().hex
        if request.url.path.startswith("/api/"):
            token = config.api_token.get_secret_value()
            supplied = request.headers.get("Authorization", "")
            if token and not secrets.compare_digest(supplied.encode(), ("Bearer " + token).encode()):
                return JSONResponse(
                    {"detail": "需要有效的Bearer token", "request_id": request_id},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
            if request.method == "POST":
                allowed, retry = limiter.consume(
                    request.client.host if request.client else "unknown", clock()
                )
                if not allowed:
                    return JSONResponse(
                        {"detail": "提交过于频繁", "request_id": request_id},
                        status_code=429,
                        headers={"Retry-After": str(retry)},
                    )
                size = 0
                chunks = []
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > 65536:
                        return JSONResponse(
                            {"detail": "请求体超过64KiB", "request_id": request_id}, status_code=413
                        )
                    chunks.append(chunk)
                request._body = b"".join(chunks)
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        log_event("http_request", request_id=request_id)
        return response

    def get_service():
        if app.state.service is None:
            app.state.service = RunService(config)
        return app.state.service

    def call(fn, *, conflict=False):
        try:
            return fn()
        except KeyError:
            raise HTTPException(404, detail="run不存在") from None
        except CapacityError:
            raise HTTPException(429, detail="最多同时执行2个run", headers={"Retry-After": "2"}) from None
        except ExecutionBusy:
            raise HTTPException(409, detail="该run已有执行者") from None
        except ValueError as exc:
            raise HTTPException(409 if conflict else 400, detail=str(exc)) from None
        except (OSError, sqlite3.Error, MigrationRequired):
            raise HTTPException(503, detail="存储不可用；旧数据库需显式备份迁移") from None

    @app.get("/health")
    def health():
        return {"ok": True, "app": "codecraft-harness", "version": "0.2.0"}

    @app.get("/ready")
    def ready():
        try:
            svc = get_service()
            config.validate_mode(config.options())
            with svc.database():
                from .store import db

                with db._conn() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    conn.rollback()
            return {"ready": True, "mode": config.default_mode}
        except (ValueError, OSError, sqlite3.Error, MigrationRequired):
            raise HTTPException(503, detail="配置或存储未就绪；请检查配置及schema版本") from None

    @app.get("/api/packs")
    def packs():
        result = []
        for p in sorted((BASE_DIR / "tasks").iterdir()):
            if p.is_dir() and not p.is_symlink() and (p / "README.md").is_file():
                result.append(
                    {
                        "task_id": p.name,
                        "desc": (p / "README.md").read_text(encoding="utf-8").splitlines()[0][:120],
                        "offline_demo": p.name in ("t1_single_fix", "t2_missing_fn", "t3_cross_file"),
                    }
                )
        return {"packs": result}

    @app.get("/api/runs")
    def runs(limit: int = Query(default=30, ge=1, le=100)):
        return call(lambda: {"runs": get_service().list(limit)})

    @app.post("/api/tasks")
    def create(body: TaskCreate):
        options = config.options(**body.model_dump(exclude={"task_id", "goal"}, exclude_none=True))
        return call(lambda: get_service().submit(body.task_id, body.goal, options))

    @app.get("/api/tasks/{run_id}")
    def get(run_id: str):
        return call(lambda: get_service().get(run_id))

    @app.get("/api/tasks/{run_id}/traces")
    def traces(
        run_id: str, limit: int = Query(default=50, ge=1, le=200), offset: int = Query(default=0, ge=0)
    ):
        return call(lambda: get_service().traces(run_id, limit, offset))

    @app.get("/api/tasks/{run_id}/result")
    def result(run_id: str):
        from .export_run import result_bundle

        return call(lambda: result_bundle(get_service(), run_id), conflict=True)

    @app.post("/api/tasks/{run_id}/resume")
    def resume(run_id: str, body: ResumeBody):
        def perform():
            svc = get_service()
            limit = body.new_budget
            if body.approve_budget and limit is None:
                limit = svc.get(run_id)["options"].get("token_budget", 30000) + 30000
            svc.start(run_id, resume=True, new_budget=limit)
            return svc.get(run_id)

        return call(perform, conflict=True)

    app.mount("/console", StaticFiles(directory=str(BASE_DIR / "ui"), html=True), name="console")
    return app


app = create_app()
