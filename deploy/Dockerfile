# Telegram bot image — long-polling, no inbound ports needed.
#
# Build from the repo root so the whole telegram_bot package is in context:
#   docker build -f deploy/Dockerfile -t polish-it-jobs-bot .
#   docker run --env-file .env -v botdata:/app/telegram_bot polish-it-jobs-bot
#
# The volume persists the per-user config, analytics, tracker, subscriptions and
# the DuckDB serving cache across restarts.

FROM python:3.12-slim

# matplotlib needs a couple of runtime libs even with the Agg backend.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libfreetype6 libpng16-16 \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MPLBACKEND=Agg

WORKDIR /app

# Install deps first for better layer caching.
COPY telegram_bot/requirements.txt /app/telegram_bot/requirements.txt
RUN pip install --no-cache-dir -r /app/telegram_bot/requirements.txt

# Copy the packages the bot imports at runtime.
COPY telegram_bot/ /app/telegram_bot/

# Run as a non-root user.
RUN useradd --create-home --uid 10001 botuser \
    && chown -R botuser:botuser /app
USER botuser

# Long-polling bot: no EXPOSE needed.
CMD ["python", "-m", "telegram_bot.bot"]
