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
