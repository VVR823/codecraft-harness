FROM python:3.13-slim
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home codecraft \
    && mkdir -p /data /app/backend \
    && chown -R codecraft:codecraft /data /app
WORKDIR /app/backend
COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir --require-hashes -r requirements.txt
COPY --chown=codecraft:codecraft backend/app/ ./app/
COPY --chown=codecraft:codecraft backend/tasks/ ./tasks/
COPY --chown=codecraft:codecraft backend/ui/ ./ui/
COPY --chown=codecraft:codecraft backend/skills/ ./skills/
COPY --chown=codecraft:codecraft backend/scripts/drive_task.py backend/scripts/smoke.py ./scripts/
COPY docker-entrypoint.sh /usr/local/bin/codecraft-entrypoint
RUN chmod +x /usr/local/bin/codecraft-entrypoint
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    CODECRAFT_DATA_DIR=/data CODECRAFT_MODE=offline_demo PORT=8000
USER codecraft
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/ready', timeout=3)"
ENTRYPOINT ["/usr/local/bin/codecraft-entrypoint"]
