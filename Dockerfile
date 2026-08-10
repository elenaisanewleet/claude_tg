# Образ с Node (для Claude Code) и Python (для бота).
FROM node:22-bookworm-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data \
    WORKSPACE_ROOT=/data/workspaces \
    DB_PATH=/data/claude-tg.sqlite3

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 python3-pip python3-venv git ripgrep ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Claude Code — «мозг» бота. В контейнере обновляется пересборкой образа,
# поэтому AUTO_UPDATE по умолчанию выключен (см. docker-compose.yml).
RUN npm install -g @anthropic-ai/claude-code@latest

WORKDIR /app
COPY requirements.txt ./
RUN python3 -m venv /opt/venv && /opt/venv/bin/pip install -r requirements.txt
ENV PATH="/opt/venv/bin:$PATH"

COPY . .
RUN pip install --no-deps -e .

VOLUME ["/data", "/root/.claude"]

CMD ["python", "-m", "claude_tg"]
