import importlib
import io
import json
import logging


def test_logs_have_stable_fields_and_redact_secrets():
    mod = importlib.import_module("app.logging_config")
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    handler.setFormatter(mod.EventFormatter())
    logger = logging.getLogger("codecraft.test")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)
    logger.info(
        "request failed Authorization: Bearer marker-secret CODECRAFT_API_KEY=another-marker",
        extra={"event": "model_error", "run_id": "r1", "step": 2, "request_id": "q1"},
    )
    item = json.loads(output.getvalue())
    assert item["run_id"] == "r1" and item["request_id"] == "q1" and item["event"] == "model_error"
    assert "marker-secret" not in output.getvalue() and "another-marker" not in output.getvalue()
    assert {"time", "level", "event", "run_id", "step", "request_id"} <= item.keys()
    logger.handlers = []
