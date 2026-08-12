# Agent Memory — Polish IT Job Market Intelligence

_Last updated: 2026-08-12 by Kiro. Purpose: durable context for future sessions._

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
- **Deploy note:** pushing to `origin/main` does NOT update prod. The GCP VM must
  `git pull` + restart its service for changes (e.g. PREMIUM_FREE) to take effect.
- Pipeline: `deploy/pipeline.service` + `pipeline.timer` (scheduled scrape+build).
- Secrets in `.env` (gitignored). NOTE: bot token leaks into journald logs via
  the getUpdates URL — rotate via BotFather before sharing any logs.

## Legal / compliance posture (IMPORTANT — commercial, redistributive product)
Full audit lives in chat history 2026-08-12. Summary:
- justjoin.it robots.txt **disallows `/api/`**; ToS is a PDF (not machine-read) —
  treat as at-least-restrictive. Operator: Just Join IT sp. z o.o. (Grupa Pracuj).
  EU sui generis **database rights** likely apply. Risk for a paid product:
  **MEDIUM-HIGH**.
- Adzuna is the only *sanctioned* API (developer.adzuna.com) and covers Poland,
  BUT its ToS requires a **written licence** for ongoing commercial/aggregation
  use beyond a 14-day trial, plus "Jobs by Adzuna" attribution.
- Other PL boards: nofluffjobs (blocks /api/, CloudFront, reachable POST API),
  theprotocol.it + bulldogjob.pl (**Cloudflare bot challenge = 403 to bots**;
  bulldogjob also disallows `/salaryBrackets`). theprotocol/bulldogjob = avoid.

### Risk-reduction actions already taken (2026-08-12)
- Repo made **private**; git history verified clean (no `.env`/DBs/`raw_listings`
  ever committed; `.gitignore` correct).
- **Deleted** local `data/raw_listings_*.json` raw dumps (~53MB) to cut the
  database-rights / PII footprint. Bot serves **only aggregates**; alerts/`/latest`
  send only title/company/city/salary + a link back (referral-style), never
  full descriptions. No recruiter PII is extracted or served.
- **Monetization paused**: `PREMIUM_FREE = True` flag in `telegram_bot/bot.py`
  makes all former premium features free (`has_feature`/`is_paid_user` -> True).
  `/premium` menu relabeled "Analytics & tools", paywall framing removed,
  `/subscribe` hidden + announces free (no Stars invoices). Payment code intact;
  flip `PREMIUM_FREE=False` to re-enable paid tiers.

### Open legal TODOs / options
- Decide direction (see chat 2026-08-12): keep free/personal, OR license Adzuna
  for a compliant paid product, OR seek permission from Just Join IT / Grupa Pracuj.
- If ever monetizing again: consult a PL/EU IP+data lawyer (database rights + GDPR).
- Keep scraping polite: low rate, honest User-Agent, honor 429/Retry-After
  (already implemented in scraper.py). Stop immediately on any cease-and-desist.

## Code map (key files)
- `scraper/scraper.py`, `scraper/parser.py` — fetch + normalize listings.
- `pipeline/` (bronze_ingest, silver_clean, silver_tech_parse, run_pipeline) + `dbt/` — build gold marts.
- `telegram_bot/bot.py` — handlers, command menu, PREMIUM_FREE gate (~line 1052).
- `telegram_bot/serving.py` — read-only aggregate queries from DuckDB gold marts.
- `telegram_bot/payments.py` — Telegram Stars subscription logic (currently bypassed).
- `telegram_bot/notify.py` — daily alert delivery (title/company/city/salary + link).
- Local runtime DBs (gitignored): payments.db, tracker.db, analytics.db,
  serving.duckdb, user_config.json. These hold the bot's own users' Telegram IDs
  / payment records = legitimate personal data; keep, don't commit.

## Verification status (2026-08-12)
- `python3 -m pytest telegram_bot/tests` -> **84 passed**.
- `bot.py` compiles; service confirmed **active (running)** after restart.
