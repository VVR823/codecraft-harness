from module import normalize


def test_normalize_basic():
    """z-score 序列：长度不变，和≈0。"""
    out = normalize([1.0, 2.0, 3.0, 4.0, 5.0])
    assert len(out) == 5
    assert abs(sum(out)) < 1e-9


def test_normalize_known_value():
    """[2,4,4,4,5,5,7,9]：mean=5，总体 std=2 → 首元素 (2-5)/2 = -1.5。"""
    out = normalize([2.0, 4.0, 4.0, 4.0, 5.0, 5.0, 7.0, 9.0])
    assert abs(out[0] - (-1.5)) < 1e-9


def test_normalize_constant_series():
    """常数列 std=0：不能除零，应返回全 0.0。"""
    assert normalize([3.0, 3.0, 3.0]) == [0.0, 0.0, 0.0]


def test_empty_returns_empty():
    assert normalize([]) == []
