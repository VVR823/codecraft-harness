#!/bin/sh
# CodeCraft Harness 容器入口（双模式）
#   docker run codecraft-harness                 → 起 FastAPI（:8000）
#   docker run codecraft-harness drive t1_single_fix [--plan|--skills|...]
#                                                → CLI 真机任务（一次性）
set -e
cd /app/backend

if [ "$1" = "drive" ]; then
    shift
    exec python scripts/drive_task.py "$@"
fi

exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
