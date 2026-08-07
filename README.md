# Polish IT Job Market Intelligence

End-to-end lakehouse data platform for the Polish IT job market, built on Databricks.

> Stage 3 companion project to [Polish Housing Market Intelligence Platform](https://github.com/kromylodd/Polish-Housing-Market-Intelligence-Platform) — deliberately using a different stack (Databricks/PySpark/Delta Lake vs. GCP/BigQuery/Airflow) to demonstrate cross-platform fluency.

## Architecture

```
GitHub Actions (cron)
    │
    ▼
Scrape justjoin.it (HTTP + RSC payload parse)
    │
    ▼
Upload raw JSON → Unity Catalog Volume (Databricks Files SDK)
    │
    ▼ [file arrival trigger]
Databricks Workflow:
    ├── Bronze: ingest → Delta table (append-only)
    ├── Silver: dedupe, standardize, tech-stack parsing (PySpark)
    ├── dbt build: staging → star schema (dims/fact/bridges → marts)
    ├── dbt test: data quality gate
    └── Telegram alert: new listings matching saved filters
```

## Tech Stack

| Layer | Tool |
|-------|------|
| Platform | Databricks Free Edition (Unity Catalog, Delta Lake) |
| Ingestion | GitHub Actions + Databricks Files SDK |
| Transformation | PySpark |
| Modeling | dbt-databricks (star schema with bridge tables) |
| Orchestration | Databricks Workflows (Lakeflow Jobs) |
| IaC | Databricks Asset Bundles |
| CI/CD | GitHub Actions (lint, test, deploy) |
| Data Quality | dbt tests (not Great Expectations — different DQ approach vs. project #1) |
| Alerting | Telegram Bot API (push-based notifications) |
| Dashboard | Databricks Lakeview Dashboards |

## Key Differentiators vs. Project #1 (Silesia Housing)

- Lakehouse platform (Databricks/Unity Catalog/Delta) instead of GCP-native (BigQuery/GCS)
- PySpark for transformation instead of pandas/SQL-only
- Databricks Workflows instead of Airflow — a second orchestrator
- Databricks Asset Bundles instead of Terraform — a second IaC tool
- Many-to-many bridge tables (technology, city) — star schema pattern project #1 didn't need
- Regex/NLP-adjacent text parsing on free-text job descriptions
- Push-based alerting (Telegram) instead of purely pull-based dashboards

## Project Structure

```
polish-it-job-market-intelligence/
├── databricks.yml              # Asset Bundle root config
├── resources/                  # Workflow & Volume definitions
├── scraper/                    # HTTP scraper + parser + uploader
├── notebooks/                  # PySpark scripts (bronze/silver)
├── dbt/                        # dbt-databricks project (star schema)
├── telegram_bot/               # Telegram push notifications
├── .github/workflows/          # CI/CD + scheduled scrape
└── docs/                       # Architecture diagrams, decisions
```

## Data Model

**Medallion architecture** (Unity Catalog schemas):
- `bronze` — raw ingested Delta tables, append-only
- `silver` — cleaned, deduplicated, tech-parsed listings
- `gold` — star schema: dims, fact, bridges, analytical marts

**Star schema** (gold):
- `fact_job_listings` — one row per listing
- `dim_company`, `dim_technology`, `dim_seniority`, `dim_employment_type`, `dim_workplace_type`, `dim_city`, `dim_category`, `dim_date`
- `bridge_listing_technology`, `bridge_listing_city` — many-to-many relationships

**Marts:**
- `mart_salary_by_technology` — salary percentiles per tech/seniority/contract
- `mart_demand_by_technology` — listing counts and trends over time
- `mart_tech_co_occurrence` — which technologies appear together
- `mart_junior_market_snapshot` — junior-specific view (your job search, answered)
- `mart_city_summary` — listings, salary, top tech per city
- `mart_market_trends` — volume and salary trends

## Known Limitations

- **No OIDC/keyless auth for CI/CD**: Databricks Free Edition lacks account-level API access required for workload identity federation (the direct equivalent of the GCP WIF pattern used in project #1). CI/CD authenticates via a Personal Access Token stored as a GitHub encrypted secret. This is a documented trade-off, not an oversight.
- **Outbound networking from Databricks serverless compute is restricted** to a Databricks-controlled allowlist. Scraping and (potentially) Telegram notifications run in GitHub Actions, not inside Databricks.
- **5 concurrent job tasks max** on Free Edition — pipeline is mostly sequential by design.

## Setup

### Prerequisites
- Databricks Free Edition workspace
- Databricks CLI installed (`databricks configure`)
- Python 3.12+ with conda
- GitHub account (for Actions CI/CD)

### Local Development
```bash
# Clone
git clone https://github.com/kromylodd/polish-it-job-market-intelligence.git
cd polish-it-job-market-intelligence

# Install dev dependencies
pip install -r requirements-dev.txt

# Configure Databricks CLI
databricks configure

# Validate bundle
databricks bundle validate

# Deploy (dev)
databricks bundle deploy -t dev
```

## License

MIT
