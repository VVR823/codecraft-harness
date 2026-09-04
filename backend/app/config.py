"""全局配置（v2.2 Q 系列决策固化）。"""
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent        # backend/
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True, parents=True)

DB_PATH = DATA_DIR / "harness.db"

# 沙箱默认（Q8）
SANDBOX_TIMEOUT = 120.0        # 秒；超时进程树强杀
SANDBOX_COPY_TASK = True       # 复制任务包进临时目录执行（隔离，不改源）

# LLM（Q4/Q5：智谱 GLM-4-Flash 免费起步 + 薄封装；M1 才真正接线）
# 2026-09-04 升级：glm-4-flash-250414 是新版免费 flash，代码/指令遵循优于旧 glm-4-flash
LLM_BASE_URL = "https://open.bigmodel.cn/api/paas/v4/"
LLM_MODEL = "glm-4-flash-250414"

# 成本护栏（Q 系列：Day1 落字段，M2 激活审批流）
DEFAULT_TOKEN_BUDGET = 60_000  # 单 run 预算；后续按回归实测中位数 x1.5 校准

# agent loop（Q11）
MAX_STEPS = 15                 # 单 run 最大步数（防死循环/预算失控）
LLM_RETRY = 2                  # JSON 解析失败最多重试次数
