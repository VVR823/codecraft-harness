"""Durable known usage and unknown-call accounting, shared across resume."""

import json

from ..store import db


class UsageMeter:
    def __init__(self, run_id):
        self.run_id = run_id

    @property
    def total_tokens(self):
        return db.call_totals(self.run_id)["tokens"]

    @property
    def unknown_calls(self):
        return db.call_totals(self.run_id)["unknown_calls"]

    def get(self, name, default=0):
        return self.total_tokens if name == "tokens" else default

    def over_budget(self, limit):
        return self.total_tokens >= limit


def extend_budget(run_id, new_limit=None):
    row = db.get_run(run_id)
    if row is None:
        raise ValueError("run不存在，不能扩额")
    options = json.loads(row["options_json"] or "{}")
    limit = new_limit if new_limit is not None else options.get("token_budget", 30000) + 30000
    if type(limit) is not int or not UsageMeter(run_id).total_tokens < limit <= 1_000_000:
        raise ValueError("新预算必须大于已用token且不超过1000000")
    db.save_budget(run_id, limit)
    return limit
