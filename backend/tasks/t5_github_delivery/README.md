# 任务：修复 split_csv 的引号字段 bug 并交付 PR

交付仓库（工作区）里有一个 CSV 行解析器 `delivery_demo.py`，其中的 `split_csv(line)`
函数当前只按逗号盲切，遇到**双引号包裹、内部含逗号/空格**的字段会拆错（3 个测试失败）。
按 RFC4180 简化规则修复它：引号内的逗号是字段内容不是分隔符；引号包裹的字段剥掉
首尾引号；引号内连续两个双引号（""）是转义后的单个引号。

要求：
1. 先 run_tests 看失败详情，再 read_file 定位实现，用 edit_file 精准修复（不要整文件重写）。
2. 修到 run_tests 全部通过（5 passed）。
3. 修复全绿后，按 GitHub 交付流程把改动变成远端仓库上的一个 PR：
   新建分支 → 提交 → 推送 → 创建 PR（title 形如 "fix: split_csv 支持引号内逗号"，body 简述修复内容）。
   PR 创建成功（输出含 github.com 链接）后输出 done。
