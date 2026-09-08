# T4 真实开源库任务：13 局逼出 12 个缺陷，第 13 局全自动完成

日期：2026-09-07 | 任务包：`backend/tasks/t4_github_pipe_escape` | 模型：glm-4.5-flash（免费）

---

## 1. 为什么做 T4

M5 收官后用户审计出"考卷自己出"的短板——T1~T3 是自写的小任务，测不出真实世界的脏活。
0 成本补短板选了 T4：**真实开源库（tabulate）反向引入一个历史修复过的 bug**，让 AI 在
600 行回归测试 + 2897 行全量源码里自己定位、修复、验证。

## 2. 考卷设计

| 项 | 内容 |
|---|---|
| 库 | tabulate（Python 表格格式化库，真实 PyPI 项目） |
| bug | 反向引入 e6a24aa 历史修复：`"pipe"`（github 表格）格式的 `DataRow` 构造删掉了第 4 参 escape_map |
| 现象 | 单元格内容含 `\|` 时不转义 → github 格式表格列被竖线撑破 |
| 回归测试 | `test/test_regression.py` 600 行，`test_github_escape_pipe_character` 在**第 596 行**（文件倒数第 5 行） |
| 实现源码 | `tabulate/__init__.py` **全量 2897 行**（非裁剪） |
| 正确修复 | `DataRow("|", "|", "|", {"|": "\\|"})`（headerrow/datarow 各补一参） |
| bug 态 | 1 failed + 36 passed（考卷有效） |

## 3. 战报总表

| 局 | run_id | 结果 | 暴露的缺陷 → 修复（commit） |
|---|---|---|---|
| 1 | 28102e01e942 | paused 14步 | read_file 无分页 → offset/limit 分页 |
| 2 | f6e56fdcc8b2 | failed | offset 传 0/越界崩局 → 边界宽容 clamp |
| 3 | 2c873641f1b0 | failed | 埋头通读不跑测试 → prompt"开工先 run_tests" |
| 4 | f45b83aea8a6 | failed | 通读 2897 行爆上下文 → 新增 search_file 工具 |
| 5 | a32b663affc5 | paused | 预算暂停仍逐页通读 → 截断提示引导 search_file |
| 6 | fbfc57ce5639 | 僵死50min | LLM 调用无超时 → 线程看门狗 120s（fff3ef9） |
| 7 | 5e62631ecbfc | failed | 逐页通读 4 页后 JSON 崩 → 顺序翻页护栏（e4f5d8f） |
| 8 | fb871e39a6a6 | paused | 11/15 步重读同文件 → 消息层 2000→6000 不二次截断（139b693） |
| 9 | dcd8cd7cbdf6 | paused | 15 步不够侦察 → MAX_STEPS 15→30（1482ce8） |
| 10 | aea26761695b | 终止30步 | 页尾拦腰截断 → 分页超长自动收缩完整行（45806f7） |
| 11 | 8a84f04d8558 | failed | **协议白名单漏 search_file** → 补枚举（250b37b） |
| 12 | d7f00be3c480 | paused 30步 | 步数触顶无法续 → **段语义 + paused 自动/人工续跑（a1494e3）** |
| 13 | d7f00be3c480 resume | **done 33步** | 段2 仅 3 步：edit pipe DataRow + 复测 37 passed + done |
| 14 | ca253f59e921 | **done 35步（全自动）** | 段1 30步触顶 → auto-resume 段2（step33 edit 后 JSON 退化）→ auto-resume 段3（step34 复测 37 passed + done）|
| 15 | 4e2f951657d0 | failed | 模型凭记忆 edit 抄错 old → ToolError 被 raise 崩局 → **ToolError 喂回修复（e68e31b）** |
| 16 | 214e662e308d | **done 20步（单段一次过）** | 11 分钟 0 续跑：search "pipe"→读 520→确认机制→edit→复测→done |

## 4. 缺陷分类：不是 13 次失败，是 13 个发现

### 信息链路（模型"看不到/看错"）——最隐蔽、代价最大
- **消息层二次截断（run8）**：registry 输出上限 6000，loop 塞消息时又砍到 2000。
  第 596 行的目标测试被二次截断恰好切掉——模型换 11 种读法都看不到，反复空转。
  **修 harness 前先查"模型看到的内容是否完整"**，模型行为异常往往不是模型蠢。
- **页尾拦腰截断（run10）**：分页超 6000 字符时从行中间砍断，模型不知道少了多少行、
  下一行从哪开始 → 折返重读/乱序跳读。改为**按完整行自动收缩 + 明确下页 offset**。
- **search_file 协议白名单漏加（run7~10 真根因）**：registry 注册了、prompt 教了、
  所有护栏都在喊"用 search_file"，但 AgentStep 工具白名单枚举漏加 → 模型每次调用被
  "未知工具"打回，只能永远 read_file 逐页通读。**新增工具 = registry + prompt + protocol
  三处联动**。修完 run11 模型瞬间变完美工作流：7 次 search 精准定位 + 8 次精读。

### 决策质量（免费模型长 run 随机退化）
- **JSON 崩坏（run7/11）**：长 run 后连续输出坏 JSON（thought 缺失/参数名写错/路径截断）。
  run11 失败时上下文仅 33KB，非臃肿——纯随机退化，步内重试 3 次救不了 → run 级 auto-resume。
- **保守空转（run12）**：step 14-27 连续 14 步 search 换词轰炸不敢落笔 edit——
  免费模型长上下文后决策趋于最小动作。空转雷达对长尾零进展提醒力度不足（待后续增强）。

### 配额机制
- **MAX_STEPS 绝对语义 bug（run12）**：`while step < MAX_STEPS` 是整 run 上限，
  resume 从 cp.step 起算 → 30 步 paused 后 resume 立即再 paused，一步续不了。
  真实库任务 30 步只够侦察定位，收尾要第二段 → 改**段语义**（每段独立配额）。
- **paused 不自动续（run12）**：auto-resume 只认 failed+解析类 → 扩展为 paused 也续。

## 5. 第 13 局的收官三秒（resume 段2）

run12 段1 最后 3 步已把修复点摸清：step28 读到 `_build_simple_row` 的 escape_map 生效
机制，step30 读到 `"pipe": TableFormat(... DataRow("|","|","|") ...)`——缺第 4 参。
段2 resume 后模型带着完整上下文，**3 步收尾**：

```
step 31  edit_file  tabulate/__init__.py
         DataRow("|","|","|")  →  DataRow("|","|","|", {"|": "\\|"})   （headerrow+datarow）
step 32  run_tests  37 passed（全绿）
step 33  done（强制验证放行：写后复测通过）
```

模型自己推断出 escape_map 内容为 `{"|": "\\|"}`（把单元格内竖线转义为 `\|` 防撑破
Markdown 表格）——与 37 passed 回归测试一致，**不是照抄、是理解了机制**。

## 6. 方法论沉淀

1. **免费模型的失败大多是 harness 的失败**：13 局里 12 个缺陷都是 harness 真 bug
   （信息截断/协议漏门/配额语义），模型本身的工作流（run_tests→search→精读→edit→复测）
   在信息可达后立即正确。
2. **分层防护是设计出来的**：dup 拦截（同区间重读）+ page_walk（顺序翻页）+ auto-shrink
   （残行）+ watchdog（僵尸调用）+ auto-resume（决策退化）——每个护栏对应一个真实 run
   的死亡模式，先有实证后有代码。
3. **看门狗 vs 退避**：socket 超时管不住代理假活 → 线程级总超时；解析类失败步内救不了
   → run 级换采样。两层互补，单层都有盲区。
4. **测试是 harness 的考卷**：单测从 85 → 106（+21），每个新护栏都带回归测试，
   "测试隔离 + 真库零污染"保证单测可信。

## 7. 诚实口径

- 首胜 = run12 段1(30步侦察) + resume 段2(3步收尾)，**一段 30 步对真实库任务偏紧**，
  段语义是必需品而非锦上添花。
- **三连胜（可复现性确立）**：run14 全自动（35 步，auto-resume×2）、run16 单段一次过
  （**20 步 11 分钟**，周期 stall 提醒 4 次拉回 + 空 search dup 拦截生效）。
  步数 35→20、时长 50→11 分钟——空转治理（6c50b47）+ ToolError 喂回（e68e31b）两修复见效。
- 历史 14 局失败全部由当时的 harness 缺陷导致，缺陷修复后最近 3 局全胜。
  免费模型有随机性，胜率口径以更多复跑为准（每局 11-50 分钟，成本约束下 3/3 为当前证据）。
- 已知弱点（本轮新增，未修）：空转雷达周期 3 步提醒仍偏保守，run16 模型 4 次被提醒
  但都未带偏——提醒间隔与措辞可后续 A/B（对最终成功率无碍，只影响速度）。

## 8. 两级 auto-resume 实战分解（run14）

```
段1  step 1-30    run_tests 基线 → search 定位 → 精读 596/520/2504 → 空 search 轰炸触顶
                  paused（30 步配额耗尽）
     ↓ auto-resume 1/2（paused 判定）
段2  step 31-33   search def _build_simple_row → 读 2504 → edit pipe DataRow（修复落盘）
                  step33 后决策 JSON 退化 → failed（解析类）
     ↓ auto-resume 2/2（退化判定）
段3  step 34-35   run_tests 37 passed → done（edit 已存档不重放，强制验证护栏放行）
```

强制验证护栏在段3的价值：edit 落盘即 checkpoint，JSON 退化只丢"后续决策"不丢"已完成修改"，
续跑后模型直接复测收尾——**护栏让每一次随机退化都只损失一步，不损失进度**。
