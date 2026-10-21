---
name: pytest-red-fix
description: 修复 pytest 测试失败（红测试）时使用；先跑测试看结构化失败，按文件:行定位，改代码后必须重跑验证
---
# pytest 红测试修复方法论
1. 先用 run_tests 拿到结构化失败（文件:行 + 断言消息）
2. 定位到失败的文件和行号，read_file 看现场
3. 只修代码，绝不修改测试文件（测试是考卷）
4. 改完立刻 run_tests 验证，红了继续，直到全绿
