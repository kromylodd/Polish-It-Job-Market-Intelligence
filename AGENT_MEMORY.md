# Agent Memory — Polish IT Job Market Intelligence

_Last updated: 2026-08-14 by Kiro. Purpose: durable context for future sessions._

## What this project is
- Data pipeline that collects Polish IT job listings and powers a **Telegram bot**
  (salary analytics, tech co-occurrence, market trends, daily filtered alerts).
- Current source: **justjoin.it** via its undocumented internal JSON API
  (`https://justjoin.it/api/candidate-api/offers`, cursor/offset pagination,
  ~10 items/page hard cap, 10k total cap). Scraper: `scraper/scraper.py`.
- Architecture (post-migration): scrape -> raw JSON -> pipeline (bronze/silver,
  dbt gold marts) -> **DuckDB** -> bot reads aggregated `gold.*` marts read-only.
  Migrated OFF Databricks to a plain VM + systemd.

## How it runs
- **PRODUCTION runs on a GCP VM** (systemd there). That instance owns the
  Telegram long-poll — only ONE poller may run per bot token or Telegram throws
  getUpdates conflicts.
- The copy in this local dev machine is a **duplicate**: its systemd *user*
  service `telegram-bot.service` is **stopped + disabled** (2026-08-12) so it
  never conflicts with GCP. Do NOT start it while GCP is live.
  - (dev only) Restart: `systemctl --user restart telegram-bot`
  - Status/logs: `systemctl --user status telegram-bot` / `journalctl --user -u telegram-bot`
- **Deploy note:** pushing to `origin/main` does NOT update prod. The GCP VM
  auto-syncs (`git pull + pip install`) at the start of each daily scrape run
  (added 2026-08-13). Manual restart needed only for bot.py changes.
- **After every bot push:** pull on the GCP VM and restart the bot process:
  ```bash
  cd /home/kromylodd/polish-it-job-market-intelligence
  git fetch origin <branch> && git checkout <branch> && git pull
  kill $(pgrep -f 'telegram_bot.bot') 2>/dev/null
  set -a && source .env && set +a
  nohup .venv/bin/python -m telegram_bot.bot > /tmp/bot.log 2>&1 &
  ```
  The bot runs as a bare nohup process (no systemd on the VM). Env vars come
  from `.env`. Verify with `tail /tmp/bot.log`.
- Pipeline: `deploy/pipeline.service` + `pipeline.timer` (scheduled scrape+build).
- Secrets in `.env` (gitignored). NOTE: bot token leaks into journald logs via
  the getUpdates URL — rotate via BotFather before sharing any logs.

## Repo visibility & legal posture
- **Repo is PUBLIC** as of 2026-08-13 (github.com/kromylodd/Polish-It-Job-Market-Intelligence).
- Remote URL: `git@github.com:kromylodd/Polish-It-Job-Market-Intelligence.git`
- **Framing:** "personal, non-commercial tool" / "audience of one." README does NOT
  advertise an ongoing daily production scrape. The actual cron still fires daily
  for personal use — that's fine; just don't draw attention to it in public docs.
- justjoin.it robots.txt **disallows `/api/`**; ToS restrictive; EU sui generis
  database rights likely apply. **Monetization paused** (`PREMIUM_FREE = True`).
- The bot serves ONLY aggregated analytics (medians, co-occurrence %, trends).
  Alerts send title/company/city/salary + link back (referral-style). No recruiter
  PII extracted or served. No raw data redistributed.
- Databricks PAT fully deleted (was never committed to git).

### Open legal TODOs / options
- Keep free/personal, OR license Adzuna for a compliant paid product, OR seek
  permission from Just Join IT / Grupa Pracuj.
- If ever monetizing: consult a PL/EU IP+data lawyer (database rights + GDPR).
- Keep scraping polite: low rate, honest User-Agent, honor 429/Retry-After.

## GCP VM details
- Project: `polish-it-jobs-bot`
- Instance: `telegram-bot`, zone: `us-west1-b`
- IP: ephemeral (was in git history — key-only SSH, low risk)
- SSH: `gcloud compute ssh telegram-bot --project=polish-it-jobs-bot --zone=us-west1-b`
- SSH hardened: `passwordauthentication no`, `permitrootlogin no`, key-only.
- Firewall: port 22 open 0.0.0.0/0 (needed for GitHub Actions SSH trigger).
- `gh` CLI NOT installed on local machine.

## Code map (key files)
- `scraper/scraper.py`, `scraper/parser.py` — fetch + normalize listings.
- `pipeline/` (bronze_ingest, silver_clean, silver_tech_parse, run_pipeline) + `dbt/` — build gold marts.
- `telegram_bot/bot.py` — handlers, command menu, PREMIUM_FREE gate (~line 1055).
- `telegram_bot/serving.py` — read-only aggregate queries from DuckDB gold marts.
- `telegram_bot/payments.py` — Telegram Stars subscription logic (currently bypassed).
- `telegram_bot/notify.py` — daily alert delivery (title/company/city/salary + link).
- Local runtime DBs (gitignored): payments.db, tracker.db, analytics.db,
  serving.duckdb, user_config.json.

## Local-only career planning files (gitignored, NOT in repo)
- `CV_PLAN.txt` — one-page DE CV blueprint.
- `FULL_CAREER_PLAN.txt` — complete 4-phase plan (Aug 2026 → Jan 2027).
- `LINKEDIN_POSTS_PLAN.txt` — 20-post content backlog + cadence.
- `LINKEDIN_ABOUT_AND_BANNER.txt` — headline, About sections, banner prompt.
- `LINKEDIN_POST_no_launch.txt` — capstone "done but not launching" post.
- `restrictions.txt` — ToS analysis notes.

## Owner context
- Name: Daniil Demidov, turning 20 Dec 2026.
- University: Informatics (Databases & Data Engineering), UE Katowice, starts Oct 2026.
- Goal: DE internship / working-student role from Jan/Feb 2027.
- LinkedIn: ~361 connections, profile updated (banner + headline done, About/Featured pending).
- GitHub: `kromylodd` (both repos pinned, housing = public, job-market = public).
- Second project: Polish-Housing-Market-Intelligence-Platform (BigQuery, Airflow,
  Terraform, GCS, Cloud Run, dbt, Great Expectations, Power BI, 34 cities).

## Verification status (2026-08-13)
- `pytest` -> **113 passed** (full suite).
- Pipeline: PASS (22.6s, bronze 30k rows, silver 10.4k, dbt PASS=33 WARN=1 ERROR=0).
- Bot: running 24/7 on GCP VM.
- Repo: public, no secrets in history, VM IP scrubbed from current files.
- CI: green (ruff, black, pytest, dbt parse).
