"""全局测试隔离（2026-09-06）：单测绝不读写真实 data/harness.db。

背景：此前 test_m2/memory/planner/stall_radar 直接连真库，跑完在 memories/
runs 等表留 mem_test_* / running 脏行——污染真实运行数据、memory 测试自身
读到别的测试残留（顺序依赖 flaky）、clone 后真库历史混进测试。

方案（autouse，每个测试生效）：
- db.DB_PATH monkeypatch 到 tmp_path 独立库（同名 harness.db）+ init_db 建表
- mcp_client_factory：spawn 指向 tmp 库的 demo server（--db-dir），mcp 测试
  也零接触真库（且 clone 机器无 data/harness.db 也能跑）

注意：db.py 是 `from ..config import DB_PATH` 导入 → db.DB_PATH 是 db 模块
自身属性，monkeypatch.setattr 有效；所有 db.* 函数运行时读模块属性。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

BACKEND_DIR = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path_factory, monkeypatch):
    """每个测试独占一个临时库（文件名也叫 harness.db，兼容 demo 白名单）。

    库目录用 tmp_path_factory.mktemp 独立创建——绝不放在测试的 tmp_path
    （ws 工作区）里：ws_hash 扫描工作区文件，库文件在 run 中反复写会触发
    漂移误判。
    """
    from app.store import db
    db_dir = tmp_path_factory.mktemp("isolated_db")
    db_file = db_dir / "harness.db"
    monkeypatch.setattr(db, "DB_PATH", db_file)  # Path（db.init_db 用 .parent）
    db.init_db()
    return db_dir  # tmp 库所在目录（mcp fixture 当 --db-dir 用）


@pytest.fixture()
def mcp_client_factory():
    """spawn 连 tmp 隔离库的 demo server（cwd 固定 backend/ 保证 -m 可导入）。

    用法: c = mcp_client_factory("srv"); c.start() ...
    默认 --db-dir 指向 _isolated_db 的 tmp 目录 → 只读查询落在隔离库上。
    """
    from app.mcp.mcp_client import MCPClient
    py = sys.executable

    def _make(server_name: str, db_dir: str | Path | None = None,
              read_timeout: float = 15.0) -> MCPClient:
        cmd = [py, "-m", "app.mcp.demo_server"]
        if db_dir is not None:
            cmd += ["--db-dir", str(db_dir)]
        c = MCPClient(server_name, cmd, read_timeout=read_timeout)
        c._cwd = str(BACKEND_DIR)
        return c

    return _make
