# TODO

## Code-review hardening pass — 2026-08-12

Full production code review + fixes (test suite 100 → 113 passing, ruff + black clean).

- [x] **Payments: no lost/duplicated charges.** `drop_pending_updates=False`; new
  atomic idempotent `payments.record_and_activate(charge_id, …)` so a redelivered
  `successful_payment` can't double-stack a subscription and a restart can't drop it.
- [x] **SQLite stores are `0600`.** New `telegram_bot/dbutil.py` connection helper
  enforces owner-only perms; systemd `UMask=0077` + `Restart=always`.
- [x] **Serving fast-path fixed.** `_query_local_cache` now reads
  `gold.mart_market_snapshot` (was a dead unqualified name) via `fetchall()` (no
  pandas runtime dep).
- [x] **Alert idempotency log** moved out of `pipeline.duckdb` into its own SQLite
  `alerts.db` — the pipeline DB is now read-only from the bot (no write-lock contention).
- [x] **Analytics concurrency unified** (lock-per-call like payments/tracker);
  `/analytics reset` now preserves `/feedback` rows.
- [x] **Stable tracker pagination** (SQL `LIMIT/OFFSET` ordered by `created_at`).
- [x] **Broadcast cap derives from `payments`** (fixed paid-users-capped-at-free bug).
- [x] **Subscription-lookup cache** (30s TTL, invalidated on mutation) to keep SQLite off the event loop.
- [x] **Users can see premium end date** — `/premium` + `/subscribe` show expiry + days left.
- [x] **CI action versions fixed** (`checkout@v4`, `setup-python@v5` — v7/v6 don't exist), `.env.example` added.
- [x] **Databricks PAT removed** — no longer used by any code; credential deleted.
- [ ] **Off-box nightly backup of `payments.db`** (billing data) to object storage.
- [ ] **Split `bot.py`** (~2600 lines) into cohesive handler modules — deferred (needs handler tests first).
- [ ] **Observability:** systemd watchdog / liveness ping so a silently-dead bot is noticed.

---

- [x] **Salary period normalization (data-quality fix).** ✅ Done. justjoin.it
  quotes permanent (UoP) pay per month but B2B / mandate (umowa zlecenie) rates
  per **hour** (rarely per day/week/year) in the same salary field, carrying the
  period in the `unit` field. `fact_job_listings` now normalizes every variant
  to a monthly basis (`salary_min_monthly` / `salary_max_monthly`, ~168 h/mo,
  ~21 d/mo, /12 for annual) using that `unit`, and the salary/city/trend marts
  aggregate the monthly columns instead of the raw min/max. `/salary` now shows
  each contract basis as a trustworthy monthly figure (per-hour rates no longer
  contaminate the medians), so the "un-normalized B2B" caveat was dropped.
- [x] Paid subscription tier: allow paid users to see more than 20 listings per
  notification run. ✅ Done. Per-tier caps in `payments.py`
  (`FREE_MAX_LISTINGS=20`, Plus 50, Pro 100, `listing_cap()`); `/latest` uses the
  buyer's cap directly, and the daily broadcast honours it via a `max_listings`
  field the bot stamps into the shared config store (so `notify.py` / the alert
  notebook don't need access to `payments.db`).
- [x] **Bot UX improvements (2026-08-11):** ✅ Done.
  - `/stats` removed from user-facing messages (admin-only command).
  - `/tech` vs `/myskills` roles clarified: `/tech` = filter, `/myskills` = ranking.
  - `/tolerance` no longer forgives seniority mismatches (hard filter).
  - `/tech`, `/myskills`, `/city` are now additive-only (no overwrite); use
    `clear` + `add` to replace. Explicit `add`/`remove` subcommands.
  - Multi-word tech names handled correctly (`Apache Airflow`, `GitHub Actions`).
  - Duplicates within a single `add` message are deduplicated.
  - `match_pct` is now listing-centric (% of listing's techs that user has).
  - `/latest` uses local DuckDB cache first (instant) before Databricks fallback.
  - Fixed float salary crash from DuckDB cache values.
- [x] **Migrate bronze/silver/gold compute off Databricks Free Edition** before
  enabling real Telegram Stars billing. ✅ Done. Self-hosted on the GCP e2-micro
  VM (Polars + DuckDB + dbt-duckdb, systemd timer alongside the bot). As of
  2026-08-12 all legacy Databricks code is **deleted** (notebooks/, resources/,
  dashboards/, databricks.yml, scraper/uploader.py, deploy-bundle.yml) and the
  bot/config no longer reference Databricks or the Volume mirror at all. Reason
  it mattered: Databricks Free Edition ToS prohibits commercial use.
- [x] **`/analytics reset` — admin command to clear analytics data.** ✅ Done.
  `/analytics reset` wipes `events` and `filter_choices` tables (keeps `users`
  intact). Admin-only.
- [x] **Show user ID in `/privacy`.** ✅ Done. Displays raw chat ID as copyable
  `<code>` so users can share it with admin for premium grants.
- [x] **`/givepremium <chat_id> [days|forever]` — admin command to grant premium.** ✅ Done.
  Also added `/revokepremium <chat_id>` to take it away. Admin menu shows all
  admin commands via `BotCommandScopeChat`.
- [ ] **Bot visibility & SEO.** Improve discoverability:
  - Set a clear BotFather short description and "About" text with keywords
    (IT jobs Poland, praca IT Polska, justjoin.it tracker, etc.)
  - Enable inline mode (even a stub) so the bot appears in Telegram search.
  - Create a simple landing page (GitHub Pages or similar) with the bot's
    @username, keywords, and a `tg://resolve?domain=BOT_USERNAME` link for
    Google indexing.
  - Add structured data (JSON-LD) and meta tags targeting "telegram bot IT jobs
    Poland" queries.
  - Submit the landing page URL to Google Search Console.
- [ ] **Fix daily scrape workflow (GitHub Actions).** Guard added (fails fast
  with clear error if secrets empty). Still need to actually set the secrets:
  ```
  gh secret set VM_HOST --body "<VM_PUBLIC_IP>"
  gh secret set VM_USER --body "kromylodd"
  gh secret set VM_SSH_KEY < ~/.ssh/google_compute_engine
  ```
- [x] **Fix tracker menu (`/mytracker`) not showing all tracked offers.** ✅ Done.
  Paginated (10/page) with ◀️▶️ navigation and status filter buttons
  (All / ✅ Applied / 👀 Interested / ❌ Rejected). Callback pattern `trkpg:`.
- [ ] **Create public Telegram channel** (`@PolishITJobs` or similar). Auto-post
  daily market highlights (top 5 new offers, salary stat of the day). Pin bot
  link. Channels are indexed by Telegram search → free organic discovery.
- [ ] **Post bot in Polish/international dev groups & subreddits.** Target:
  "Praca IT", "Junior IT Polska", r/cscareerquestionsEU, r/poland weekly job
  thread, "Remote Jobs" / "Developer Jobs Europe" Telegram groups.
- [ ] **Inline referral system.** `/start ref_<user_hash>` tracking. "Share with
  a friend" button in daily digest. Reward: 3 extra days of Plus trial for both
  referrer and referee.
- [ ] **Landing page for Google indexing (GitHub Pages).** Keywords: "praca IT
  Polska", "IT jobs Poland Telegram", "justjoin.it alerts". JSON-LD structured
  data, `tg://resolve` link, screenshots/GIFs. Submit to Google Search Console.
- [ ] **SEO blog posts (auto-generated from data).** "Top IT Technologies in
  Poland 2026" and "Average IT Salaries by Technology" — generated from DuckDB
  marts, updated monthly, each linking to the bot.
- [ ] **LinkedIn weekly posts.** "This week in Polish IT hiring" — 3-5 bullet
  stats from data. Tag #pracaIT #ITjobs. Link to bot.
- [ ] **Twitter/X auto-posts.** Daily: "📊 Today: N new IT jobs. Top tech: X.
  Median salary: Y PLN/mo." Automated via GitHub Action from DuckDB.
- [ ] **Submit to bot directories.** Telegram Bot List (t.me/botlist), Product
  Hunt, alternativeto.com.
- [ ] **Viral features in the bot:**
  - `/share` — generates a shareable card image (user's top tech + salary range)
  - Weekly "Market Pulse" chart auto-posted to the channel
  - "Your profile vs market" comparison (high share incentive)
  - Referral leaderboard (opt-in gamification)
