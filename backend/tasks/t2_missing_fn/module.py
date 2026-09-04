"""数据标准化模块。

normalize() 依赖内部函数 _zscore()。当前 _zscore 被清空（抛 NotImplementedError），
导致依赖它的测试全红。你的任务：补全 _zscore 的实现。
"""
from __future__ import annotations


def _zscore(values: list[float]) -> list[float]:
    """把序列标准化为 z-score：每个值 (x - mean) / std。

    要求：
    - 用总体标准差（分母 = n，不是 n-1）
    - 序列全相等（std == 0）时返回长度相同的全 0.0 列表
    - 被清空的函数：当前只抛异常（bug 所在）
    """
    raise NotImplementedError("_zscore 待实现")


def normalize(values: list[float]) -> list[float]:
    """返回标准化序列；空序列返回 []（不进 zscore，避免除零）。"""
    if not values:
        return []
    return _zscore(values)
