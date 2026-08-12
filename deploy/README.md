# Deploying the Telegram bot

The bot uses **long-polling** (no inbound ports), so any always-on Linux box
works. It keeps a small amount of local state under `telegram_bot/`:

| File | Purpose | Sensitive? |
| --- | --- | --- |
| `user_config.json` | per-user filters (local, authoritative) | low |
| `analytics.db` | anonymous usage stats (hashed chat IDs) | low |
| `tracker.db` | per-user application tracker | user data |
| `payments.db` | subscriptions + Stars payment log | billing |
| `alerts.db` | per-(listing, chat) daily-alert idempotency log | disposable |

All of these are gitignored and created with `0600` permissions. Back up
`tracker.db` and `payments.db` — they hold real user/billing state. `alerts.db`
is a rebuildable idempotency log. The gold marts live in `pipeline.duckdb` (repo
root), which the bot opens **read-only**; the pipeline is its only writer.

## Required environment variables

Put these in `.env` (already gitignored):

```
TELEGRAM_BOT_TOKEN=...          # from BotFather
TELEGRAM_CHAT_ID=...            # your admin chat id (enables /stats, /analytics, /givepremium, /refund)
ANALYTICS_SALT=...              # random 32-byte hex, for hashing chat IDs
# Optional:
PIPELINE_DB_PATH=/home/<user>/polish-it-job-market-intelligence/pipeline.duckdb  # gold marts (bot reads read-only)
```

See `.env.example` at the repo root for the full annotated template.

Premium analytics (`/salary <tech>`, `/trend`, `/skills`, `/company`, `/report`)
read directly from the pipeline's local DuckDB (`pipeline.duckdb`) — no sync
step, no network, no cold starts. If the pipeline hasn't run yet, the bot still
runs and premium commands just report "data not ready" until the first run
populates the gold marts.

## Telegram Stars payments

Subscriptions are paid with **Telegram Stars** (`currency=XTR`, no payment
provider token). Nothing extra to configure in code, but Stars must be enabled
for your bot (they are by default for bots created via BotFather). Test the flow
with `/subscribe` → pick a tier → complete the in-app Stars checkout.

## Quick deploy (one script)

Once you have SSH access to an always-on Linux VM (see Option A for a free one),
the whole deployment is a single idempotent script — `deploy/setup_vm.sh`. It
installs system packages, builds the venv, installs requirements, writes a
systemd **user** service, enables linger (so the bot survives logout **and**
reboot), and starts it.

```bash
# on the VM
git clone <your-repo-url> ~/polish-it-job-market-intelligence
cd ~/polish-it-job-market-intelligence
# from your laptop, copy your secrets up (see env vars below):
#   scp -i <key> .env <vm-user>@<vm-ip>:~/polish-it-job-market-intelligence/.env
./deploy/setup_vm.sh
journalctl --user -u telegram-bot -f      # follow logs
```

Re-run it any time after a `git pull` to redeploy. When the cloud bot is
confirmed responding, **stop the laptop copy** so two instances don't fight over
the token (`Conflict: terminated by other getUpdates`):

```bash
systemctl --user disable --now telegram-bot   # run this ON YOUR LAPTOP
```

The manual steps below (Option A / Option B) are kept for reference and for the
system-service-on-a-VPS variant.

## Option A — systemd on a free-tier VM (recommended: GCP e2-micro)

Google Cloud's `e2-micro` (2 shared vCPU, 1 GB RAM) is free-forever in
`us-west1`, `us-central1`, or `us-east1` with a 30 GB `pd-standard` disk. The
bot idles at ~60 MB RSS so this is plenty.

### Provisioning the VM (one-time)

```bash
# create a dedicated project (keeps billing/IAM separate from other GCP work)
gcloud projects create polish-it-jobs-bot --name="Polish IT Jobs Bot"
gcloud billing projects link polish-it-jobs-bot \
  --billing-account=$(gcloud billing accounts list --format='value(ACCOUNT_ID)' | head -1)
gcloud services enable compute.googleapis.com --project=polish-it-jobs-bot

# create the instance
gcloud compute instances create telegram-bot \
  --project=polish-it-jobs-bot \
  --zone=us-west1-b \
  --machine-type=e2-micro \
  --image-family=debian-12 \
  --image-project=debian-cloud \
  --boot-disk-size=30GB \
  --boot-disk-type=pd-standard \
  --tags=telegram-bot \
  --metadata=startup-script='#!/bin/bash
apt-get update && apt-get install -y python3-venv python3-pip git'

# firewall: SSH only (the bot uses long-polling, no inbound ports needed)
gcloud compute firewall-rules create allow-ssh-bot \
  --project=polish-it-jobs-bot \
  --direction=INGRESS \
  --action=ALLOW \
  --rules=tcp:22 \
  --source-ranges=0.0.0.0/0 \
  --target-tags=telegram-bot
```

### SSH into the VM

```bash
gcloud compute ssh telegram-bot --zone=us-west1-b --project=polish-it-jobs-bot
```

This auto-creates and manages SSH keys for you — no manual key setup.

### Deploying the bot

```bash
# on the VM (after SSH-ing in)
git clone https://github.com/kromylodd/Polish-It-Job-Market-Intelligence.git ~/polish-it-job-market-intelligence
cd ~/polish-it-job-market-intelligence

# copy your .env from your laptop (run this ON YOUR LAPTOP in another terminal):
#   gcloud compute scp .env telegram-bot:~/polish-it-job-market-intelligence/.env \
#     --zone=us-west1-b --project=polish-it-jobs-bot

# deploy (idempotent — re-run after git pull to redeploy)
./deploy/setup_vm.sh
journalctl --user -u telegram-bot -f
```

### Manual system-service variant (if not using setup_vm.sh)

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

### Free-tier cost breakdown

| Resource | Free allowance | Bot usage |
|---|---|---|
| e2-micro instance | 744 hrs/month (1 instance, eligible regions) | 744 hrs (always-on) |
| pd-standard disk | 30 GB | 30 GB |
| Egress | 1 GB/month (to worldwide excl. China/Australia) | ~100 MB (Telegram API calls) |
| **Total** | — | **$0/month** |

> The free tier is per-billing-account. If you already use a free e2-micro in
> another project under the same billing account, this VM will incur charges
> (~$6.11/month for e2-micro in us-west1).

### Alternative: Oracle Cloud Always Free

If you have an Oracle Cloud account, their Always Free Ampere ARM VM (4 OCPU,
24 GB RAM) is even more generous. The deployment steps are identical once you
have SSH access — clone, copy `.env`, run `setup_vm.sh`.

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

## Decisions log

### 2026-08-10: GCP over Oracle Cloud

**Context:** Couldn't create an Oracle Cloud account (verification issues).

**Decision:** GCP `e2-micro` free tier instead. Created a **separate GCP project**
(`polish-it-jobs-bot`) rather than reusing the `silesia-housing` project from the
housing platform because:

1. Clean billing visibility — bot costs isolated ($0, but verifiable).
2. No Terraform state drift — the housing project's IAM/infra is Terraform-managed;
   adding a VM there without updating `.tf` files would cause drift.
3. Portfolio separation — two projects, two GCP projects, no coupling.

The free tier applies at the billing-account level, so both projects share the
same billing account and the bot VM is still free.

**Alternatives evaluated:**
- Oracle Cloud Always Free (best specs but account creation blocked)
- Hetzner CX22 (~€3.29/month, no free tier)
- Fly.io free tier (unreliable for always-on long-polling)
- Railway/Render (sleep idle containers — breaks long-polling)

**VM details:**
- Instance: `telegram-bot`, zone `us-west1-b`, `e2-micro`
- OS: Debian 12, 30 GB `pd-standard`
- Firewall: SSH only, no inbound HTTP/HTTPS (bot uses outbound long-polling)
- Startup script pre-installs `python3-venv`, `python3-pip`, `git`
