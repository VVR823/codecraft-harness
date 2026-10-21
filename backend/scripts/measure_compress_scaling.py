"""Synthetic context-size trend; local estimates, no API tokens or repair claim."""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.runtime.context import build_view, estimate_tokens


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--steps", default="2,4,8,12,16,20")
    ap.add_argument("--output", type=Path, required=True)
    args = ap.parse_args()
    rows = []
    for count in [int(s) for s in args.steps.split(",")]:
        if not 1 <= count <= 100:
            ap.error("steps must be 1..100")
        messages = [
            {"role": "system", "content": "synthetic context test"},
            {"role": "user", "content": "synthetic task"},
        ]
        for i in range(count):
            messages += [
                {"role": "assistant", "content": f'调用 read_file {{"path":"file{i}.py"}}'},
                {"role": "user", "content": "[工具结果 read_file] " + ("synthetic public text " * 100)},
            ]
        full = estimate_tokens("".join(m["content"] for m in messages))
        compressed = estimate_tokens("".join(m["content"] for m in build_view(messages, 3)))
        rows.append(
            {
                "steps": count,
                "estimated_full_tokens": full,
                "estimated_compressed_tokens": compressed,
                "estimated_reduction": 1 - compressed / full,
            }
        )
    output = {
        "evidence_kind": "local_estimate",
        "verified_repair": None,
        "note": "合成上下文的本地估算，不是API token或修复效果。",
        "rows": rows,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(output, ensure_ascii=False))


if __name__ == "__main__":
    main()
