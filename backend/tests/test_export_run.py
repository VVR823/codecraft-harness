import importlib
import json
import shutil
from pathlib import Path

import pytest

from app.runtime.run_service import RunService
from app.runtime.settings import Settings, RunOptions


def test_export_only_relative_patch_and_public_summary(tmp_path):
    mod = importlib.import_module("app.export_run")
    from app.runtime.model_client import ModelReply

    source = tmp_path / "source"
    shutil.copytree(Path(__file__).parents[1] / "tasks/t1_single_fix", source)
    (source / ".env").write_text("TOKEN=private-token")
    steps = iter(
        [
            {
                "tool": "edit_file",
                "args": {"path": "utils.py", "old": 'return text.strip(" ")', "new": "return text.strip()"},
            },
            {
                "tool": "write_file",
                "args": {"path": "diagnostic.py", "content": 'API_KEY="secret with spaces"\n'},
            },
            {"tool": "run_tests", "args": {}},
            {"done": True},
        ]
    )

    class Model:
        def chat(self, messages):
            return ModelReply(json.dumps({"thought": "test", **next(steps)}), 1, 1, 2)

        def close(self):
            pass

    service = RunService(
        Settings(data_dir=tmp_path / "data", base_url="http://127.0.0.1:9999/v1"),
        model_factory=lambda *a: Model(),
    )
    run = service.create("t1_single_fix", "", RunOptions(mode="local"), source=source)
    service.execute(run["run_id"])
    workspace = Path(run["workspace_path"])
    bundle = mod.export_run(service, run["run_id"], tmp_path / "export")
    text = json.dumps(bundle, ensure_ascii=False) + (tmp_path / "export/changes.patch").read_text(
        encoding="utf-8"
    )
    assert "secret with spaces" not in text and "private-token" not in text
    assert "ctx_messages" not in text and "任务目标" not in text
    assert str(tmp_path) not in text
    assert '-    return text.strip(" ")' in text
    assert "+    return text.strip()" in text
    assert bundle["evidence_kind"] == "local"
    assert bundle["verified"] is True
    assert (tmp_path / "export/result.json").is_file()
    with pytest.raises(ValueError):
        mod.export_run(service, run["run_id"], workspace / "export")


def test_export_rejects_changed_workspace_and_does_not_claim_old_verification(tmp_path):
    from app.export_run import result_bundle

    svc = RunService(Settings(data_dir=tmp_path / "data"))
    run = svc.create("t1_single_fix", "", RunOptions())
    assert svc.execute(run["run_id"])["verified"]
    (Path(run["workspace_path"]) / "utils.py").write_text('def trim_whitespace(text):\n    return "BROKEN"\n')
    with pytest.raises(ValueError, match="漂移|验证"):
        result_bundle(svc, run["run_id"])


def test_export_obeys_run_execution_lock(tmp_path):
    from app.export_run import result_bundle
    from app.runtime.execution_lock import execution_lock, ExecutionBusy

    svc = RunService(Settings(data_dir=tmp_path / "data"))
    run = svc.create("t1_single_fix", "", RunOptions())
    svc.execute(run["run_id"])
    with execution_lock(svc.settings.data_dir, run["run_id"]):
        with pytest.raises(ExecutionBusy):
            result_bundle(svc, run["run_id"])
