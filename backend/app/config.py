"""底层模型客户端与执行循环使用的默认配置。"""

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent  # backend/
DATA_DIR = BASE_DIR / "data"

DB_PATH = DATA_DIR / "harness.db"

# 沙箱默认值
SANDBOX_TIMEOUT = 120.0  # 秒；超时进程树强杀
SANDBOX_COPY_TASK = True  # 复制任务包进临时目录执行（隔离，不改源）

# OpenAI 兼容客户端的旧版默认值；实际运行是否允许远端端点由 Settings 校验。
LLM_BASE_URL = "https://open.bigmodel.cn/api/paas/v4/"
LLM_MODEL = "glm-4.5-flash"

# 单次运行达到预算后暂停；只有显式提高有限总预算才能继续。
DEFAULT_TOKEN_BUDGET = 30_000  # 单 run 预算

# 步数提供最终兜底；预算限制累计用量，空转检测记录或提示无效探索。
# 较大的仓库通常需要多轮定位、编辑和复测，因此上限需容纳完整修复链路。
MAX_STEPS = 30  # 单 run 最大步数（防死循环/预算失控）
LLM_RETRY = 3  # JSON 决策解析失败的最大重试次数
