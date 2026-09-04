# run_all 回归表（2026-09-04 15:20 | model=glm-4.5-flash | repeat=2）

| 任务 | 全绿/次数 | 成功率 | token中位数 | 步数中位数 | LLM调用中位 | 耗时(s)中位 |
|---|---|---|---|---|---|---|
| t1_single_fix | 2/2 | 100% | 5369 | 4 | 6 | 192 |
| t2_missing_fn | 2/2 | 100% | 12826 | 6 | 7 | 334 |
| t3_cross_file | 2/2 | 100% | 14662 | 6 | 9 | 352 |

## 明细
| run_id | 任务 | status | 全绿 | 步数 | token | LLM调用 | 重试 | 失败原因 |
|---|---|---|---|---|---|---|---|---|
| 7206695814a0 | t1_single_fix | done | ✅ | 4 | 4983 | 5 | 0 |  |
| 1fff9b3e1fa5 | t1_single_fix | done | ✅ | 5 | 5755 | 6 | 0 |  |
| b9fda9d9c6a9 | t2_missing_fn | done | ✅ | 6 | 12088 | 7 | 0 |  |
| e39974b16408 | t2_missing_fn | done | ✅ | 5 | 13563 | 7 | 0 |  |
| b74bdaf6f844 | t3_cross_file | done | ✅ | 6 | 14519 | 9 | 0 |  |
| a37655007be4 | t3_cross_file | done | ✅ | 6 | 14806 | 9 | 0 |  |