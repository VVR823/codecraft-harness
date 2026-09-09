# CodeCraft Harness 容器化（W12：发布面补 Docker，2026-09-09）
#
# 设计要点（每个都是工程决策，不是"把代码塞进镜像"）：
# 1. 镜像内保留 .git（约 1.3MB）——API 的 _restore_task 靠 `git restore` 把任务包
#    还原成"带 bug 考卷"态，没有 .git 这个语义就残缺（restore 静默失败、run 从脏态起跑）。
# 2. apt 装 git——同上，镜像内必须真有 git 可执行。
# 3. entrypoint 双模式：默认 = FastAPI 服务（:8000）；`drive <task>` = CLI 真机任务
#    runner。Docker 既当服务端、也当一次性任务执行器，compose 里一条命令切换。
# 4. backend/data（harness.db + 真机产物）不进镜像，compose 用 named volume 挂载
#    → run 会话/checkpoint 跨容器持久化，可 resume。密钥 ZHIPU_API_KEY 走 env 注入，
#    镜像内零密钥（backend/.env 被 .dockerignore 排除）。
#
# 本地没装 docker 也能交付：CI（GitHub Actions，runner 自带 docker）每次 push
# 都会 docker build + health 冒烟，Dockerfile 有效性由 CI 背书（证据链风格）。

FROM python:3.13-slim

# git：_restore_task / 交付工具 / 任务包还原都需要（slim 默认没有）
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 整仓拷贝（含 .git，见设计要点 1；.dockerignore 已排除 data 产物 / .env / 缓存）
COPY . /app

# 后端依赖（含 uvicorn[standard]）
RUN pip install --no-cache-dir -r /app/backend/requirements.txt

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000

WORKDIR /app/backend

# 双模式入口：默认 FastAPI 服务；`drive <task>` 跑 CLI 真机任务
COPY docker-entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]

EXPOSE 8000
