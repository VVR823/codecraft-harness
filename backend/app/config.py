"""全局配置（v2.2 Q 系列决策固化）。"""
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent        # backend/
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True, parents=True)

DB_PATH = DATA_DIR / "harness.db"

# 沙箱默认（Q8）
SANDBOX_TIMEOUT = 120.0        # 秒；超时进程树强杀
SANDBOX_COPY_TASK = True       # 复制任务包进临时目录执行（隔离，不改源）

# LLM（智谱 GLM 免费起步 + 薄封装；不引 LangChain）
# 2026-09-04 演进：免费三代——glm-4-flash-250414(老, T2六连败写长JSON崩) /
#   glm-4.5-flash(免费第2代, 稳健, 配护栏 6/6) / glm-4.7-flash(免费30B但服务端过载429不可用)
# 默认用稳健免费的 glm-4.5-flash（满足"免费又高成功率"硬要求）；更强可选 glm-4-air-250414(付费)
LLM_BASE_URL = "https://open.bigmodel.cn/api/paas/v4/"
LLM_MODEL = "glm-4.5-flash"

# 成本护栏（Q 系列：Day1 落字段，M2 激活审批流）
# 2026-09-04 校准：实测单次最大 18147 token（glm-4.5-flash T2）×1.5 ≈ 27k → 收紧到 30k。
# 超限 → loop 转 budget_paused → 人工批准（approvals 表）后才可 resume 续跑（底线 4）。
DEFAULT_TOKEN_BUDGET = 30_000  # 单 run 预算

# agent loop（Q11）
# MAX_STEPS：15 → 30（2026-09-07，T4 真实库任务实证）。玩具任务（T1~T3）4~6 步
# 就到 done，15 步绰绰有余；真实库任务（600 行测试 + 千行源码）光"读测试定位目标
# 测试 → 读实现相关段"就要 8~12 步侦察，15 步会在 edit+复测前触顶 paused（T4 run8/9
# 连续两局实证）。防失控的真正刹车是 token 预算（DEFAULT_TOKEN_BUDGET）+ 空转雷达
# + 护栏，步数是兜底——30 步对免费模型可接受（每步决策 token 受 watch 限）。
MAX_STEPS = 30                 # 单 run 最大步数（防死循环/预算失控）
LLM_RETRY = 3                  # JSON 解析失败最多重试次数（免费模型 JSON 偶发崩，2026-09-04 从 2 提 3）
