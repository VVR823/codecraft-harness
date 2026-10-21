"""No CLI auto-resume; manual resume still grants one bounded segment."""

import pytest
from scripts import drive_task


@pytest.mark.parametrize(
    "status,expected", [("paused", 2), ("budget_paused", 2), ("failed", 1), ("interrupted", 2)]
)
def test_cli_returns_after_one_attempt(monkeypatch, tmp_path, status, expected):
    calls = []

    class Service:
        def __init__(self, settings):
            pass

        def create(self, *args, **kwargs):
            return {"run_id": "one"}

        def execute(self, run_id):
            calls.append(run_id)
            return {
                "run_id": run_id,
                "status": status,
                "mode": "offline_demo",
                "verified": False,
                "known_tokens": 0,
                "unknown_calls": 0,
                "workspace_path": str(tmp_path),
            }

        def resume(self, *args):
            pytest.fail("automatic resume forbidden")

    monkeypatch.setattr(drive_task, "RunService", Service)
    assert drive_task.main(["t1_single_fix", "--data-dir", str(tmp_path)]) == expected
    assert calls == ["one"]


def test_max_steps_is_per_segment(monkeypatch, tmp_path):
    """段语义：run() 一段触顶 paused 后，resume() 获得全新配额继续跑。

    T4 run12 实证（旧绝对语义 bug）：run 在 30 步 paused 后 resume，step 从 30 起算，
    `while step < 30` 立即退出 → 一步都续不了。改段语义后每段（run/每次 resume）
    最多 MAX_STEPS 步。
    """
    import app.runtime.loop as loop_mod
    from app.runtime.loop import HarnessLoop
    from app.store import db

    monkeypatch.setattr(loop_mod, "MAX_STEPS", 3)
    # 600 行文件：乱序 offset 精读不会撞 dup/翻页护栏（护栏会让步不落 trace）
    (tmp_path / "x.py").write_text("\n".join(f"line{i:03d} = {i}" for i in range(1, 601)), encoding="utf-8")

    state = {"n": 0}

    def decider(messages):
        # decider 契约：返回 JSON 文本，loop 内 parse_step 解析
        state["n"] += 1
        off = (state["n"] * 37) % 500 + 1  # 伪随机 offset：异区间不重复、非接续不翻页
        return (
            '{"thought": "侦察", "tool": "read_file",'
            f' "args": {{"path": "x.py", "offset": {off}, "limit": 5}},'
            ' "done": false}'
        )

    loop = HarnessLoop(tmp_path, "测试目标", decider=decider)
    r1 = loop.run()
    assert r1["status"] == "paused" and r1["steps"] == 3  # 段1：step 1-3
    r2 = loop.resume()
    assert r2["status"] == "paused" and r2["steps"] == 6  # 段2：step 4-6 真跑了
    # 过滤空转雷达的观测 trace（tool 为空，非真实动作）后，6 步全执行
    traced = sorted(t["step"] for t in db.list_traces(loop.run_id) if t["tool"])
    assert traced == [1, 2, 3, 4, 5, 6]  # 6 个动作都执行了
