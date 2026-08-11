# Migration Plan: Databricks Free Edition → Self-Hosted DuckDB/Polars

## Motivation

Databricks Free Edition ToS **prohibits commercial use**. Once paying Telegram
Stars subscribers exist, the pipeline serving them cannot legally run on
Databricks Free Edition. The solution: self-host the entire data pipeline on
the existing GCP e2-micro VM that already runs the Telegram bot 24/7.

## Current Architecture (Databricks)

```
GitHub Actions (scrape.yml)
  → scraper.py  → raw_listings_latest.json
  → uploader.py → Databricks Volume (.json.gz)
  → API run-now → Databricks Job:
      01_bronze_ingest (Spark Auto Loader → Delta)
      02_silver_clean  (Spark dedup/standardize)
      03_silver_tech_parse (Spark UDFs → tech canonicalization)
      run_dbt.py (dbt-databricks → gold star schema)
      run_dbt_test.py (data quality)

Telegram bot (GCP VM, 24/7)
  → serving.py sync_marts() pulls gold marts from Databricks SQL warehouse → local DuckDB cache
  → notify.py run_daily_broadcast() queries Databricks SQL warehouse at 08:00 Warsaw
```

## Target Architecture (Self-Hosted)

```
GitHub Actions (scrape.yml)
  → scraper.py → raw_listings_latest.json
  → SCP to VM: /opt/pipeline/data/raw_listings_latest.json
  (No Databricks upload, no API trigger)

GCP VM (e2-micro, 1GB RAM) — systemd timer triggers after file arrives:
  pipeline/
    bronze_ingest.py   (Polars: JSON → DuckDB bronze table)
    silver_clean.py    (DuckDB SQL: dedup, standardize)
    silver_tech_parse.py (Polars: regex/canonicalization → DuckDB silver table)
  dbt/ (dbt-duckdb adapter)
    stg_listings → fact/dims/bridges → analytics marts (unchanged SQL, adapted to DuckDB)
  run_pipeline.py (orchestrator: bronze → silver → tech → dbt build → dbt test)

Telegram bot (same VM, same process)
  → serving.py reads directly from the pipeline DuckDB file (no Databricks sync)
  → notify.py reads from local DuckDB (no Databricks SQL connector)
```

## Tech Stack

| Layer | Before | After |
|-------|--------|-------|
| Bronze ingest | Spark Auto Loader + Delta Lake | Polars → DuckDB table (append) |
| Silver clean | PySpark DataFrame ops | DuckDB SQL (run from Python) |
| Silver tech parse | PySpark UDFs | Polars (regex/canonicalization) → DuckDB |
| Gold (star schema) | dbt-databricks (Unity Catalog) | dbt-duckdb (local .duckdb file) |
| Orchestration | Databricks Job (5 tasks) | Python script + systemd timer |
| File transfer | Databricks SDK (Volume upload) | SCP/rsync from GitHub Actions |
| Serving cache | DuckDB ← Databricks SQL sync | DuckDB shared directly (pipeline output = serving source) |
| Notifications | databricks-sql-connector | Local DuckDB read |
| Scheduling | Databricks Job trigger via API | systemd timer (05:00 Warsaw) |

### Key Dependencies

**Pipeline (new)**:
- `polars==1.4.1` — DataFrame processing (fast, zero JVM, ~30MB RAM for 10k listings)
- `duckdb==1.1.3` — analytical DB (already used by bot for serving cache)
- `dbt-core==1.8.7` + `dbt-duckdb==1.8.3` — gold layer transformations

**Removed**:
- `databricks-sdk` — no longer needed in scraper or pipeline
- `databricks-sql-connector` — no longer needed in bot
- `pyspark` — was only available inside Databricks runtime

## Migration Phases

### Phase 1: Pipeline Module (pipeline/)
1. `pipeline/__init__.py` — shared config (DB path, schema names)
2. `pipeline/bronze_ingest.py` — read JSON, append to DuckDB `bronze.raw_job_listings`
3. `pipeline/silver_clean.py` — dedup, standardize (DuckDB SQL)
4. `pipeline/silver_tech_parse.py` — tech canonicalization (Polars + DuckDB)
5. `pipeline/run_pipeline.py` — orchestrator

### Phase 2: dbt Adaptation
1. Add `dbt-duckdb` profile in `dbt/profiles.yml`
2. Adapt SQL syntax: `collect_set` → `list()`, `lateral view explode` → `unnest()`,
   `percentile_approx` → `quantile_cont`, `date_format`/`dayofweek` → DuckDB equivalents
3. Validate `dbt build` + `dbt test` against local DuckDB

### Phase 3: Bot Integration
1. `serving.py` — remove `_databricks_connect()` and `sync_marts()`. Replace with
   direct read from pipeline DuckDB. The pipeline output IS the serving cache.
2. `notify.py` — replace `get_sql_connection()` + Databricks queries with local DuckDB reads.
   Move `telegram_alerts_sent` to a local SQLite/DuckDB table on the VM.

### Phase 4: Deployment & Scheduling
1. `scrape.yml` — replace Databricks upload + API trigger with SCP to VM + systemd timer kick
2. `deploy/setup_vm.sh` — install pipeline deps, set up systemd timer
3. Remove old Databricks bundle files (`databricks.yml`, `resources/jobs.yml`)

## Data Flow (Post-Migration)

```
03:00 UTC — GitHub Actions cron
  ├── scraper.py → data/raw_listings_latest.json
  └── SCP → VM:/opt/pipeline/data/raw_listings_latest.json

03:15 UTC (approx) — systemd timer or path-trigger on VM
  └── run_pipeline.py
      ├── bronze_ingest.py  → pipeline.duckdb: bronze.raw_job_listings
      ├── silver_clean.py   → pipeline.duckdb: silver.stg_listings
      ├── silver_tech_parse.py → pipeline.duckdb: silver.listings_with_tech
      └── dbt build → pipeline.duckdb: gold.* (all marts)

08:00 Warsaw — bot's daily_broadcast job (already exists)
  └── notify.run_daily_broadcast()
      └── reads pipeline.duckdb: gold.mart_market_snapshot
      └── dedup via local telegram_alerts_sent table
      └── sends matching listings to each user
```

## Resource Constraints (e2-micro: 0.25 vCPU burst, 1GB RAM)

- Raw JSON file: ~30-40MB uncompressed, ~10k listings
- Polars processes this comfortably in <200MB RAM
- DuckDB database: ~50MB for full star schema
- Pipeline runtime: estimated 30-60 seconds total
- The bot uses ~50-80MB at rest → plenty of headroom

## Risks & Mitigations

| Risk | Mitigation |
|------|-----------|
| DuckDB write lock conflicts (pipeline vs bot reads) | Use WAL mode; bot uses read-only connections |
| e2-micro OOM during pipeline | Polars streaming/lazy mode; process in chunks if needed |
| Pipeline failure → stale data | Bot already handles stale cache gracefully (shows last-known data) |
| GitHub Actions SCP auth | Use SSH deploy key (no PAT needed); key stored as GH secret |
| dbt SQL syntax differences | All models tested; DuckDB is quite compatible with ANSI SQL |

## Files to Create

```
pipeline/
├── __init__.py          # Config: DB path, schema names
├── bronze_ingest.py     # JSON → DuckDB bronze
├── silver_clean.py      # Dedup + standardize
├── silver_tech_parse.py # Tech canonicalization
└── run_pipeline.py      # Orchestrator

dbt/profiles.yml         # Add duckdb profile
dbt/models/**/*.sql      # Adapt Spark-specific syntax to DuckDB

deploy/
├── pipeline.timer       # systemd timer
├── pipeline.service     # systemd service for one-shot pipeline run
└── setup_vm.sh          # Updated with pipeline deps

pipeline-requirements.txt  # polars, duckdb, dbt-core, dbt-duckdb
```

## Files to Modify

- `telegram_bot/serving.py` — remove Databricks sync, read from pipeline DB
- `telegram_bot/notify.py` — remove Databricks SQL connector, use local DB
- `telegram_bot/requirements.txt` — remove databricks-sql-connector, databricks-sdk
- `.github/workflows/scrape.yml` — replace Databricks trigger with SCP + pipeline kick
- `scraper/requirements.txt` — remove databricks-sdk
- `deploy/setup_vm.sh` — add pipeline venv + systemd timer

## Files to Remove (cleanup, after validation)

- `databricks.yml` — bundle config (no longer needed)
- `resources/jobs.yml` — Databricks job definition
- `resources/volumes.yml` — Volume definition
- `.databricks/` — local bundle state
- `scraper/uploader.py` — Databricks SDK uploader
- `notebooks/run_dbt.py`, `notebooks/run_dbt_test.py` — Databricks-specific wrappers

## Rollback Plan

Keep all Databricks resources (workspace, Volume, Delta tables) intact during
migration. The scrape.yml can be reverted to the Databricks trigger path at any
time. The bot can be switched back to Databricks SQL sync by restoring the old
serving.py. Full rollback = one `git revert`.
