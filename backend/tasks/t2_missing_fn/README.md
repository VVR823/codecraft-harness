# T2 - 补缺失函数（简单）

难度：简单 | 预估 token：10~20k | 类型：补缺失函数

## 目标
`module.zscore` 被清空（只抛 `NotImplementedError`），导致依赖它的测试全红（4 failed）。
请补全 `zscore` 的实现，让 `pytest` 全绿（4 passed）。

## 线索
- 阅读 `test_module.py`：它就是你实现该遵守的"验收规格"
- z-score 公式：`(x - mean) / std`，**总体标准差**（分母 = n）
- 序列全相等时 `std == 0`，不能除零——测试要求返回全 `0.0` 的等长列表
- `module.py` 里只有 `zscore` 一个函数，整文件覆盖也不会误删其他代码

## 验收
- `python -m pytest -q` 退出码 0（4 passed）
- 不修改 `test_module.py`（测试是考卷，不是答题卡）
