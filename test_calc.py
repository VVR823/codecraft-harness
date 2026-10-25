from calc import add

def test_positive():
    assert add(2, 3) == 5

def test_signed():
    assert add(-2, 3) == 1
