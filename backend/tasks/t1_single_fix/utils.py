def trim_whitespace(text: str) -> str:
    """去掉字符串首尾的空白字符（空格、制表符、换行等）。"""
    return text.strip(" ")  # bug: 只去空格，不去制表符/换行
