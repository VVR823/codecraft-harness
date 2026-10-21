# T3 - 跨文件 suite 红（中等）

难度：中等 | 预估 token：15~30k | 类型：跨文件 bug

## 目标
订单结算规则：**满 3 件打 9 折**。当前测试 suite 有 3 个测试红，根因横跨两个文件：
- `pricing.py`：批量折扣率的判定
- `order.py`：整单如何调用折扣率并汇总

只改 `pricing.py` 一行就够，但你要先读两个文件才能定位——别急着写。

## 线索
- `test_orders.py` 的 5 个测试是验收规格：3 红集中在"恰好满 3 件"的边界
- 提示：折扣率函数 `bulk_discount_rate` 的边界运算符疑似有误

## 验收
- `python -m pytest -q` 退出码 0（5 passed）
- 不修改 `test_orders.py` 与 `order.py`（测试和调用方是考卷；真凶在 `pricing.py`）
