# T1 - 单测修复（入门）

难度：入门 | 预估 token：5~10k | 类型：单测修复

## 目标
utils.trim_whitespace 有一个 bug，导致 test_utils.py 里 1 个测试失败。
请修复它，让 `pytest` 全绿（2 passed）。

## 验收
- `python -m pytest -q` 退出码 0
- 不修改 test_utils.py（测试是考卷，不是答题卡）
