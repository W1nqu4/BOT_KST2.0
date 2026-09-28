# syntax=docker/dockerfile:1

# --- builder: собираем виртуальное окружение отдельным слоем ---
FROM python:3.11-slim AS builder

WORKDIR /app

COPY requirements.txt .
RUN python -m venv /opt/venv && \
    /opt/venv/bin/pip install --no-cache-dir --upgrade pip && \
    /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

# --- runtime: только venv и код, без компиляторов ---
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    TZ=Asia/Krasnoyarsk

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv

COPY bot/ ./bot/
COPY run.py ./

# Непривилегированный пользователь: контейнер не должен работать от root.
# /app/data — точка монтирования персистентного тома (БД, кэш, бэкапы).
RUN useradd -r -u 1000 app && \
    mkdir -p /app/data && \
    chown -R app /app

USER app

# EXPOSE — только документация порта; сервер слушает значение из env PORT.
EXPOSE 8080

# HEALTHCHECK сам берёт порт из env PORT (по умолчанию 8080), поэтому
# менять здесь ничего не нужно даже при смене PORT.
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import os, urllib.request; port = os.environ.get('PORT', '8080'); urllib.request.urlopen(f'http://127.0.0.1:{port}/health', timeout=4)"

CMD ["python", "-m", "bot.main"]