from utils import trim_whitespace


def test_trim_spaces():
    assert trim_whitespace("  hello  ") == "hello"


def test_trim_tabs_and_newlines():
    # 边界：strip 要处理 \t \n，不只是空格
    assert trim_whitespace("\t hi \n") == "hi"
