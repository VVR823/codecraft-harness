# run_all 回归表（2026-09-04 14:45 | model=glm-4.5-flash | repeat=2）

| 任务 | 全绿/次数 | 成功率 | token中位数 | 步数中位数 | LLM调用中位 | 耗时(s)中位 |
|---|---|---|---|---|---|---|
| t1_single_fix | 2/2 | 100% | 7886 | 6 | 7 | 164 |
| t2_missing_fn | 2/2 | 100% | 11437 | 5 | 6 | 189 |
| t3_cross_file | 2/2 | 100% | 11780 | 6 | 8 | 160 |

## 明细
| run_id | 任务 | status | 全绿 | 步数 | token | LLM调用 | 失败原因 |
|---|---|---|---|---|---|---|---|
| d1823e4626e3 | t1_single_fix | done | ✅ | 7 | 10837 | 9 |  |
| 2502d4ef2844 | t1_single_fix | done | ✅ | 4 | 4935 | 5 |  |
| a42a3ab3bcd4 | t2_missing_fn | done | ✅ | 5 | 10023 | 6 |  |
| fea415abc6ca | t2_missing_fn | done | ✅ | 5 | 12851 | 7 |  |
| 593637040272 | t3_cross_file | done | ✅ | 6 | 12130 | 8 |  |
| d4bde24cf323 | t3_cross_file | done | ✅ | 6 | 11431 | 7 |  |