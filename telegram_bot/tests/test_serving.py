"""Tests for the DuckDB serving layer (query helpers + skill ranking)."""

import importlib

import pytest


@pytest.fixture
def serving(tmp_path, monkeypatch):
    """Load serving.py pointed at a temp DuckDB file, seeded with sample marts."""
    monkeypatch.setenv("SERVING_DB_PATH", str(tmp_path / "serving.duckdb"))
    import telegram_bot.serving as serving_mod

    serving_mod = importlib.reload(serving_mod)

    duckdb = pytest.importorskip("duckdb")
    con = duckdb.connect(str(tmp_path / "serving.duckdb"))

    # All columns VARCHAR to mirror how sync_marts stores them.
    con.execute(
        "CREATE TABLE salary_by_technology AS SELECT * FROM (VALUES "
        "('Python','junior','b2b','PLN','10','8000','12000','10000','9500','8000','11000','6000','15000'),"
        "('Python','senior','b2b','PLN','20','18000','26000','22000','21000','19000','24000','15000','30000')"
        ") AS t(technology_name,seniority,employment_type,currency,listing_count,"
        "avg_salary_min,avg_salary_max,avg_salary_mid,median_salary,p25_salary,p75_salary,"
        "min_salary,max_salary)"
    )
    con.execute(
        "CREATE TABLE demand_by_technology AS SELECT * FROM (VALUES "
        "('Python','2026','30','2026-07-20','30','25','5'),"
        "('Python','2026','31','2026-07-27','40','30','10'),"
        "('SQL','2026','31','2026-07-27','22','20','2')"
        ") AS t(technology_name,year,week_of_year,week_start,listing_count,prev_week_count,wow_change)"
    )
    con.execute(
        "CREATE TABLE tech_co_occurrence AS SELECT * FROM (VALUES "
        "('Python','SQL','50'),"
        "('Airflow','Python','30'),"
        "('Java','Spring','40')"
        ") AS t(tech_a,tech_b,co_occurrence_count)"
    )
    con.execute(
        "CREATE TABLE market_trends AS SELECT * FROM (VALUES "
        "('2026-08-01','31','8','2026','12','20000','60','19500'),"
        "('2026-08-02','31','8','2026','15','21000','70','20000')"
        ") AS t(full_date,week_of_year,month,year,new_listings,avg_salary_mid,"
        "rolling_7d_listings,rolling_7d_avg_salary)"
    )
    con.execute(
        "CREATE TABLE junior_snapshot AS SELECT * FROM (VALUES "
        "('l1','Junior Python Dev','Acme','jr-py','junior','b2b','remote','python','7000','9000','PLN','2026-08-01','Python, SQL','Warszawa'),"
        "('l2','Junior Data Analyst','Acme','jr-da','junior','b2b','hybrid','data','6000','8000','PLN','2026-08-01','SQL, Excel','Kraków')"
        ") AS t(listing_id,title,company_name,slug,seniority,employment_type,workplace_type,"
        "category,salary_min,salary_max,currency,posted_date,technologies,cities)"
    )
    con.execute("CREATE TABLE _sync_meta (synced_at DOUBLE, tables VARCHAR)")
    con.execute("INSERT INTO _sync_meta VALUES (2000000000.0, 'seeded')")
    con.close()

    return serving_mod


def test_is_ready(serving):
    assert serving.is_ready() is True


def test_salary_for_tech(serving):
    stats = serving.salary_for_tech("python")
    assert stats is not None
    assert stats["listing_count"] == 30  # 10 + 20
    assert stats["min"] == 6000
    assert stats["max"] == 30000
    # Weighted average mid: (10000*10 + 22000*20) / 30 = 18000
    assert stats["avg_mid"] == 18000


def test_salary_for_tech_seniority_filter(serving):
    stats = serving.salary_for_tech("Python", "senior")
    assert stats["listing_count"] == 20
    assert stats["avg_mid"] == 22000


def test_salary_for_tech_unknown(serving):
    assert serving.salary_for_tech("cobol") is None


def test_salary_by_seniority_order(serving):
    rows = serving.salary_by_seniority("python")
    seniorities = [r["seniority"] for r in rows]
    assert seniorities == ["junior", "senior"]  # sorted by rank


def test_skills_for_tech(serving):
    data = serving.skills_for_tech("Python")
    assert data is not None
    names = {r["tech"] for r in data["related"]}
    assert names == {"SQL", "Airflow"}
    # total python listings = 30 + 40 = 70; SQL co-occ = 50 -> 71%
    sql_row = next(r for r in data["related"] if r["tech"] == "SQL")
    assert sql_row["count"] == 50
    assert sql_row["pct"] == 71  # round(100*50/70)


def test_market_trend(serving):
    rows = serving.market_trend(weeks=8)
    assert len(rows) == 2
    assert rows[0]["full_date"] == "2026-08-01"  # oldest first
    assert rows[-1]["rolling_7d_listings"] == 70


def test_tech_demand_trend(serving):
    rows = serving.tech_demand_trend("Python")
    assert [r["listing_count"] for r in rows] == [30, 40]  # oldest first
    assert rows[-1]["wow_change"] == 10


def test_company_intel(serving):
    data = serving.company_intel("acme")
    assert data["listing_count"] == 2
    assert data["avg_min"] == 6500  # (7000+6000)/2
    assert len(data["sample_titles"]) == 2


def test_top_hiring_companies(serving):
    rows = serving.top_hiring_companies()
    assert rows[0]["company_name"] == "Acme"
    assert rows[0]["listing_count"] == 2


def test_hot_technologies(serving):
    rows = serving.hot_technologies()
    # latest week is 2026-07-27; Python wow=10 should rank above SQL wow=2
    assert rows[0]["technology_name"] == "Python"


def test_rank_listings_by_skills(serving):
    listings = [
        {"listing_id": "a", "technologies": ["Python", "Django"]},
        {"listing_id": "b", "technologies": ["Python", "SQL", "Airflow"]},
        {"listing_id": "c", "technologies": ["Java"]},
    ]
    ranked = serving.rank_listings_by_skills(listings, ["python", "sql", "airflow"])
    assert ranked[0]["listing_id"] == "b"  # 100% match
    assert ranked[0]["match_pct"] == 100
    assert ranked[-1]["listing_id"] == "c"  # 0% match


def test_rank_listings_handles_string_techs(serving):
    listings = [{"listing_id": "a", "technologies": "Python, SQL"}]
    ranked = serving.rank_listings_by_skills(listings, ["python"])
    assert ranked[0]["match_pct"] == 100
