"""Fixed, public T1/T2/T3 walkthroughs. No model, network, or arbitrary goals."""

import json

from .model_client import ModelReply
from .plan import PLAN_PROMPT
from ..store import db

PATCHES = {
    "t1_single_fix": [("utils.py", 'return text.strip(" ")', "return text.strip()")],
    "t2_missing_fn": [
        (
            "module.py",
            'raise NotImplementedError("zscore 待实现")',
            "if not values:\n        return []\n    mean = sum(values) / len(values)\n    std = (sum((x - mean) ** 2 for x in values) / len(values)) ** 0.5\n    return [(x - mean) / std for x in values] if std else [0.0] * len(values)",
        )
    ],
    "t3_cross_file": [
        (
            "pricing.py",
            "return BULK_RATE if qty > BULK_THRESHOLD else 1.0",
            "return BULK_RATE if qty >= BULK_THRESHOLD else 1.0",
        )
    ],
}


class DemoClient:
    def __init__(self, task_id, run_id):
        self.task_id, self.run_id = task_id, run_id

    def chat(self, messages, *, response_schema=None):
        if response_schema is not None:
            raise ValueError("离线演示不执行结构化模型请求")
        payload: dict
        if messages[0]["content"] == PLAN_PROMPT:
            payload = {"objective": "离线固定演示：测试、修复、再验证", "steps": []}
        else:
            script: list[dict] = [{"tool": "run_tests", "args": {}}]
            for path, old, new in PATCHES[self.task_id]:
                script += [
                    {"tool": "read_file", "args": {"path": path}},
                    {"tool": "edit_file", "args": {"path": path, "old": old, "new": new}},
                ]
            script += [{"tool": "run_tests", "args": {}}, {"done": True}]
            index = min(len(db.committed_actions(self.run_id)), len(script) - 1)
            payload = {"thought": "离线固定演示步骤", **script[index]}
        # Zero model tokens, not an estimate of real-model repair cost.
        return ModelReply(json.dumps(payload, ensure_ascii=False), 0, 0, 0)

    def close(self):
        pass
