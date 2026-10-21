from order import line_total, order_total


def test_single_item_no_discount():
    """不满批量阈值：原价。"""
    assert line_total(10.0, 1) == 10.0
    assert line_total(10.0, 2) == 20.0


def test_bulk_threshold_applies():
    """边界：恰好满 3 件应打 9 折（bug 漏掉这个 case）。"""
    assert line_total(10.0, 3) == 27.0   # 10*3*0.9


def test_bulk_beyond_threshold():
    """超过阈值：正常打 9 折。"""
    assert line_total(10.0, 5) == 45.0   # 10*5*0.9


def test_bulk_exact_three_via_order():
    """整单结算视角：恰好 3 件同款也要 9 折。"""
    assert order_total([(20.0, 3)]) == 54.0   # 20*3*0.9


def test_order_total_mixed():
    """混合订单：3 件打折款 + 1 件原价款。"""
    assert order_total([(10.0, 3), (10.0, 1)]) == 37.0   # 27 + 10
    assert order_total([(100.0, 1)]) == 100.0
