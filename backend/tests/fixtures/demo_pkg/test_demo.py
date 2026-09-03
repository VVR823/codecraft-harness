from module import add, sub


def test_add_ok():
    assert add(2, 3) == 5      # 绿


def test_sub_should_pass():
    assert sub(5, 2) == 3      # 红（bug 在 module.sub）
