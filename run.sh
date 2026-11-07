#!/usr/bin/env sh
# Build and (re)start the dashboard as a plain container — no docker compose.
#   ./run.sh
#   PORT=9000 PUID=1000 PGID=1000 ./run.sh
#   VISION_BASE_URL=https://ollama.com/v1 VISION_MODEL=kimi-k3 VISION_API_KEY=… ./run.sh
# Unset VISION_* variables keep the image defaults (a local Ollama on 127.0.0.1).
set -eu

IMAGE=nespresso-stats
NAME=nespresso-stats
PORT="${PORT:-8787}"

docker build -t "$IMAGE" .
docker rm -f "$NAME" >/dev/null 2>&1 || true
docker run -d --name "$NAME" \
    -p "$PORT:8787" \
    -e PUID="${PUID:-99}" \
    -e PGID="${PGID:-100}" \
    ${VISION_BASE_URL+-e VISION_BASE_URL="$VISION_BASE_URL"} \
    ${VISION_MODEL+-e VISION_MODEL="$VISION_MODEL"} \
    ${VISION_API_KEY+-e VISION_API_KEY="$VISION_API_KEY"} \
    -v "$PWD/data:/data" \
    --restart unless-stopped \
    "$IMAGE"

echo "nespresso-stats → http://localhost:$PORT"
