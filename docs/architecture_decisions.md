# Architecture Decisions

## Why Databricks Free Edition (not a paid tier)?

Portfolio project — demonstrates the platform without incurring cost. Free Edition provides:
- Serverless compute
- One 2X-Small SQL warehouse
- Unity Catalog (governance)
- Enough to run the full pipeline end-to-end

The constraints it imposes (restricted outbound networking, no OIDC federation, 5-task concurrency cap) are documented as known limitations and worked around explicitly.

## Why GitHub Actions for scraping (not Databricks)?

Databricks Free Edition restricts outbound internet to a Databricks-controlled allowlist. justjoin.it is not on that list. GitHub Actions provides unrestricted outbound access at no cost for public repos.

## Why dbt tests instead of Great Expectations?

1. Different DQ approach from project #1 (which used GE) — demonstrates breadth
2. GE's Python-version friction (hit on Silesia project) doesn't need solving twice
3. dbt tests integrate natively with the dbt build → no separate orchestration step

## Why Telegram (not Discord/email)?

- Dead-simple API: one HTTP POST, no OAuth, no gateway connection
- Standard choice for developer alerting bots in the data/trading community
- This is push-to-self notifications, not a community — Telegram is purpose-built for this
- Not about Polish messaging market share; it's about using the right tool for the job

## Why `relationships` tests over `accepted_values`?

Lesson from Silesia: `accepted_values` breaks silently when new values appear (a city you didn't list, a tech you didn't expect). `relationships` tests against a seed table that you explicitly maintain. When a new value appears, the test fails visibly, prompting you to add it to the lookup — better data governance.

## Why bridge tables?

A job listing can require multiple technologies and be posted for multiple cities. This is a many-to-many relationship that a simple foreign key on the fact table can't represent. Bridge tables (`bridge_listing_technology`, `bridge_listing_city`) solve this cleanly. Silesia's schema didn't need this pattern (one apartment = one city = one price), so this is a genuinely new modeling challenge.

## Why medallion naming (bronze/silver/gold) instead of raw/staging/marts?

Deliberate signal that you know both conventions. Silesia uses raw/staging/marts (BigQuery-idiomatic). This project uses bronze/silver/gold (Databricks/lakehouse-idiomatic). Both are valid; using both across projects shows range.

## Auth: PAT vs. OIDC

Databricks Free Edition does not expose the account-level API needed to configure GitHub OIDC workload identity federation (the Databricks equivalent of GCP WIF). CI/CD therefore uses a Personal Access Token stored as a GitHub encrypted secret. This is a documented, understood trade-off — not an oversight. The Silesia project demonstrates the fully keyless pattern; this project documents why it's not possible here.
