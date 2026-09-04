"""订单结算（调用 pricing.py 的折扣规则）。

订单金额 = 逐行 单价 × 数量 × 折扣率，保留 2 位小数后累加。
"""
from __future__ import annotations

from pricing import bulk_discount_rate


def line_total(unit_price: float, qty: int) -> float:
    """单行小计 = 单价 × 数量 × 折扣率，保留 2 位小数。"""
    return round(unit_price * qty * bulk_discount_rate(qty), 2)


def order_total(lines: list[tuple[float, int]]) -> float:
    """整单合计。lines = [(单价, 数量), ...]，结果保留 2 位小数。"""
    return round(sum(line_total(unit_price, qty) for unit_price, qty in lines), 2)
