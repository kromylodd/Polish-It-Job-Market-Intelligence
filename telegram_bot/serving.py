"""
Fast local serving layer for premium analytics commands.

The premium commands (/salary, /trend, /skills, /company) must respond in well
under a second. Querying the Databricks SQL warehouse per user request is a
non-starter: a cold Free-Edition warehouse can take tens of seconds to wake, and
every query re-runs a network round trip.

Everything those commands need is already precomputed by dbt as small gold marts
(hundreds to a few thousand rows). So instead of querying Databricks live, we
periodically *sync* those marts into a local DuckDB file next to the bot and
answer every premium query from DuckDB — instant, offline-capable, and immune to
warehouse cold starts / throttling.

Design:
  * ``sync_marts()`` pulls each gold mart from Databricks (via the SQL connector)
    into the local DuckDB file. Called on bot startup and periodically in the
    background (see bot.py), and on-demand via the admin ``/refresh`` command.
  * The query helpers (``salary_for_tech``, ``market_trend``, ``skills_for_tech``,
    ``company_intel``, ...) read only from DuckDB. They never touch Databricks, so
    they're fast and keep working even if Databricks is unavailable.
  * Everything degrades gracefully: if DuckDB or the connector isn't installed, or
    no sync has happened yet, the helpers return ``None``/empty and the caller shows
    a friendly "data not ready" message rather than crashing.

The DuckDB file is disposable — it's a cache rebuilt from the marts, so it's
gitignored and safe to delete.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Local cache file (gitignored). Override for tests via SERVING_DB_PATH.
SERVING_DB_PATH = Path(
    os.environ.get(
        "SERVING_DB_PATH",
        str(Path(__file__).parent / "serving.duckdb"),
    )
)

# Gold marts to mirror locally, keyed by the local DuckDB table name.
# Values are the fully-qualified Databricks source tables.
MARTS: dict[str, str] = {
    "salary_by_technology": "job_market.gold.mart_salary_by_technology",
    "market_trends": "job_market.gold.mart_market_trends",
    "tech_co_occurrence": "job_market.gold.mart_tech_co_occurrence",
    "demand_by_technology": "job_market.gold.mart_demand_by_technology",
    "city_summary": "job_market.gold.mart_city_summary",
    "market_snapshot": "job_market.gold.mart_market_snapshot",
}

# Consider the cache stale after this many seconds (used to decide auto-refresh).
DEFAULT_TTL_SECONDS = 6 * 3600


# --------------------------------------------------------------------------- #
# Connection helpers
# --------------------------------------------------------------------------- #
def _duckdb_connect(read_only: bool = False):
    """Open the local DuckDB cache. Returns None if duckdb isn't installed."""
    try:
        import duckdb
    except ImportError:
        logger.warning("duckdb not installed; serving layer disabled")
        return None

    if read_only and not SERVING_DB_PATH.exists():
        return None

    SERVING_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        return duckdb.connect(str(SERVING_DB_PATH), read_only=read_only)
    except Exception as e:  # e.g. locked file, corrupt cache
        logger.warning("Could not open DuckDB cache (%s): %s", SERVING_DB_PATH, e)
        return None


def _databricks_connect():
    """Open a Databricks SQL connection from env vars. Returns None if unavailable."""
    host = os.environ.get("DATABRICKS_HOST", "").replace("https://", "").strip().rstrip("/")
    token = os.environ.get("DATABRICKS_TOKEN", "").strip()
    warehouse_id = os.environ.get("DATABRICKS_WAREHOUSE_ID", "").strip()
    if not (host and token and warehouse_id):
        logger.info("Databricks env vars not set; cannot sync marts")
        return None
    try:
        from databricks import sql
    except ImportError:
        logger.warning("databricks-sql-connector not installed; cannot sync marts")
        return None
    # Bound the retry policy. The connector defaults to retrying for 900s (15 min)
    # against a cold/unreachable warehouse; because sync_marts runs in a non-daemon
    # worker thread, that would also delay clean process shutdown by up to 15 min.
    # Fail fast instead — a missed sync just means the cache stays as-is until the
    # next interval (or /refresh). Override via SYNC_RETRY_MAX_SECONDS.
    retry_seconds = int(os.environ.get("SYNC_RETRY_MAX_SECONDS", "90"))
    return sql.connect(
        server_hostname=host,
        http_path=f"/sql/1.0/warehouses/{warehouse_id}",
        access_token=token,
        _retry_stop_after_attempts_count=5,
        _retry_stop_after_attempts_duration=float(retry_seconds),
        _socket_timeout=float(retry_seconds),
    )


# --------------------------------------------------------------------------- #
# Sync
# --------------------------------------------------------------------------- #
def sync_marts(marts: dict[str, str] | None = None) -> dict[str, int]:
    """Pull each gold mart from Databricks into the local DuckDB cache.

    Returns a mapping of ``{table_name: row_count}`` for the marts synced.
    On any failure the previous cache is left intact (writes go to a temp table
    that atomically replaces the live one only after a successful fetch).

    This is a blocking network operation — callers run it in a thread.
    """
    marts = marts or MARTS
    db = _duckdb_connect(read_only=False)
    if db is None:
        return {}
    try:
        dbx = _databricks_connect()
    except Exception as e:
        # A cold/unreachable warehouse raises on OpenSession. Treat as "no sync"
        # rather than propagating — the cache just stays as-is until next time.
        logger.warning("Databricks connection failed; skipping sync: %s", e)
        db.close()
        return {}
    if dbx is None:
        db.close()
        return {}

    synced: dict[str, int] = {}
    try:
        with dbx:
            for table, source in marts.items():
                try:
                    rows, columns = _fetch_table(dbx, source)
                except Exception as e:
                    logger.warning("Failed to fetch %s: %s", source, e)
                    continue
                _replace_table(db, table, columns, rows)
                synced[table] = len(rows)
                logger.info("Synced %s -> %s (%d rows)", source, table, len(rows))
        _write_meta(db, synced)
    finally:
        db.close()
    return synced


def _fetch_table(dbx, source: str) -> tuple[list[tuple], list[str]]:
    """Fetch all rows + column names from a Databricks table."""
    with dbx.cursor() as cursor:
        cursor.execute(f"SELECT * FROM {source}")  # noqa: S608 - table names are constants
        columns = [desc[0] for desc in cursor.description]
        raw = cursor.fetchall()
    # Normalize numpy arrays / exotic types to plain Python for DuckDB insertion.
    rows = [tuple(_normalize(v) for v in row) for row in raw]
    return rows, columns


def _normalize(value: Any) -> Any:
    """Convert numpy arrays/scalars to plain Python; lists become comma strings.

    DuckDB can store lists, but keeping array columns as comma-joined strings keeps
    the local schema simple and is all the analytics helpers need.
    """
    if hasattr(value, "tolist"):
        value = value.tolist()
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value)
    return value


def _replace_table(db, table: str, columns: list[str], rows: list[tuple]) -> None:
    """Atomically (re)create ``table`` in DuckDB with the given columns/rows."""
    col_defs = ", ".join(f'"{c}"' for c in columns)
    placeholders = ", ".join(["?"] * len(columns))
    tmp = f"_tmp_{table}"

    db.execute(f'DROP TABLE IF EXISTS "{tmp}"')
    # Create an empty typed shell by selecting zero rows isn't possible without a
    # source; instead create with all VARCHAR then let DuckDB infer via inserts.
    # Simpler + robust: build from a VALUES-less CREATE then INSERT with params.
    col_shell = ", ".join(f'"{c}" VARCHAR' for c in columns)
    db.execute(f'CREATE TABLE "{tmp}" ({col_shell})')
    if rows:
        db.executemany(
            f'INSERT INTO "{tmp}" ({col_defs}) VALUES ({placeholders})',
            rows,
        )
    db.execute(f'DROP TABLE IF EXISTS "{table}"')
    db.execute(f'ALTER TABLE "{tmp}" RENAME TO "{table}"')


def _write_meta(db, synced: dict[str, int]) -> None:
    """Record the last sync timestamp so freshness can be checked."""
    db.execute("CREATE TABLE IF NOT EXISTS _sync_meta (synced_at DOUBLE, tables VARCHAR)")
    db.execute("DELETE FROM _sync_meta")
    db.execute(
        "INSERT INTO _sync_meta (synced_at, tables) VALUES (?, ?)",
        [time.time(), ",".join(synced.keys())],
    )


# --------------------------------------------------------------------------- #
# Freshness
# --------------------------------------------------------------------------- #
def last_sync_epoch() -> float | None:
    """Return the epoch seconds of the last successful sync, or None."""
    db = _duckdb_connect(read_only=True)
    if db is None:
        return None
    try:
        row = db.execute("SELECT synced_at FROM _sync_meta LIMIT 1").fetchone()
        return float(row[0]) if row else None
    except Exception:
        return None
    finally:
        db.close()


def is_ready() -> bool:
    """Whether the cache has at least one successful sync."""
    return last_sync_epoch() is not None


def is_stale(ttl_seconds: int = DEFAULT_TTL_SECONDS) -> bool:
    """Whether the cache is missing or older than ``ttl_seconds``."""
    ts = last_sync_epoch()
    if ts is None:
        return True
    return (time.time() - ts) > ttl_seconds


# --------------------------------------------------------------------------- #
# Query helpers (read-only, fast)
# --------------------------------------------------------------------------- #
def _query(sql: str, params: list | None = None) -> list[dict]:
    """Run a read-only query against the cache, returning list-of-dicts. []."""
    db = _duckdb_connect(read_only=True)
    if db is None:
        return []
    try:
        cur = db.execute(sql, params or [])
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception as e:
        logger.debug("Serving query failed: %s", e)
        return []
    finally:
        db.close()


def _num(value: Any) -> float | None:
    """Best-effort convert a stored VARCHAR to float."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


# Contract bases. justjoin.it quotes permanent (UoP) pay per MONTH, but B2B and
# mandate (umowa zlecenie) rates are quoted per HOUR *or* per month in the same
# field, with no period normalization upstream. Blending them is what produces
# nonsense like a 28-PLN "min" next to a 600k "max". Until the pipeline
# normalizes B2B to a monthly basis (see TODO), we keep the bases separate and
# only treat permanent/UoP as trustworthy monthly figures.
_MONTHLY_EMPLOYMENT = {"permanent", "any"}
_BASIS_LABEL = {
    "permanent": "Permanent (UoP)",
    "b2b": "B2B",
    "mandate": "Mandate (zlecenie)",
}


def _basis_for(employment_type: str | None) -> str:
    et = (employment_type or "").lower()
    if et in _MONTHLY_EMPLOYMENT:
        return "permanent"
    if et == "b2b":
        return "b2b"
    return "mandate"


def salary_for_tech(tech: str, seniority: str | None = None) -> dict | None:
    """Salary stats for a technology, split by contract basis + currency.

    Reads the ``salary_by_technology`` mart (granular by technology, seniority,
    employment_type, currency). Salary is period-normalized to a monthly basis
    upstream (fact_job_listings converts per-hour/day/year quotes to monthly),
    so B2B and UoP figures are directly comparable. We still group by (contract
    basis, currency) — B2B (gross) and permanent/UoP pay are genuinely
    different comp, so blending their medians would mislead — and report the
    P25-P75 interquartile band the mart computes, weighted by listing_count.
    Returns None if no data.
    """
    where = ["lower(technology_name) = lower(?)"]
    params: list = [tech]
    if seniority:
        where.append("lower(seniority) = lower(?)")
        params.append(seniority)
    rows = _query(
        f"SELECT * FROM salary_by_technology WHERE {' AND '.join(where)}",  # noqa: S608
        params,
    )
    if not rows:
        return None

    # Listing-count-weighted median / p25 / p75 per (basis, currency).
    acc: dict[tuple[str, str], dict] = {}
    total = 0
    for r in rows:
        cnt = int(_num(r.get("listing_count")) or 0)
        if cnt <= 0:
            continue
        total += cnt
        key = (_basis_for(r.get("employment_type")), r.get("currency") or "PLN")
        g = acc.setdefault(
            key,
            {
                "count": 0,
                "med_w": 0.0,
                "med_n": 0,
                "p25_w": 0.0,
                "p25_n": 0,
                "p75_w": 0.0,
                "p75_n": 0,
            },
        )
        g["count"] += cnt
        for col, wkey, nkey in (
            ("median_salary", "med_w", "med_n"),
            ("p25_salary", "p25_w", "p25_n"),
            ("p75_salary", "p75_w", "p75_n"),
        ):
            v = _num(r.get(col))
            if v is not None:
                g[wkey] += v * cnt
                g[nkey] += cnt
    if total == 0:
        return None

    groups = []
    for (basis, currency), g in acc.items():
        groups.append(
            {
                "basis": basis,
                "label": _BASIS_LABEL.get(basis, basis),
                "currency": currency,
                "count": g["count"],
                "median": round(g["med_w"] / g["med_n"]) if g["med_n"] else None,
                "p25": round(g["p25_w"] / g["p25_n"]) if g["p25_n"] else None,
                "p75": round(g["p75_w"] / g["p75_n"]) if g["p75_n"] else None,
                # All bases are period-normalized to a monthly basis upstream
                # (fact_job_listings converts per-hour/day/year quotes to
                # monthly before the marts aggregate), so every group is a
                # comparable monthly figure.
                "normalized": True,
            }
        )
    # Order by listing count desc (all groups are monthly-normalized now).
    groups.sort(key=lambda x: -x["count"])

    return {
        "technology": tech,
        "seniority": seniority,
        "listing_count": total,
        "groups": groups,
    }


def salary_by_seniority(tech: str) -> list[dict]:
    """Per-seniority median for the permanent/UoP PLN basis, sorted by rank.

    Restricted to permanent (UoP) PLN contracts as a single, consistent
    reference ladder — UoP monthly pay is the figure most job seekers compare
    against. (Salary is period-normalized upstream, so B2B could be included
    too, but UoP is kept as the canonical apples-to-apples baseline.) Each row:
    ``seniority``, ``n``, ``median``.
    """
    rows = _query(
        "SELECT seniority, "
        "sum(CAST(listing_count AS INTEGER)) AS n, "
        "sum(CAST(median_salary AS DOUBLE) * CAST(listing_count AS INTEGER)) "
        "  / nullif(sum(CAST(listing_count AS INTEGER)),0) AS median "
        "FROM salary_by_technology WHERE lower(technology_name)=lower(?) "
        "  AND lower(employment_type) IN ('permanent','any') "
        "  AND upper(currency)='PLN' "
        "GROUP BY seniority ORDER BY median",
        [tech],
    )
    order = {
        s: i
        for i, s in enumerate(["intern", "junior", "mid", "senior", "lead", "manager", "c_level"])
    }
    return sorted(rows, key=lambda r: order.get((r.get("seniority") or "").lower(), 99))


def skills_for_tech(tech: str, limit: int = 8) -> dict | None:
    """Co-occurring technologies for ``tech`` with a co-occurrence percentage.

    Percentage = (listings with both tech and Y) / (listings with tech). The
    denominator is derived from the demand mart (total distinct listings per tech).
    Returns {"technology", "total", "related": [{"tech","count","pct"}]} or None.
    """
    pairs = _query(
        "SELECT tech_a, tech_b, CAST(co_occurrence_count AS INTEGER) AS c "
        "FROM tech_co_occurrence "
        "WHERE lower(tech_a)=lower(?) OR lower(tech_b)=lower(?)",
        [tech, tech],
    )
    if not pairs:
        return None

    total = _tech_total_listings(tech)
    related = []
    for p in pairs:
        a, b = p.get("tech_a"), p.get("tech_b")
        other = b if (a or "").lower() == tech.lower() else a
        cnt = int(p.get("c") or 0)
        pct = round(100 * cnt / total) if total else None
        related.append({"tech": other, "count": cnt, "pct": pct})
    related.sort(key=lambda r: r["count"], reverse=True)
    return {"technology": tech, "total": total, "related": related[:limit]}


def _tech_total_listings(tech: str) -> int:
    """Total distinct listings mentioning a tech (summed from the demand mart).

    Excludes the first collection week (partial bootstrap) so the co-occurrence
    denominator matches the trend series, which also drops it.
    """
    rows = _query(
        "SELECT sum(CAST(listing_count AS INTEGER)) AS n "
        "FROM demand_by_technology WHERE lower(technology_name)=lower(?) "
        "AND week_start > (SELECT min(week_start) FROM demand_by_technology)",
        [tech],
    )
    if rows and rows[0].get("n") is not None:
        return int(rows[0]["n"])
    return 0


def market_trend(weeks: int = 8) -> list[dict]:
    """Recent daily market-trend rows (most recent ``weeks`` weeks), oldest first.

    Returns rows with full_date, new_listings, rolling_7d_listings,
    rolling_7d_avg_salary — suitable for a chart or a text summary. The earliest
    day ever collected is dropped: the pipeline's first run captured only a
    partial day, which otherwise shows as an artificially low leading bar and
    inflates the next day's change. Once that bootstrap day ages out of the
    window the filter is a no-op.
    """
    rows = _query(
        "SELECT full_date, "
        "CAST(new_listings AS INTEGER) AS new_listings, "
        "CAST(rolling_7d_listings AS INTEGER) AS rolling_7d_listings, "
        "CAST(rolling_7d_avg_salary AS DOUBLE) AS rolling_7d_avg_salary "
        "FROM market_trends "
        "WHERE full_date > (SELECT min(full_date) FROM market_trends) "
        "ORDER BY full_date DESC LIMIT ?",
        [weeks * 7],
    )
    return list(reversed(rows))


def tech_demand_trend(tech: str, limit: int = 12) -> list[dict]:
    """Weekly demand (listing_count + WoW change) for a tech, oldest first.

    Drops the first collection week (the same partial-bootstrap artifact as
    ``market_trend``): its ``wow_change`` compares against a missing/partial
    prior week and reads as a spurious spike.
    """
    rows = _query(
        "SELECT week_start, "
        "CAST(listing_count AS INTEGER) AS listing_count, "
        "CAST(wow_change AS INTEGER) AS wow_change "
        "FROM demand_by_technology WHERE lower(technology_name)=lower(?) "
        "AND week_start > (SELECT min(week_start) FROM demand_by_technology) "
        "ORDER BY week_start DESC LIMIT ?",
        [tech, limit],
    )
    return list(reversed(rows))


def company_intel(company: str) -> dict | None:
    """Listing intel for a company from the all-seniorities snapshot mart.

    Returns listing_count, avg salary range, and a few sample titles. Uses a
    substring (case-insensitive) match so partial names work.
    """
    rows = _query(
        "SELECT title, salary_min, salary_max, currency, seniority, workplace_type "
        "FROM market_snapshot WHERE lower(company_name) LIKE lower(?)",
        [f"%{company}%"],
    )
    if not rows:
        return None
    mins = [v for v in (_num(r.get("salary_min")) for r in rows) if v]
    maxes = [v for v in (_num(r.get("salary_max")) for r in rows) if v]
    return {
        "company": company,
        "listing_count": len(rows),
        "avg_min": round(sum(mins) / len(mins)) if mins else None,
        "avg_max": round(sum(maxes) / len(maxes)) if maxes else None,
        "currency": rows[0].get("currency", "PLN"),
        "sample_titles": [r.get("title") for r in rows[:5] if r.get("title")],
    }


def top_hiring_companies(limit: int = 10) -> list[dict]:
    """Companies with the most listings in the current snapshot."""
    return _query(
        "SELECT company_name, count(*) AS listing_count "
        "FROM market_snapshot WHERE company_name IS NOT NULL "
        "GROUP BY company_name ORDER BY listing_count DESC LIMIT ?",
        [limit],
    )


def hot_technologies(limit: int = 10) -> list[dict]:
    """Technologies with the biggest recent week-over-week demand increase."""
    return _query(
        "SELECT technology_name, "
        "CAST(listing_count AS INTEGER) AS listing_count, "
        "CAST(wow_change AS INTEGER) AS wow_change "
        "FROM demand_by_technology "
        "WHERE week_start = (SELECT max(week_start) FROM demand_by_technology) "
        "ORDER BY wow_change DESC LIMIT ?",
        [limit],
    )


def rank_listings_by_skills(listings: list[dict], skills: list[str]) -> list[dict]:
    """Rank listings by percent overlap with the user's skill set.

    Adds ``match_pct`` and ``matched_skills`` to a copy of each listing and returns
    them sorted by match_pct desc. Pure set-overlap — no ML, no DuckDB needed.
    """
    if not skills:
        return listings
    wanted = {s.strip().lower() for s in skills if s.strip()}
    if not wanted:
        return listings

    ranked = []
    for listing in listings:
        techs = listing.get("technologies", [])
        if isinstance(techs, str):
            techs = [t.strip() for t in techs.split(",") if t.strip()]
        listing_techs = {str(t).lower() for t in techs}
        matched = wanted & listing_techs
        pct = round(100 * len(matched) / len(wanted)) if wanted else 0
        item = dict(listing)
        item["match_pct"] = pct
        item["matched_skills"] = sorted(matched)
        ranked.append(item)
    ranked.sort(key=lambda x: x["match_pct"], reverse=True)
    return ranked
