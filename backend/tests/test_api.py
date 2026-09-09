"""W13 Web 控制台 API 测试（隔离库 + TestClient，不真跑 LLM run）。

覆盖：
- GET /api/packs   任务包清单（tasks/ 下含 README 的目录）
- GET /api/runs    run 历史列表（步数/token 概要 join）
- GET /api/tasks/{run_id} 详情含 recent_traces（UI 时间线数据源）
- POST /api/tasks 参数校验语义（不存在任务 400；不测真建——会起线程烧 LLM）
- POST /api/tasks/{run_id}/resume 404 语义
- /console/ 静态控制台页面可访问

隔离：conftest autouse fixture 把 db.DB_PATH 指到临时库 → main.lifespan 的
init_db 建在临时库上，零接触真实 data/harness.db。
"""
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.store import db


@pytest.fixture()
def client():
    return TestClient(app)  # lifespan 触发 init_db（落在 conftest 的临时库上）


def test_packs_lists_task_dirs(client):
    d = client.get("/api/packs")
    assert d.status_code == 200
    packs = {p["task_id"]: p for p in d.json()["packs"]}
    # 内置任务包都在且带首行摘要
    for name in ("t1_single_fix", "t2_missing_fn", "t3_cross_file",
                 "t4_github_pipe_escape"):
        assert name in packs, f"缺少任务包 {name}"
        assert packs[name]["desc"].strip(), f"{name} 缺 desc"


def test_runs_list_shows_summary(client):
    db.create_run("aaa111", "t1_single_fix", "goal A")
    db.create_run("bbb222", "t2_missing_fn", "goal B")
    db.save_checkpoint("aaa111", 5, [], tokens_used=1234)
    db.bump_usage("aaa111", 1234)
    r = client.get("/api/runs")
    assert r.status_code == 200
    runs = r.json()["runs"]
    assert len(runs) == 2
    by_id = {x["run_id"]: x for x in runs}
    assert by_id["aaa111"]["status"] == "pending"
    assert by_id["aaa111"]["step"] == 5
    assert by_id["aaa111"]["tokens"] == 1234
    # 排序：新 → 旧（bbb222 后创建应排前）
    assert runs[0]["run_id"] == "bbb222"


def test_run_detail_includes_recent_traces(client):
    db.create_run("ccc333", "t3_cross_file", "goal C")
    db.save_checkpoint("ccc333", 2, [], tokens_used=99)
    db.bump_usage("ccc333", 99)
    for step, tool, verdict in ((1, "read_file", "ok"),
                                (2, "edit_file", "fail")):
        db.append_trace("ccc333", step, tool, "", "", 0, verdict)
    r = client.get("/api/tasks/ccc333")
    assert r.status_code == 200
    d = r.json()
    assert d["status"] == "pending"
    assert d["steps"] == 2
    assert d["tokens"] == 99
    tr = d["recent_traces"]
    assert len(tr) == 2
    assert tr[1]["tool"] == "edit_file" and tr[1]["verdict"] == "fail"
    assert "last_action" in d and d["last_action"] == "edit_file"


def test_detail_404_for_missing_run(client):
    assert client.get("/api/tasks/nope000").status_code == 404


def test_create_task_validation_400(client):
    """不存在的任务目录 → 400（不碰 db / 不起线程）。"""
    r = client.post("/api/tasks", json={"task_id": "not_a_pack"})
    assert r.status_code == 400


def test_resume_404_for_missing_run(client):
    r = client.post("/api/tasks/nope000/resume", json={})
    assert r.status_code == 404


def test_console_page_served(client):
    r = client.get("/console/")
    assert r.status_code == 200
    assert "CodeCraft Harness" in r.text
    assert "api/packs" in r.text and "api/runs" in r.text  # 页面消费新端点
