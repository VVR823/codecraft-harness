"""z-score 标准化函数（唯一函数）。

当前实现被清空（抛 NotImplementedError），依赖它的测试全红。
你的任务：补全 zscore 的实现。文件里只有这一个函数，整文件覆盖是安全的。
"""
from __future__ import annotations


def zscore(values: list[float]) -> list[float]:
    """把序列标准化为 z-score：每个值 (x - mean) / std。

    要求：
    - 空序列返回 []
    - 用总体标准差（分母 = n，不是 n-1）
    - 序列全相等（std == 0）时返回长度相同的全 0.0 列表
    - 被清空的函数：当前只抛异常（bug 所在）
    """
    raise NotImplementedError("zscore 待实现")
