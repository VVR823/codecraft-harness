"""定价规则（被 order.py 调用）。

当前批量折扣的边界条件有 bug：满 BULK_THRESHOLD 件应打 9 折，
但实现只在【超过】阈值时才打折，恰好等于阈值时漏掉折扣。
"""

BULK_THRESHOLD = 3     # 满 3 件起批量折扣
BULK_RATE = 0.9        # 批量 9 折


def bulk_discount_rate(qty: int) -> float:
    """返回该数量适用的折扣率：qty >= BULK_THRESHOLD 打 9 折，否则原价 1.0。

    bug：当前写成 qty > BULK_THRESHOLD，等于阈值时不打折。
    """
    return BULK_RATE if qty > BULK_THRESHOLD else 1.0
