# T4 - 真实开源库回归修复（进阶）

难度：进阶 | 预估 token：8~15k | 类型：真实第三方库 bug 修复

## 背景

本任务包是知名开源库 **tabulate**（GitHub 上 2k+ star 的纯 Python 表格格式化库）
的一个真实回归场景。代码来自该库真实源码（`tabulate/` 目录），测试来自该库
作者自己写的回归测试（`test/test_regression.py`，收录 issue #241 的修复验证）。

## 目标

`test/test_regression.py` 里有一个回归测试失败：`test_github_escape_pipe_character`。
它断言 tabulate 以 github 格式输出表格时，单元格内含 `|` 字符必须被正确转义。

请修复 `tabulate/__init__.py` 里的 bug，让该回归测试通过，
且**不影响其他 36 个已通过的回归测试**。

## 验收

- `python -m pytest -q` 退出码 0（修复后应为 37 passed, 5 skipped）
- 不得修改 `test/` 目录下任何文件（测试是考卷，不是答题卡）
- 只允许修改 `tabulate/__init__.py`（若确有必要动其他文件需在 thought 中说明理由）

## 提示

- 先读失败的回归测试，理解它期望的输出格式
- 再读 `tabulate/__init__.py` 里 github/pipe 表格格式的定义（`_table_formats`）
- 注意：本任务包是从真实库裁剪的最小复现集，`cli.py`/`__main__.py` 仅为保持包完整，与本 bug 无关
