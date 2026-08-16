"""
Fast local serving layer for premium analytics commands.

The premium commands (/salary, /trend, /skills, /company) must respond in well
under a second. Previously this module synced gold marts from Databricks into a
local DuckDB cache. After migration, the pipeline writes directly to the same
DuckDB file — so there is no sync step. The query helpers read the gold schema
tables directly from the pipeline database: instant, offline-capable, and immune
to network issues.

Design:
  * No sync needed: the pipeline (run_pipeline.py) populates the gold.* tables
    directly in pipeline.duckdb. The bot reads them with read_only=True.
  * The query helpers read from gold.* tables in the pipeline DuckDB.
  * Everything degrades gracefully: if DuckDB isn't installed, or no pipeline
    run has happened yet, the helpers return None/empty and the caller shows
    a friendly "data not ready" message rather than crashing.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Pipeline database — the single source of truth.
# The pipeline writes here; the bot reads with read_only=True.
SERVING_DB_PATH = Path(
    os.environ.get(
        "PIPELINE_DB_PATH",
        os.environ.get(
            "SERVING_DB_PATH",
            str(Path(__file__).parent.parent / "pipeline.duckdb"),
        ),
    )
)

# Gold mart table names (qualified with schema).
# The pipeline's dbt build creates these in the gold schema.
_T_SALARY = "gold.mart_salary_by_technology"
_T_TRENDS = "gold.mart_market_trends"
_T_COOCCURRENCE = "gold.mart_tech_co_occurrence"
_T_DEMAND = "gold.mart_demand_by_technology"
_T_CITY = "gold.mart_city_summary"
_T_SNAPSHOT = "gold.mart_market_snapshot"

# Consider the data stale after this many seconds (pipeline runs daily ~05:00).
DEFAULT_TTL_SECONDS = 24 * 3600


# --------------------------------------------------------------------------- #
# Connection helpers
# --------------------------------------------------------------------------- #
def _duckdb_connect(read_only: bool = True):
    """Open the pipeline DuckDB. Returns None if duckdb isn't installed."""
    try:
        import duckdb
    except ImportError:
        logger.warning("duckdb not installed; serving layer disabled")
        return None

    if not SERVING_DB_PATH.exists():
        logger.debug("Pipeline database not found: %s", SERVING_DB_PATH)
        return None

    try:
        return duckdb.connect(str(SERVING_DB_PATH), read_only=read_only)
    except Exception as e:
        logger.warning("Could not open pipeline DB (%s): %s", SERVING_DB_PATH, e)
        return None


# --------------------------------------------------------------------------- #
# Freshness
# --------------------------------------------------------------------------- #
def last_sync_epoch() -> float | None:
    """Return the mtime of the pipeline DB file as a proxy for 'last sync'.

    After migration there's no _sync_meta table — the pipeline just writes
    directly. We use the file modification time as freshness indicator.
    """
    if not SERVING_DB_PATH.exists():
        return None
    return SERVING_DB_PATH.stat().st_mtime


def is_ready() -> bool:
    """Whether the pipeline database exists and has gold tables."""
    db = _duckdb_connect(read_only=True)
    if db is None:
        return False
    try:
        db.execute(f"SELECT 1 FROM {_T_SNAPSHOT} LIMIT 1").fetchone()
        return True
    except Exception:
        return False
    finally:
        db.close()


def is_stale(ttl_seconds: int = DEFAULT_TTL_SECONDS) -> bool:
    """Whether the pipeline data is older than ``ttl_seconds``."""
    ts = last_sync_epoch()
    if ts is None:
        return True
    return (time.time() - ts) > ttl_seconds


# --------------------------------------------------------------------------- #
# Query helpers (read-only, fast)
# --------------------------------------------------------------------------- #
def _query(sql: str, params: list | None = None) -> list[dict]:
    """Run a read-only query against the pipeline DB, returning list-of-dicts."""
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
    """Best-effort convert a value to float."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


# Contract bases.
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

    Reads the salary_by_technology mart (granular by technology, seniority,
    employment_type, currency). All figures are period-normalized to monthly.
    Returns None if no data.
    """
    where = ["lower(technology_name) = lower(?)"]
    params: list = [tech]
    if seniority:
        where.append("lower(seniority) = lower(?)")
        params.append(seniority)
    rows = _query(
        f"SELECT * FROM {_T_SALARY} WHERE {' AND '.join(where)}",  # noqa: S608
        params,
    )
    if not rows:
        return None

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
                "normalized": True,
            }
        )
    groups.sort(key=lambda x: -x["count"])

    return {
        "technology": tech,
        "seniority": seniority,
        "listing_count": total,
        "groups": groups,
    }


def salary_by_seniority(tech: str) -> list[dict]:
    """Per-seniority median for the permanent/UoP PLN basis, sorted by rank."""
    rows = _query(
        f"SELECT seniority, "
        f"sum(CAST(listing_count AS INTEGER)) AS n, "
        f"sum(CAST(median_salary AS DOUBLE) * CAST(listing_count AS INTEGER)) "
        f"  / nullif(sum(CAST(listing_count AS INTEGER)),0) AS median "
        f"FROM {_T_SALARY} WHERE lower(technology_name)=lower(?) "
        f"  AND lower(employment_type) IN ('permanent','any') "
        f"  AND upper(currency)='PLN' "
        f"GROUP BY seniority ORDER BY median",
        [tech],
    )
    order = {
        s: i
        for i, s in enumerate(["intern", "junior", "mid", "senior", "lead", "manager", "c_level"])
    }
    return sorted(rows, key=lambda r: order.get((r.get("seniority") or "").lower(), 99))


def skills_for_tech(tech: str, limit: int = 8) -> dict | None:
    """Co-occurring technologies for ``tech`` with a co-occurrence percentage."""
    pairs = _query(
        f"SELECT tech_a, tech_b, CAST(co_occurrence_count AS INTEGER) AS c "
        f"FROM {_T_COOCCURRENCE} "
        f"WHERE lower(tech_a)=lower(?) OR lower(tech_b)=lower(?)",
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
    """Total distinct listings mentioning a tech (summed from the demand mart)."""
    rows = _query(
        f"SELECT sum(CAST(listing_count AS INTEGER)) AS n "
        f"FROM {_T_DEMAND} WHERE lower(technology_name)=lower(?) "
        f"AND week_start > (SELECT min(week_start) FROM {_T_DEMAND})",
        [tech],
    )
    if rows and rows[0].get("n") is not None:
        return int(rows[0]["n"])
    return 0


def market_trend(weeks: int = 8) -> list[dict]:
    """Recent daily market-trend rows (most recent ``weeks`` weeks), oldest first."""
    rows = _query(
        f"SELECT full_date, "
        f"CAST(new_listings AS INTEGER) AS new_listings, "
        f"CAST(rolling_7d_listings AS INTEGER) AS rolling_7d_listings, "
        f"CAST(rolling_7d_avg_salary AS DOUBLE) AS rolling_7d_avg_salary "
        f"FROM {_T_TRENDS} "
        f"WHERE full_date > (SELECT min(full_date) FROM {_T_TRENDS}) "
        f"ORDER BY full_date DESC LIMIT ?",
        [weeks * 7],
    )
    return list(reversed(rows))


def tech_demand_trend(tech: str, limit: int = 12) -> list[dict]:
    """Weekly demand (listing_count + WoW change) for a tech, oldest first."""
    rows = _query(
        f"SELECT week_start, "
        f"CAST(listing_count AS INTEGER) AS listing_count, "
        f"CAST(wow_change AS INTEGER) AS wow_change "
        f"FROM {_T_DEMAND} WHERE lower(technology_name)=lower(?) "
        f"AND week_start > (SELECT min(week_start) FROM {_T_DEMAND}) "
        f"ORDER BY week_start DESC LIMIT ?",
        [tech, limit],
    )
    return list(reversed(rows))


def company_intel(company: str) -> dict | None:
    """Listing intel for a company from the all-seniorities snapshot mart."""
    rows = _query(
        f"SELECT title, salary_min, salary_max, currency, seniority, workplace_type "
        f"FROM {_T_SNAPSHOT} WHERE lower(company_name) LIKE lower(?)",
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
        f"SELECT company_name, count(*) AS listing_count "
        f"FROM {_T_SNAPSHOT} WHERE company_name IS NOT NULL "
        f"GROUP BY company_name ORDER BY listing_count DESC LIMIT ?",
        [limit],
    )


def hot_technologies(limit: int = 10) -> list[dict]:
    """Technologies with the biggest recent week-over-week demand increase."""
    return _query(
        f"SELECT technology_name, "
        f"CAST(listing_count AS INTEGER) AS listing_count, "
        f"CAST(wow_change AS INTEGER) AS wow_change "
        f"FROM {_T_DEMAND} "
        f"WHERE week_start = (SELECT max(week_start) FROM {_T_DEMAND}) "
        f"ORDER BY wow_change DESC LIMIT ?",
        [limit],
    )


def listing_meta(listing_id: str) -> dict | None:
    """Look up display metadata for a single listing by its id.

    Reads the current market snapshot mart and returns ``{title, company, url}``
    (url built from the listing's slug), or ``None`` if the id isn't present —
    e.g. the offer has aged out of the snapshot / been de-listed at the source.

    Used to hydrate tracker rows whose metadata wasn't captured at button-press
    time (bot restart or LRU eviction between showing a listing and tracking it),
    so the tracker shows the offer title/link instead of a raw id.
    """
    from urllib.parse import quote

    lid = str(listing_id or "").strip()
    if not lid:
        return None
    rows = _query(
        f"SELECT title, company_name, slug FROM {_T_SNAPSHOT} "  # noqa: S608
        "WHERE CAST(listing_id AS VARCHAR) = ? LIMIT 1",
        [lid],
    )
    if not rows:
        return None
    r = rows[0]
    slug = r.get("slug") or ""
    url = f"https://justjoin.it/offers/{quote(str(slug), safe='')}" if slug else None
    return {
        "title": r.get("title"),
        "company": r.get("company_name"),
        "url": url,
    }


def rank_listings_by_skills(listings: list[dict], skills: list[str]) -> list[dict]:
    """Rank listings by percent overlap with the user's skill set.

    match_pct = what % of the LISTING's required techs does the user have.
    Adds ``match_pct`` and ``matched_skills`` to a copy of each listing.
    """
    if not skills:
        return listings
    wanted = {s.strip().lower() for s in skills if s.strip()}
    if not wanted:
        return listings

    ranked = []
    for listing in listings:
        listing_techs = _extract_all_techs(listing)
        if not listing_techs:
            pct = 0
            matched = set()
        else:
            matched = wanted & listing_techs
            pct = round(100 * len(matched) / len(listing_techs))
        item = dict(listing)
        item["match_pct"] = pct
        item["matched_skills"] = sorted(matched)
        ranked.append(item)
    ranked.sort(key=lambda x: x["match_pct"], reverse=True)
    return ranked


def _extract_all_techs(listing: dict) -> set[str]:
    """Extract all technology/skill names from a listing (lowercased)."""
    techs: set[str] = set()
    for key in ("technologies", "required_skills", "nice_to_have_skills"):
        val = listing.get(key, [])
        if isinstance(val, str):
            techs.update(t.strip().lower() for t in val.split(",") if t.strip())
        elif isinstance(val, list):
            techs.update(str(t).strip().lower() for t in val if t)
    return techs
