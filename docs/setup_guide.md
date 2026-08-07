# Setup Guide

## Prerequisites

1. **Databricks Free Edition workspace** — [databricks.com/try-databricks](https://databricks.com/try-databricks)
2. **Databricks CLI** — `curl -fsSL https://raw.githubusercontent.com/databricks/setup-cli/main/install.sh | sh`
3. **Python 3.12+** with conda or venv
4. **GitHub account** with a repo for this project

## Local Environment Setup

### 1. Clone the repo

```bash
git clone https://github.com/kromylodd/polish-it-job-market-intelligence.git
cd polish-it-job-market-intelligence
```

### 2. Create Python environment

```bash
conda create -n job-market python=3.12 -y
conda activate job-market
pip install -r requirements-dev.txt
pip install -r scraper/requirements.txt
```

### 3. Configure Databricks CLI

```bash
databricks configure
# Enter your workspace URL and Personal Access Token
```

### 4. Validate the bundle

```bash
databricks bundle validate -t dev
```

## GitHub Secrets

Configure these in your repo's Settings → Secrets and variables → Actions:

| Secret | Description |
|--------|-------------|
| `DATABRICKS_HOST` | Your workspace URL (e.g., `https://dbc-xxxxx.cloud.databricks.com`) |
| `DATABRICKS_TOKEN` | Personal Access Token |
| `DATABRICKS_WAREHOUSE_ID` | SQL Warehouse ID (for Telegram notifier queries) |
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | Your chat ID (get via @userinfobot) |

## Databricks Workspace Setup

### 1. Create the Unity Catalog structures

The Asset Bundle will create these on deploy, but you can also do it manually:

```sql
CREATE CATALOG IF NOT EXISTS job_market;
CREATE SCHEMA IF NOT EXISTS job_market.bronze;
CREATE SCHEMA IF NOT EXISTS job_market.silver;
CREATE SCHEMA IF NOT EXISTS job_market.gold;

CREATE VOLUME IF NOT EXISTS job_market.bronze.raw_listings;
```

### 2. Deploy the bundle

```bash
databricks bundle deploy -t dev
```

### 3. Verify

```bash
databricks bundle summary -t dev
```

## Testing Locally

```bash
# Lint
ruff check .
black --check .

# Unit tests
pytest scraper/tests/ -v

# dbt parse (no live connection needed)
cd dbt && dbt deps && dbt parse
```

## First Run

1. Trigger the scraper manually: `python -m scraper.scraper`
2. Upload to Volume: `python -m scraper.uploader data/raw_listings_*.json`
3. Monitor the Databricks Workflow in the workspace UI
4. Check bronze/silver/gold tables after pipeline completes
