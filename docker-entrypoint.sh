#!/bin/sh
set -eu
cd /app/backend
case "${1:-serve}" in
    drive) shift; exec python scripts/drive_task.py "$@" ;;
    migrate) shift; exec python -m app.store.migrations "$@" ;;
    smoke) shift; exec python scripts/smoke.py "$@" ;;
    serve) exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}" --workers 1 --no-access-log ;;
    *) exec "$@" ;;
esac
