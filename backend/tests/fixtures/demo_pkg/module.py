"""冒烟夹具：故意埋一个 bug 的 mini 任务包。"""


def add(a: int, b: int) -> int:
    return a + b          # 正确


def sub(a: int, b: int) -> int:
    return a + b          # bug：应为 a - b
