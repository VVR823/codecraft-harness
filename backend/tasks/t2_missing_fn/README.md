# T2 - 补缺失函数（简单）

难度：简单 | 预估 token：10~20k | 类型：补缺失函数

## 目标
`module._zscore` 被清空（只抛 `NotImplementedError`），导致 `normalize()` 依赖的
3 个测试失败。请补全 `_zscore` 的实现，让 `pytest` 全绿（4 passed）。

## 线索
- 阅读 `test_module.py`：它就是你实现该遵守的"验收规格"
- z-score 公式：`(x - mean) / std`，**总体标准差**（分母 = n）
- 序列全相等时 `std == 0`，不能除零——测试要求返回全 `0.0` 的等长列表
- 空序列由 `normalize` 兜底返回 `[]`，不会进 `_zscore`，无需在 `_zscore` 处理

## 验收
- `python -m pytest -q` 退出码 0（4 passed）
- 不修改 `test_module.py`（测试是考卷，不是答题卡）
