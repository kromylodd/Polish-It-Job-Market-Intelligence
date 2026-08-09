# Deploying the Telegram bot

The bot uses **long-polling** (no inbound ports), so any always-on Linux box
works. It keeps a small amount of local state under `telegram_bot/`:

| File | Purpose | Sensitive? |
| --- | --- | --- |
| `user_config.json` | per-user filters (mirrored to the Databricks Volume) | low |
| `analytics.db` | anonymous usage stats (hashed chat IDs) | low |
| `tracker.db` | per-user application tracker | user data |
| `payments.db` | subscriptions + Stars payment log | billing |
| `serving.duckdb` | cache of gold marts for instant premium queries | disposable |

All of these are gitignored. Back up `tracker.db` and `payments.db` — they hold
real user/billing state. `serving.duckdb` is a rebuildable cache.

## Required environment variables

Put these in `.env` (already gitignored):

```
TELEGRAM_BOT_TOKEN=...          # from BotFather
TELEGRAM_CHAT_ID=...            # your admin chat id (enables /stats, /analytics, /refresh)
ANALYTICS_SALT=...              # random 32-byte hex, for hashing chat IDs
DATABRICKS_HOST=...             # workspace host (scheme optional; the bot adds https://)
DATABRICKS_TOKEN=...            # PAT
DATABRICKS_WAREHOUSE_ID=...     # SQL warehouse id — needed for the serving-cache sync
# Optional:
SERVING_SYNC_INTERVAL_SECONDS=21600   # background mart refresh cadence (default 6h)
USER_CONFIG_VOLUME_PATH=...           # override the Volume mirror path
```

Premium analytics (`/salary <tech>`, `/trend`, `/skills`, `/company`, `/report`)
read from the local **serving cache** (`serving.duckdb`), which the bot refreshes
from Databricks on startup (if stale) and every `SERVING_SYNC_INTERVAL_SECONDS`.
You can force a refresh anytime with the admin `/refresh` command. If Databricks
env vars are missing the bot still runs — premium analytics just report
"data not ready" until a sync succeeds.

## Telegram Stars payments

Subscriptions are paid with **Telegram Stars** (`currency=XTR`, no payment
provider token). Nothing extra to configure in code, but Stars must be enabled
for your bot (they are by default for bots created via BotFather). Test the flow
with `/subscribe` → pick a tier → complete the in-app Stars checkout.

## Option A — systemd on a VPS (recommended: Oracle Cloud Always Free)

Oracle Cloud's Always Free Ampere ARM VM runs 24/7 at no cost and is the top
pick. Google Cloud `e2-micro` free tier also works.

```bash
# on the VM
sudo git clone <repo> /opt/polish-it-job-market-intelligence
cd /opt/polish-it-job-market-intelligence
python3 -m venv .venv
.venv/bin/pip install -r telegram_bot/requirements.txt

sudo useradd --system --create-home botuser
sudo chown -R botuser:botuser /opt/polish-it-job-market-intelligence
# create /opt/.../.env with the vars above (chmod 600)

sudo cp deploy/telegram-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now telegram-bot
journalctl -u telegram-bot -f
```

## Option B — Docker

```bash
docker build -f deploy/Dockerfile -t polish-it-jobs-bot .
docker run -d --name jobs-bot \
  --restart unless-stopped \
  --env-file .env \
  -v jobs-bot-data:/app/telegram_bot \
  polish-it-jobs-bot
```

The named volume `jobs-bot-data` persists the SQLite stores and the DuckDB cache
across restarts/redeploys.

> ⚠️ On free tiers that sleep idle containers (some Render/Fly/Railway plans),
> long-polling breaks when the process is paused. Prefer an always-on VM.

## Local run (development)

```bash
cd ~/polish-it-job-market-intelligence
set -a && source .env && set +a
python3 -m telegram_bot.bot
```
