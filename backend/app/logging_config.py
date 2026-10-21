"""Structured events deliberately exclude prompts, code, and exception bodies."""

from datetime import datetime, timezone
import json
import logging
import re


def redact(text):
    text = re.sub(r"(?i)Bearer\s+[^\s,;]+", "Bearer [REDACTED]", str(text))
    text = re.sub(
        r"""(?i)((?:[A-Z_]*(?:API_KEY|TOKEN|SECRET|PASSWORD))["']?\s*[=:]\s*)(?:"[^"\n]*"|'[^'\n]*'|[^\s,;]+)""",
        r"\1[REDACTED]",
        text,
    )
    return re.sub(r"\bsk-[A-Za-z0-9_-]+", "[REDACTED]", text)


class EventFormatter(logging.Formatter):
    def format(self, record):
        item = {
            "time": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "event": getattr(record, "event", "message"),
            "run_id": getattr(record, "run_id", ""),
            "step": getattr(record, "step", None),
            "request_id": getattr(record, "request_id", ""),
            "message": redact(record.getMessage()),
        }
        return json.dumps(item, ensure_ascii=False)


def configure_logging(format="json"):
    logger = logging.getLogger("codecraft")
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            EventFormatter() if format == "json" else logging.Formatter("%(levelname)s %(message)s")
        )
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


def log_event(event, *, run_id="", step=None, request_id=""):
    logging.getLogger("codecraft").info(
        event, extra={"event": event, "run_id": run_id, "step": step, "request_id": request_id}
    )
