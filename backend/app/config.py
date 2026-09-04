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
DEFAULT_TOKEN_BUDGET = 60_000  # 单 run 预算；后续按回归实测中位数 x1.5 校准

# agent loop（Q11）
MAX_STEPS = 15                 # 单 run 最大步数（防死循环/预算失控）
LLM_RETRY = 3                  # JSON 解析失败最多重试次数（免费模型 JSON 偶发崩，2026-09-04 从 2 提 3）
