# run_all 回归表（2026-09-04 13:13 | model=glm-4-air-250414 | repeat=2）

| 任务 | 全绿/次数 | 成功率 | token中位数 | 步数中位数 | LLM调用中位 | 耗时(s)中位 |
|---|---|---|---|---|---|---|
| t1_single_fix | 2/2 | 100% | 6638 | 5 | 7 | 11 |
| t2_missing_fn | 2/2 | 100% | 14602 | 6 | 10 | 15 |
| t3_cross_file | 2/2 | 100% | 7924 | 4 | 6 | 10 |

## 明细
| run_id | 任务 | status | 全绿 | 步数 | token | LLM调用 | 失败原因 |
|---|---|---|---|---|---|---|---|
| 4aa152fc1e12 | t1_single_fix | done | ✅ | 5 | 6586 | 7 |  |
| fd5cbc7002b2 | t1_single_fix | done | ✅ | 5 | 6690 | 7 |  |
| 7eb3b8f9de4a | t2_missing_fn | done | ✅ | 6 | 18147 | 11 |  |
| 3f7325b40de4 | t2_missing_fn | done | ✅ | 6 | 11058 | 8 |  |
| 26d8b87db0e5 | t3_cross_file | done | ✅ | 4 | 5404 | 5 |  |
| 7aa915af5f12 | t3_cross_file | done | ✅ | 5 | 10444 | 8 |  |