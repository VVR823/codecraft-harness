import importlib

import pytest

from app.runtime.model_client import ModelReply
from app.store import db


def test_known_usage_survives_resume_and_deduplicates():
    meter_type = importlib.import_module("app.runtime.usage").UsageMeter
    db.create_run("usage", "task")
    db.record_model_call("usage", "one", "execute", ModelReply("x", 28000, 1000, 29000), outcome="ok")
    assert meter_type("usage").total_tokens == 29000
    db.record_model_call("usage", "two", "plan", ModelReply("bad-json", 90, 10, 100), outcome="ok")
    db.record_model_call("usage", "two", "plan", ModelReply("bad-json", 90, 10, 100), outcome="ok")
    assert meter_type("usage").total_tokens == 29100


def test_unknown_call_remains_visible():
    meter_type = importlib.import_module("app.runtime.usage").UsageMeter
    db.create_run("unknown", "task")
    db.record_model_call("unknown", "one", "execute", ModelReply("{}"), outcome="ok")
    meter = meter_type("unknown")
    assert meter.total_tokens == 0
    assert meter.unknown_calls == 1


def test_budget_extension_is_finite():
    mod = importlib.import_module("app.runtime.usage")
    db.create_run("budget", "task")
    db.record_model_call("budget", "c", "execute", ModelReply("x", 30000, 1000, 31000), outcome="ok")
    assert mod.extend_budget("budget", None) == 60000
    with pytest.raises(ValueError):
        mod.extend_budget("budget", 30000)


def test_extending_unknown_run_is_a_validation_error():
    import pytest
    from app.runtime.usage import extend_budget

    with pytest.raises(ValueError, match="run"):
        extend_budget("missing", 100)
