FROM python:3-alpine

RUN apk add --no-cache su-exec

WORKDIR /app
COPY . .
RUN chmod +x /app/entrypoint.sh

ENV HOST=0.0.0.0 \
    NESPRESSO_DB=/data/nespresso.db \
    VISION_BASE_URL=http://127.0.0.1:11434/v1 \
    VISION_MODEL=qwen3.5:4b \
    PUID=99 \
    PGID=100
RUN mkdir -p /data && chown 99:100 /data
VOLUME /data
EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=5s \
    CMD wget -qO /dev/null http://127.0.0.1:8787/api/state

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["python3", "server.py"]
