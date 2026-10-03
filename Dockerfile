# syntax=docker/dockerfile:1

# --- stage 1: сборка Mini App (Next.js static export) ---
# Node нужен ТОЛЬКО на этой стадии: в runtime остаётся Python и готовая статика
# webapp/out. Сам образ node в финальный образ не попадает.
FROM node:20-slim AS frontend

WORKDIR /build

# pnpm через corepack — версия зафиксирована и совпадает с packageManager
# в webapp/package.json (pnpm@12.3.4).
RUN corepack enable && corepack prepare pnpm@12.3.4 --activate

# Сначала манифесты: слой с зависимостями не сбрасывается при правках кода.
#
# pnpm-workspace.yaml обязателен: без него corepack-pnpm считает lockfile
# устаревшим и падает с ERR_PNPM_FROZEN_LOCKFILE_WITH_OUTDATED_LOCKFILE
# (проверено локально на pnpm@12.3.4). Файл лежит рядом с манифестами и
# переносится вместе с ними.
COPY webapp/package.json webapp/pnpm-lock.yaml webapp/pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile

# Потом код фронта целиком (app/, components/, lib/, public/, конфиги).
COPY webapp/ ./

# Собираем статику: next.config.mjs задаёт output: 'export' и basePath '/app',
# поэтому результат — файлы, которым Node.js в runtime не нужен.
RUN pnpm build

# --- stage 2: builder — виртуальное окружение Python отдельным слоем ---
FROM python:3.11-slim AS builder

WORKDIR /app

COPY requirements.txt ./
RUN python -m venv /opt/venv && \
    /opt/venv/bin/pip install --no-cache-dir --upgrade pip && \
    /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

# --- stage 3: runtime — только venv, код и собранная статика ---
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    TZ=Asia/Krasnoyarsk

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv

COPY bot/ ./bot/
COPY run.py ./

# Пакеты, которые нужны runtime-коду, но лежат вне bot/.
#
# bot_vk/ — VK-бот (Long Poll): bot/main.py импортирует его в build_vk_bot(),
# поэтому без этой строки контейнер падал на старте с
# ModuleNotFoundError: No module named 'bot_vk'.
# core/ — общие модули для обоих ботов (парсеры, БД, форматирование); пока
# заготовка, но копируем сразу: иначе тот же баг повторится при переносе кода.
COPY bot_vk/ ./bot_vk/
COPY core/ ./core/

# Собранный фронт из stage 1. Путь совпадает с WEBAPP_DIR в bot/web.py
# (корень проекта / webapp / out): по нему create_app монтирует /app/.
COPY --from=frontend /build/out ./webapp/out

# /app/data — точка монтирования персистентного тома (БД, кэш, бэкапы).
#
# Работаем от root: на Railway контейнер изолирован, а Volume при
# монтировании поверх /app/data приходит с владельцем root:root — под
# uid 1000 писать в него нельзя, и SQLite падал бы с
# «unable to open database file».
RUN mkdir -p /app/data && chmod -R 777 /app/data

# USER app — убран намеренно: Volume в Railway принадлежит root,
# а смена владельца тома из контейнера не работает.
# Вернуть изоляцию: добавить обратно useradd + USER app и
# убедиться, что хостинг отдаёт том пользователю uid 1000.

# EXPOSE — только документация порта; сервер слушает значение из env PORT.
EXPOSE 8080

# HEALTHCHECK сам берёт порт из env PORT (по умолчанию 8080), поэтому
# менять здесь ничего не нужно даже при смене PORT.
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import os, urllib.request; port = os.environ.get('PORT', '8080'); urllib.request.urlopen(f'http://127.0.0.1:{port}/health', timeout=4)"

CMD ["python", "-m", "bot.main"]