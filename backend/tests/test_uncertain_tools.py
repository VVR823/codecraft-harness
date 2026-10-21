import subprocess

import pytest

from app.runtime.loop import HarnessLoop, LoopError
from app.runtime.protocol import AgentStep
from app.store import db
from app.tools import registry


@pytest.mark.parametrize(
    "tool,args", [("git_push", {}), ("gh_create_pr", {"title": "fix", "body": "fixture"})]
)
def test_remote_timeout_keeps_uncertain_intent_and_cannot_replay(tmp_path, monkeypatch, tool, args):
    attempts = []

    def command(cmd, **kwargs):
        if cmd[0] == "fake-gh" or (cmd[0] == "git" and cmd[1] == "push"):
            attempts.append(cmd)
            raise subprocess.TimeoutExpired(cmd, 1)
        output = "https://github.com/example/fixture.git" if cmd[1] == "remote" else "fix"
        return subprocess.CompletedProcess(cmd, 0, output, "")

    monkeypatch.setattr(registry, "_find_gh", lambda: "fake-gh")
    monkeypatch.setattr(subprocess, "run", command)
    loop = HarnessLoop(
        tmp_path,
        "fixture",
        lambda _: pytest.fail("uncertain action must not replay"),
        registry=registry.ToolRegistry(github=True),
    )
    db.create_run(loop.run_id, "task")
    action = AgentStep(thought="deliver", tool=tool, args=args)
    with pytest.raises(Exception, match="结果未知"):
        loop._execute(action, 1)
    assert db.inspect_recovery(loop.run_id)["status"] == "interrupted"
    with pytest.raises(LoopError):
        loop.resume()
    assert len(attempts) == 1
    assert db.get_run(loop.run_id)["status"] == "interrupted"
