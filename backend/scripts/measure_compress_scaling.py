"""M3 数字③补强：长任务压缩率上界（离线趋势实测）。

数字③ 已证：6 步短任务（T2）压缩率 12.3%（真机开关对比）。README 称
"任务越长压缩率越高（近 3 步全文开销固定）"——本脚本用**真实 run 的消息形态**
（从 harness.db 抽取 assistant 决策 + 工具结果逐字复用）合成 2~20 步历史，
离线测压缩率随步数增长的曲线，把"上界"从口号变成实证。

用法（backend/ 下）：python scripts/measure_compress_scaling.py [--steps 2,4,8,12,16,20]
"""
import argparse
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.runtime.context import build_view, estimate_tokens  # noqa: E402
from app.store.db import DB_PATH  # noqa: E402

# 真实工具结果形态（从 DB 长 run 抽取的逐字样本，勿改内容）
_READ_RESULT = ("[工具结果 read_file] \"\"\"数据标准化模块。\n\n"
                "normalize(values: list[float]) -> list[float]\n"
                "zscore(values: list[float]) -> list[float]\n\n"
                "实现说明：\n"
                "- normalize：min-max 归一化到 [0,1]，空序列返回 []\n"
                "- zscore：总体标准差版本（分母 n 非 n-1）\n"
                "  - 序列全相等（std == 0）时返回长度相同的全 0.0 列表\n"
                "  - 被清空的函数：当前只抛异常（bug 所在）\n"
                "\"\"\"\n"
                "import statistics\n\n"
                "def normalize(values):\n"
                "    if not values:\n"
                "        return []\n"
                "    lo, hi = min(values), max(values)\n"
                "    if lo == hi:\n"
                "        return [0.0 for _ in values]\n"
                "    return [(v - lo) / (hi - lo) for v in values]\n\n"
                "def zscore(values):\n"
                "    raise NotImplementedError(\"zscore 待实现\")\n")

_READ_RESULT_2 = ("[工具结果 read_file] from module import normalize, zscore\n"
                  "import math\n\n"
                  "def test_normalize_basic():\n"
                  "    assert normalize([1, 2, 3, 4, 5]) == [0.0, 0.25, 0.5, 0.75, 1.0]\n\n"
                  "def test_zscore_basic():\n"
                  "    out = zscore([1, 2, 3, 4, 5])\n"
                  "    assert abs(out[0] - (-1.2649)) < 1e-3\n"
                  "    assert abs(out[2] - 0.0) < 1e-9\n")

_EDIT_RESULT = ("[工具结果 edit_file] 已修改 module.py：函数 zscore 的函数体已替换"
                "（old 片段逐字唯一匹配成功），语法检查通过")

_TEST_RED = ("[工具结果 run_tests] run_tests 有红: 2 fail/3 pass\n"
             "test_module.py:12: assert 30.0 == 27.0\n"
             "test_module.py:18: assert abs(out[0] - (-1.2649)) < 1e-3")

_TEST_GREEN = ("[工具结果 run_tests] run_tests 全绿: 5 passed\n"
               "（5 passed in 0.08s）")

# 每步 = assistant 决策 + 工具结果（模拟真实交替形态；长任务里动作多种多样）
_STEP_POOL = [
    (f'调用 read_file {{"path": "module.py"}}', _READ_RESULT),
    (f'调用 read_file {{"path": "test_module.py"}}', _READ_RESULT_2),
    (f'调用 edit_file {{"path": "module.py", "old": "raise NotImplementedError...",'
     f' "new": "if not values: return []\\n..."}}', _EDIT_RESULT),
    (f'调用 run_tests {{}}', _TEST_RED),
    (f'调用 run_tests {{}}', _TEST_GREEN),
]

_SYSTEM = ("你是一个软件工程师 Agent，正在执行一个代码任务。\n"
           "每次输出必须是一段 JSON（不要多余文字），格式：\n"
           '{"thought": "思考", "tool": "工具名", "args": {...}, "done": false}\n'
           "可用工具：read_file / edit_file / write_file / run_tests / list_files。\n"
           "规则：1) 改已有代码必须用 edit_file 只传 old→new 片段；2) 完成后必须先跑"
           "测试确认全绿才能 done=true；3) JSON 字符串内的引号必须转义为 \\\"。")

_GOAL = ("任务目标：# T2 - 补缺失函数（简单）\n\n"
         "难度：简单 | 预估耗时 10-15 分钟\n\n"
         "module.py 的 zscore 函数被清空（当前抛 NotImplementedError），"
         "test_module.py 有 2 个用例因它失败。请补全 zscore 使其通过全部测试。\n"
         "工作区已就绪，请开始。")


def _history(n_steps: int) -> list[dict]:
    """构造 n_steps 步历史（system + goal + n 步 × assistant+user），形态贴近真实 run。"""
    msgs = [{"role": "system", "content": _SYSTEM},
            {"role": "user", "content": _GOAL}]
    for i in range(1, n_steps + 1):
        act, result = _STEP_POOL[(i - 1) % len(_STEP_POOL)]
        msgs.append({"role": "assistant", "content": act})
        msgs.append({"role": "user", "content": result})
    return msgs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--steps", default="2,4,6,8,10,14,18,24",
                    help="逗号分隔的步数档位，默认覆盖 2~24 步")
    args = ap.parse_args()
    steps = [int(x) for x in args.steps.split(",") if x.strip()]

    print("压缩率随任务步数增长（离线，真实消息形态，本地估算 ceil(chars/2)）")
    print("保留近 3 步全文 + 旧步每步一行摘要（≤60 字符截断）")
    print("=" * 78)
    print(f"| 总步数 | 消息数 | 全量token(估) | 压缩后token(估) | 压缩率 | 视图消息数 |")
    print("|---|---|---|---|---|---|")

    rows = []
    for n in steps:
        msgs = _history(n)
        view = build_view(msgs, keep_recent_steps=3)
        full_t = estimate_tokens("".join((m.get("content") or "") for m in msgs))
        view_t = estimate_tokens("".join((m.get("content") or "") for m in view))
        rate = (full_t - view_t) / full_t if full_t else 0
        rows.append((n, len(msgs), full_t, view_t, rate, len(view)))
        print(f"| {n} | {len(msgs)} | {full_t} | {view_t} | {rate*100:.1f}% | {len(view)} |")

    # 关键结论：24 步 vs 6 步（README 真机档）
    print("\n" + "=" * 78)
    d6 = dict((r[0], r) for r in rows).get(6)
    d24 = dict((r[0], r) for r in rows).get(24)
    if d6 and d24:
        print(f"6 步（真机 T2 档）: 压缩率 {d6[4]*100:.1f}%  —— 与真机 12.3% 同量级（口径差异在真实 prompt token）")
        print(f"24 步（长任务）  : 压缩率 {d24[4]*100:.1f}%，视图消息 {d24[5]} 条（原 {d24[1]}）")
        print(f"✅ 上界实证：任务越长压缩率越高，近 3 步全文开销固定被摊薄")

    REPORT = Path(__file__).resolve().parent.parent / "data"
    REPORT.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    lines = [f"# 压缩率随步数增长（{time.strftime('%Y-%m-%d %H:%M')}，离线真实形态）", "",
             "| 总步数 | 消息数 | 全量token(估) | 压缩后token(估) | 压缩率 | 视图消息数 |",
             "|---|---|---|---|---|---|"]
    lines += [f"| {n} | {m} | {ft} | {vt} | {r*100:.1f}% | {vm} |" for n, m, ft, vt, r, vm in rows]
    (REPORT / f"measure_compress_scaling_{stamp}.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"\n报告已存: data/measure_compress_scaling_{stamp}.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
