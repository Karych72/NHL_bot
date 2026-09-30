# NHL_bot — Telegram bot and tooling (runtime: requirements.txt only).
# Build from repo root: docker build -t nhl-bot .
# Stages: base (shared) -> modeling (+ lightgbm/sklearn, service `retrain`, Task 26)
# -> bot (LAST, so a plain `docker build .` / `build: .` yields the bot image without
# the modeling stack; the modeling image is `--target modeling`).
FROM python:3.11-slim-bookworm AS base

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN python -m pip install --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

COPY . .

# Non-root user for running the bot and the sync service; packages above are
# installed as root. The bot itself never writes under /app (its only disk
# writes are tempfile.NamedTemporaryFile in telegram_bot/video_replay.py, i.e.
# /tmp); the sync service (pipeline/scheduled_sync.py) does, into /app/all_data
# (disk cache + status file), so that directory is created and chowned to
# appuser here — the rest of /app stays root-owned.
RUN groupadd --gid 1000 appuser \
    && useradd --uid 1000 --gid appuser --no-create-home --shell /usr/sbin/nologin appuser \
    && mkdir -p /app/all_data /app/artifacts \
    && chown -R appuser:appuser /app/all_data /app/artifacts
USER appuser

# Modeling image for the weekly retrain service (pipeline/scheduled_retrain.py).
# libgomp1 is the OpenMP runtime lightgbm needs on slim; /app/artifacts (chowned above)
# is where the compose bind mount lands. appuser has no home directory, so matplotlib
# (imported by modeling reports) gets an explicit writable config dir.
FROM base AS modeling
ENV MPLCONFIGDIR=/tmp/matplotlib
USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir -r requirements-modeling.txt
USER appuser

FROM base AS bot
WORKDIR /app/telegram_bot

# The bot is long-polling and listens on no port, so "is the process alive" would be
# tautological (bot.py dying kills the container anyway). Check the actual dependency
# instead: can we reach PostgreSQL with the same PG_* env vars the bot connects with.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os, psycopg2; psycopg2.connect(host=os.environ['PG_HOST'], port=os.environ['PG_PORT'], user=os.environ['PG_USER'], dbname=os.environ['PG_DATABASE']).close()"

CMD ["python", "bot.py"]
