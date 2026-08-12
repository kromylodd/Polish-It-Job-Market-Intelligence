# TODO

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
- [ ] **Migrate bronze/silver/gold compute off Databricks Free Edition** before
  enabling real Telegram Stars billing. Decided approach: self-host on the
  existing GCP e2-micro VM (dbt-duckdb/Polars, systemd timer alongside the
  bot service) — see docs/architecture_decisions.md. Reason: Databricks Free
  Edition ToS explicitly prohibits commercial use, so once paying subscribers
  exist the pipeline serving them can't legally keep running there.
- [ ] **`/analytics reset` — admin command to clear analytics data.** Add a
  subcommand so `/analytics reset` wipes the command-popularity counters and
  total-interactions count, allowing a fresh start. Should delete rows from
  `events` and `filter_choices` tables (keep the `users` table intact so the
  user count stays).
- [ ] **Show user ID in `/privacy`.** Display the user's raw Telegram chat ID in
  the `/privacy` message (it's already visible to the user in Telegram settings
  and forwarded messages, so not a security concern). Useful so users can share
  their ID with the admin for premium grants.
- [ ] **`/givepremium <chat_id> [days|forever]` — admin command to grant premium.**
  Admin-only command that activates Pro tier for a given chat_id. If `forever`
  is passed, set expiry far in the future (e.g. 36500 days ≈ 100 years). If a
  number is passed, grant that many days. Default: 30 days. Calls
  `payments.activate(chat_id, "pro", days=N)`. Example:
  `/givepremium 123456789 forever`.
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
- [ ] **Fix daily scrape workflow (GitHub Actions).** The `Set up SSH key` step
  fails because `VM_HOST`, `VM_USER`, and `VM_SSH_KEY` secrets are empty/unset
  in the repository. Root cause: secrets were never configured in the repo's
  GitHub Settings → Secrets → Actions. Fix: add the three secrets via GitHub
  web UI or `gh secret set`. Also verify `ssh-keyscan` doesn't silently fail
  when `$VM_HOST` is empty (add a guard).
- [ ] **Fix tracker menu (`/mytracker`) not showing all tracked offers.** The
  menu currently renders only the first 40 entries (`apps[:40]`). If a user has
  more, the rest are invisible. Additionally, the menu is flat (no
  pagination/filtering by status). Fix: add status-filter buttons
  (All / Applied / Interested / Rejected) and paginate (10 per page, ◀️▶️
  buttons), or at minimum bump the cap and inform the user of overflow.
