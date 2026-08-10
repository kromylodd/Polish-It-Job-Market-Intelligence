# TODO

- [ ] **Salary period normalization (data-quality fix).** justjoin.it quotes
  permanent (UoP) pay per month but B2B / mandate (umowa zlecenie) rates per
  **hour** *or* per month in the same salary field, with no period marker
  carried through. As a result `fact_job_listings.salary_min/max` mixes hourly
  and monthly figures, so `mart_salary_by_technology` produces nonsense extremes
  (e.g. a 28-PLN hourly rate next to a 600k annual figure) for B2B.
  - Detect the salary period from the source API (justjoin.it exposes it) and
    normalize B2B hourly → monthly (~168 h/mo) and annual → monthly in the
    silver layer, before the gold marts aggregate.
  - Interim mitigation already shipped in the serving layer: `/salary` reports
    permanent (UoP) and B2B **separately**, uses the P25–P75 band instead of
    raw min/max, and flags B2B as un-normalized (`telegram_bot/serving.py`
    `salary_for_tech`). This TODO is the upstream root-cause fix that lets B2B
    be shown as a trustworthy monthly figure.
- [ ] Paid subscription tier: allow paid users to see more than 20 listings per notification run (current `MAX_PER_USER` cap in `telegram_bot/notify.py`)
