---
name: pytest-green
description: 修复测试失败使 pytest 全绿时使用；面向代码 bug 修复类任务
---
# 让 pytest 全绿的方法论
1. 先 run_tests 拿结构化失败（红 N 条 + 文件:行 + 断言消息），别猜
2. read_file 看失败文件当前真实内容，再决定改哪里
3. 修改已有代码必须用 edit_file（old 逐字复制原文、只改片段），绝不整文件重写
4. 绝不修改测试文件（测试是考卷）
5. 改完立刻 run_tests；红了重复 1-4，全绿才 done
