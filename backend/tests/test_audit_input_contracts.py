"""Public typed inputs and invalid endpoint configuration fail before dispatch."""

import pytest

from app.runtime.protocol import AgentStep, Tool
from app.runtime.run_service import RunService
from app.runtime.settings import RunOptions, Settings


@pytest.mark.parametrize("tool", list(Tool))
def test_decision_accepts_its_declared_tool_enum_values(tool):
    assert AgentStep(thought="inspect", tool=tool).tool_name == tool.value


@pytest.mark.parametrize("endpoint", ["http://127.0.0.1:not-a-port/v1", "http://localhost:65536/v1"])
def test_invalid_endpoint_port_is_rejected_before_run_creation(tmp_path, endpoint):
    service = RunService(Settings(data_dir=tmp_path / "data", base_url=endpoint))
    with pytest.raises(ValueError):
        service.create("t1_single_fix", "", RunOptions(mode="local"))
    assert service.list() == []
