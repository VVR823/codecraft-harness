# CodeCraft Harness

[![CI](https://github.com/VVR823/codecraft-harness/actions/workflows/ci.yml/badge.svg)](https://github.com/VVR823/codecraft-harness/actions/workflows/ci.yml)

> 给一个自然语言工程目标，AI Agent 自主完成「规划 → 写码 → 跑测试 → 失败自修 → 全绿」的多轮长任务。
> 轻量沙箱隔离、可中断续跑、可审计回放、成本有护栏。**核心全部自研**（不引 LangChain）。

定位：秋招第二作品，主打**系统设计深度**——证明"能设计 AI 系统本身的工程骨架"。
配套文档：[执行计划 v2.2](docs/执行计划_v2.2.md)（grill 三轮拷问定稿，15 项决策有出处）·
[业界对标调研](docs/业界对标调研.md)（MyCoder/MiniCode/OpenHands/LangGraph 逐能力项对照，含面试话术速查）。

License: [MIT](LICENSE)（作者 文宇）

## 硬数字（2026-09-04~05 实测）

**数字① 回归通过率：6/6 全绿**（**免费** `glm-4.5-flash` × 3 任务 × 2 次，run_all 自愈重试≤2，详见 [报告](docs/run_all_report_free_2026-09-04.md)）

> 同套 harness 用付费 `glm-4-air-250414` 也是 6/6——**证明架构正确，模型只是变量**（早期用更老的免费 `glm-4-flash` 连败，是靠护栏演进 + 换免费新代才拉满）。

**数字② 杀进程恢复率：3/3 = 100%**（T1 在 3 个不同决策点 kill -9，resume 全部续跑至全绿且不重放，详见 [报告](docs/resume_and_compress_report_2026-09-04.md)）

> 度量的是 **resume 机制可靠性**：机制性失败（漂移/预算）立即判 FAIL，LLM 偶发抖动走 ≤2 次自愈重试（与 run_all 同口径）——3/3 里无一次是机制失败。

**数字③ 上下文压缩率：12.3%，压缩且绿**（T2 真机开关对比：单步 prompt 中位 1,770→1,553 token；开压缩后仍 6 步全绿、步数/调用数与关压缩一致——**压缩不损正确性**）

> 压缩只影响 decider 视图，`self.messages` 全量存档（resume/审计不丢信息）。12.3% 是 6 步短任务的下界（近 3 步全文占大半上下文）；**长任务趋势实测**（离线真实消息形态）：24 步任务压缩率 55.5%、视图消息从 50 条恒定压到 9 条——近 3 步全文开销固定被摊薄，任务越长越省（详见 [报告](docs/resume_and_compress_report_2026-09-04.md)）。

**数字④ planner A/B：健康期 6/6 全绿（plan 不破坏 self-repair），短任务纯开销 + 放大免费限流风险 → 默认关**（glm-4.5-flash 真机背靠背补测：no-plan 3/3、plan 健康期 T1~T3 各 2 次 6/6；总 token T1 +4,665 / T2 +1,952 / T3 +3,636——**初版"T2 −953"为单次运气已证伪**；T2 稳定省 1 步（6→5）。补测失败样本全为 429 限流高压期污染，另揭示 plan 隐藏成本：token 消耗更高更易撞免费账户限流；详见 [报告](docs/planner_ab_report_2026-09-05.md) + [背靠背补测](docs/planner_backtoback_report_2026-09-05.md)）

> planner = **advisory plan + agentic execution 两段式**：执行前先一次规划（~1k token）注入上下文当参考，执行仍让 LLM 每步自选工具（保住 self-repair，不引 LangGraph）。**T2（多文件侦察型）plan 稳定省 1 步（6→5，2/2 复现）但不省 token**（背靠背 +1,952）——价值在长任务/复杂任务放大（与压缩同构），短任务规划是开销下界。故默认关（数字① 回归口径不动），`--plan` 按需开。

| 任务 | 难度 | 全绿(免费) | token 中位 | 步数中位 | LLM 调用 | 耗时 |
|---|---|---|---|---|---|---|
| T1 单测修复 | 入门 | 2/2 | 5,369 | 4 | 6 | ~192s |
| T2 补缺失函数 | 简单 | 2/2 | 12,826 | 6 | 7 | ~334s |
| T3 跨文件 bug | 中等 | 2/2 | 14,662 | 6 | 9 | ~352s |

单次 run 实际 token 5k~18k，免费档成本≈0；付费 air 单次约 1~3 分钱。最大单次 18,147 token（预算护栏校准基数 ≈27k）。

**数字⑤ 真实开源库端到端验证：13 个失败样本 → 13 个 harness 缺陷全修，收官 3 局成功**（免费 `glm-4.5-flash` 在**真实开源库 tabulate** 上定位并修复一个历史 bug；600 行回归测试 + 2897 行全量源码、目标测试在文件倒数第 5 行，详见 [T4 战报](docs/t4_real_library_report_2026-09-07.md)）

> T1~T3 是自写"考卷"，T4 换**真实开源库**检验脏活：前 13 局失败逐一逼出 harness 自身缺陷（消息层二次截断/工具白名单漏门/配额段语义 bug…），每修一个都带回归测试；修复后 run13 resume 首胜（33 步）、run14 全自动完成（35 步，auto-resume×2）、run16 单段一次过（**20 步 11 分钟**，空转治理 + ToolError 喂回见效）。中间 run15 又暴露一个缺陷（工具错误崩局）已修复——**失败全部归因 harness，不甩锅模型**。方法论沉淀：新增工具 = registry + prompt + protocol 三处联动；先查"模型看到的内容是否完整"，再怀疑模型。

**MVP 底线五条进度（2026-09-04 五条全证，②③ 已有独立真机实测）**

| # | 底线 | 状态 | 证据 |
|---|---|---|---|
| 1 | 长任务自修到绿 | ✅ | 真 LLM 全链路 run 数十次，T1~T3 全绿 |
| 2 | 进程杀死可恢复（resume 不重放） | ✅ | **M3 专项实测 3/3**：T1 杀点 2/3/4 全部续跑至全绿（`run_resume_test.py`，退出码分类 + 自愈重试）；预算 demo resume 日志"已完成 3 个动作（不重放）"复证 |
| 3 | 工作区漂移可识别（ws_hash） | ✅ | `tests/test_m2.py`：改文件后 resume 拒绝（"工作区漂移"），未改则正常续跑 |
| 4 | 预算护栏（超限暂停+人工批准） | ✅ | `scripts/demo_budget.py` 真机证据：3940 token 超 3000 预算 → budget_paused → approvals 表 requested+approved → resume 续跑至绿 |
| 5 | T1~T3 全绿 + 预算内 | ✅ | 上方回归表 |

## 架构

```
backend/
├── app/
│   ├── config.py              # 全局配置（沙箱超时/步数上限/预算）
│   ├── main.py                # FastAPI：/api/tasks 提交 → 查询 → resume
│   ├── runtime/
│   │   ├── loop.py            # 自研 agent loop：决策→执行→checkpoint→trace
│   │   ├── protocol.py        # JSON 决策协议（thought/tool/args/done）+ Pydantic 校验
│   │   ├── memory.py          # 长期记忆（M5）：跨 run 经验沉淀/复用（成功路径/失败教训）
│   │   └── llm.py             # LLM 薄封装（openai 兼容，智谱，可换模型）
│   ├── tools/
│   │   ├── registry.py        # 工具注册表：权限分级 LOW/MED/HIGH + 工具说明书同源 + MCP 动态注册
│   │   └── sandbox_exec.py    # 轻量沙箱：复制执行 + utf-8 强制 + 超时进程树强杀
│   ├── skills/                # Skill 注册体系（M5）：SKILL.md 解析/发现/goal 语义匹配
│   ├── mcp/                   # MCP 最小实现（M5）：自研 stdio client + demo server
│   ├── verifier/pytest_runner.py  # pytest 结构化解析（红 N 条 / 文件:行 / 断言消息）
│   ├── store/db.py            # SQLite：runs/checkpoints/traces/usage/approvals/plans/memories（WAL）
│   └── trace/                 # 事件 trace 记录
├── tasks/                     # T1~T3 手写任务包（module + 测试 + README，git 作还原点）
├── scripts/                   # drive_task/run_all/run_resume_test/measure_*（实测工具）
├── tests/                     # 138 个单元测试（含真沙箱跑任务包 + GitHub 交付四工具真 git 链路 + API TestClient；conftest 隔离临时库，不碰 data/harness.db）
└── pytest.ini                 # 回归只收 tests/，排除任务包"考卷"
```

**设计要点**
- **决策协议**：LLM 每步输出一段 JSON（思考+工具+参数），Pydantic 校验失败自动重试并附纠错教学
- **edit_file 精准编辑**：改已有代码只输出 old→new 片段（规避整文件长 JSON 转义），write_file 仅新建；写 .py 即时语法检查
- **工具权限分级**：只读/沙箱 LOW，写工作区 MED，装包等 HIGH（审批流 W3 接）
- **结构化失败反馈**：测试红时喂给 LLM 的是 `test_orders.py:12: assert 30.0 == 27.0`，不是几十行原始日志
- **checkpoint/resume**：每步全量存档上下文，进程被杀从断点续跑、不重放已完成动作（实测 3/3）；resume 前比对工作区 hash 识别外部漂移
- **分层上下文压缩**（M3，可开关）：system+目标全文、近 3 步消息全文、更早历史每步压成一行摘要——只影响决策视图，全量消息仍存档，压缩与可恢复性正交
- **任务规划器**（M4，`--plan` 按需开）：执行前先一次规划（Pydantic 强校验 + 重试），计划注入上下文当 advisory 参考、随 checkpoint 持久化（resume 不重规划）——非硬调度，执行仍 agentic，保住 self-repair
- **Skills**（M5，`--skills` 按需开）：SKILL.md 目录协议自研实现（frontmatter name/description + 正文），按任务 goal 语义匹配注入 system——对标 Anthropic Skills 标准的轻量落地，不引框架
- **MCP**（M5，`--mcp` 按需开）：自研最小 stdio MCP client（initialize 握手/tools/list/tools/call），demo server 工具动态注册进工具表（`mcp_<server>__<tool>`），与本地工具同源同审计——协议自研不依赖官方 SDK
- **长期记忆**（M5，`--memory` 按需开）：run 结束沉淀（成功路径/失败教训 → memories 表），下次同任务 run 注入复用——短期记忆=checkpoint（断点续跑），长期记忆=跨 run 经验（不重踩坑）
- **沙箱隔离**：任务包复制执行、源目录只读；`git restore` 一键重置"考卷"

## 快速开始

```bash
# 1. 环境
cd backend
python -m venv .venv && .venv/Scripts/activate     # Windows
pip install -r requirements.txt
cp .env.example .env                                # 填入 ZHIPU_API_KEY

# 2. 单任务真机自修（goal 自动从任务包 README 读）
python scripts/drive_task.py t2_missing_fn --model glm-4.5-flash   # 免费档，6/6
# 付费更强档（可选）：python scripts/drive_task.py t2_missing_fn --model glm-4-air-250414

# 3. 回归表（数字①，含自愈重试）
python scripts/run_all.py --model glm-4.5-flash --repeat 2

# 4. 韧性实测（M3/M4/M5）
python scripts/run_resume_test.py --task t1_single_fix --kill-points 2,3,4   # 数字② 杀进程恢复率
python scripts/measure_compress.py --task t2_missing_fn                      # 数字③ 压缩率（开关对比）
python scripts/measure_plan.py --task t2_missing_fn                          # 数字④ planner A/B（plan vs no-plan）
# 开 planner 跑任务/回归（默认关）：drive_task.py t2_missing_fn --plan / run_all.py --plan

# 5. M5 能力（默认关，演示按需开；全部经同一注册表/审计链）
python scripts/drive_task.py t1_single_fix --skills   # Skills：SKILL.md 匹配注入
python scripts/drive_task.py t1_single_fix --mcp      # MCP：连 demo server 动态注册工具
python scripts/drive_task.py t1_single_fix --memory   # 长期记忆：复用历史经验 + 沉淀

# 6. 单元测试
python -m pytest tests/ -q

# 7. T4 真实开源库任务（tabulate 反向 bug，600 行回归测试）
python scripts/drive_task.py t4_github_pipe_escape --model glm-4.5-flash

# 8. Docker 容器（W12：免本地 venv，起 API 或跑一次性任务）
#    准备：仓库根建 .env 写 ZHIPU_API_KEY（docker compose 只读根目录 .env，不是 backend/.env）
echo "ZHIPU_API_KEY=sk-xxx" > .env
docker compose up -d                                  # FastAPI → http://localhost:8000/health
docker compose run --rm backend drive t1_single_fix   # CLI 真机任务（一次性）

# 9. Web 控制台（W13：浏览器看 agent 实时修 bug）
python -m uvicorn app.main:app --port 8000            # 起 API（本地 venv 方式）
#   → 浏览器开 http://localhost:8000/console
#   Docker 方式则 docker compose up -d 后同 URL；页面=纯静态单页，零前端依赖
```

## 已知边界（踩坑记录，面试可讲）

- **免费模型也能 100%**：早期免费 `glm-4-flash`（最老一代）在多步上下文尾部输出含代码 JSON 时稳定性崩塌（连败 6 次：JSON 裸参数/引号包裹 content/丢字段）。根因不是"模型不会修代码"，而是"把长代码塞进 JSON 字符串"这个动作。把代码移出 JSON（edit_file 只传 old→new 片段）+ 结构化失败反馈 + 强制验证 + 重复动作拦截 + 换免费新代 `glm-4.5-flash` → 拉到 6/6。
- **模型差异仍是真实变量**：同套 harness，付费 `glm-4-air` 一次全绿、免费 `glm-4.5-flash` 靠护栏 + 重试拉满——**架构正确性靠 A/B 验证**，不靠单一模型"能跑"。
- **免费最强档 `glm-4.7-flash`（30B）服务端过载不可用**：高峰期频繁 429/1305（访问量过大），agent 循环每步都调用喂不饱，放弃做默认；`glm-4.5-flash` 免费且稳健。
- 任务包设计影响 AI 行为：包装函数会诱使整文件覆盖误删代码 → 待补函数用单函数文件（已改 T2）。
- **M5 Skills 示例内容与 system 规则重叠（诚实口径）**：内置示例 `pytest-green` 的指引（edit_file 优先/写完必跑测试/不改测试）在 system prompt 已有——所以"加了 skill 行为应没差"。这不是机制缺陷：**机制（SKILL.md 注册/发现/按 goal 匹配/注入）才是要讲的点，内容只是教学示例**。像 Anthropic Skills 那样真正改变行为的是"领域专有方法"（某类 bug 的排查套路），属后续可扩展方向。
- **M5 MCP demo 工具无任务相关性（诚实口径）**：`sqlite_query` 查的是 harness 自己的运行库——对一个修代码的 agent 没有任务价值，**只演示 capability**（协议自研/动态注册/权限对齐）。不包装成"agent 通过 MCP 获取任务关键数据"。
- **M5 记忆无 A/B 硬数字（诚实口径）**：planner 有 measure_plan A/B，记忆没有"有/无记忆第二次跑的 token/步差"对比——M5 定位是关键词补强不是新硬数字。价值主张是**经验防重踩**（机制可指 distill/render 代码 + 同 kind 去重/上限），**不报省多少**；若要硬数字需另立评测。

## 里程碑进度

| 周 | 里程碑 | 状态 |
|---|---|---|
| W1 | M0：骨架/沙箱/loop/协议/trace/checkpoint | ✅ Day1~4 |
| W2 | M1：T1~T3 + pytest 解析 + 端到端闭环 | ✅ Day5~6（数字① 6/6） |
| W3 | M2：漂移识别 + 预算护栏 + 审批流 | ✅（底线 3/4 证据到手，MVP 五条全证） |
| W4~5 | M3：压缩开关（数字③）+ 杀 N 次测恢复率（数字②） | ✅ 2026-09-04（数字② 3/3、数字③ 12.3% 压缩且绿，commit 1e144d2） |
| W5+ | M3 余项：多模型评估 + 长任务压缩率上界复测 | ✅ 2026-09-04~05（Day5 A/B + 24 步 55.5% 上界，commit 6eba0ea） |
| W6~8 | M4：planner 补欠账 + API 一致性 + 打磨（详见 [执行计划_M4.md](docs/执行计划_M4.md)） | ✅ 2026-09-06 收官（B1~B4 + B6a 背靠背 9b151cd/d323d65 + B6b 归档 87cc0f1 + B6c 文档收官；O1-O6 精益优化 78ddfcd，单测 52/52；B5 最简 UI 未做——Q10 余力项，保持待拍板） |
| W9 | M5：JD 关键词补强——Skills 注册 + MCP（自研 client+demo server）+ 长期记忆（详见 [执行计划_M5.md](docs/执行计划_M5.md)） | ✅ 2026-09-06（Skills/MCP/记忆 29 新测，单测 52→85；真机三开关全开 T1 全绿 run `b5c4a1e61208`，见 [留档报告](docs/m5_smoke_report_2026-09-06.md)；测试 DB 隔离 + 两处 docstring 对齐 85/85；三模块默认关不碰 6/6 基线） |
| W10 | T4：真实开源库任务（tabulate 反向 bug）真机验证 + GitHub 发布 + CI | ✅ 2026-09-07（13 失败→13 harness 缺陷全修 + 单测 85→110；收官 3 局成功：resume 首胜/全自动 35 步/单段 20 步·11 分钟，见 [T4 战报](docs/t4_real_library_report_2026-09-07.md)；公开仓 VVR823/codecraft-harness，Actions CI ubuntu+py3.13 全绿） |
| W11 | GitHub 交付模式（对标 MyCoder GitHub mode）：git_branch/commit/push + gh_create_pr 四工具，自修到全绿 → 自修到 PR | ✅ 2026-09-08（`drive_task --github --workspace <clone>`；MED 权限 + HARNESS_GITHUB 环境门闩，基线 run 零影响；真机实证：修复 5/5 绿 → branch fix_csv → commit → push → **真实 PR** [VVR823/codecraft-delivery-demo#1](https://github.com/VVR823/codecraft-delivery-demo/pull/1)（+39/-1）；撞出并修复 Windows git unborn 竞态——三层防线：_head_sha 校验 / update-ref 自修复（含 .lock 清理）/ 失败喂回模型；单测 110→131） |
| W12 | Docker 容器化（发布面补 Docker）：Dockerfile + compose + 双模式 entrypoint | ✅ 2026-09-09（镜像含 .git → API 的 git restore 考卷还原语义容器内完整；data/ 命名卷持久化 run 会话可 resume；密钥零进镜像；CI docker job 背书：build + /health 冒烟 + drive 入口链全绿——本地无 docker 也可靠 CI 验证；单测 131 不动） |
| W13 | 最简 Web 控制台（M4 B5 补欠账，发布面收官）：纯静态单页消费现有 API | ✅ 2026-09-09（backend/ui/index.html 零前端依赖——原生 fetch 轮询 /api/*；任务包下拉+新建 run+run 历史侧栏+详情（状态徽章/步数/token/最近 12 步动作时间线）+ budget_paused 人工 approve 续跑；后端补 GET /api/packs、GET /api/runs、详情加 recent_traces；db.list_runs 修同秒排序不稳；单测 131→138；uvicorn 冒烟 /console 200 12KB。起法：`uvicorn app.main:app` 后开 http://localhost:8000/console） |
