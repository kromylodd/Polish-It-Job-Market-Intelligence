"""Tests for the market report + chart builders."""

import importlib

import pytest


@pytest.fixture
def modules(tmp_path, monkeypatch):
    """Seed a DuckDB serving cache and return (serving, reports)."""
    monkeypatch.setenv("SERVING_DB_PATH", str(tmp_path / "serving.duckdb"))
    import telegram_bot.serving as serving_mod

    serving_mod = importlib.reload(serving_mod)
    import telegram_bot.reports as reports_mod

    reports_mod = importlib.reload(reports_mod)

    duckdb = pytest.importorskip("duckdb")
    con = duckdb.connect(str(tmp_path / "serving.duckdb"))
    con.execute(
        "CREATE TABLE market_trends AS SELECT * FROM (VALUES "
        "('2026-08-01','31','8','2026','12','20000','60','19500'),"
        "('2026-08-08','32','8','2026','15','21000','80','20500')"
        ") AS t(full_date,week_of_year,month,year,new_listings,avg_salary_mid,"
        "rolling_7d_listings,rolling_7d_avg_salary)"
    )
    con.execute(
        "CREATE TABLE market_snapshot AS SELECT * FROM (VALUES "
        "('l1','Junior Dev','Acme','s1','junior','b2b','remote','python','7000','9000','PLN','2026-08-01','Python','Warszawa'),"
        "('l2','Data Analyst','Acme','s2','junior','b2b','hybrid','data','6000','8000','PLN','2026-08-01','SQL','Kraków'),"
        "('l3','QA','Globex','s3','junior','b2b','office','testing','5000','7000','PLN','2026-08-01','Selenium','Gdańsk')"
        ") AS t(listing_id,title,company_name,slug,seniority,employment_type,workplace_type,"
        "category,salary_min,salary_max,currency,posted_date,technologies,cities)"
    )
    con.execute(
        "CREATE TABLE demand_by_technology AS SELECT * FROM (VALUES "
        "('Python','2026','32','2026-08-08','40','30','10'),"
        "('SQL','2026','32','2026-08-08','22','20','2')"
        ") AS t(technology_name,year,week_of_year,week_start,listing_count,prev_week_count,wow_change)"
    )
    con.execute("CREATE TABLE _sync_meta (synced_at DOUBLE, tables VARCHAR)")
    con.execute("INSERT INTO _sync_meta VALUES (2000000000.0, 'seeded')")
    con.close()

    return serving_mod, reports_mod


def test_market_report_ready(modules):
    _, reports = modules
    text = reports.build_market_report()
    assert "Weekly IT Market Report" in text
    assert "Acme" in text  # top hiring company
    assert "Python" in text  # hot technology


def test_market_report_not_ready(tmp_path, monkeypatch):
    monkeypatch.setenv("SERVING_DB_PATH", str(tmp_path / "empty.duckdb"))
    import telegram_bot.serving as serving_mod

    importlib.reload(serving_mod)
    import telegram_bot.reports as reports_mod

    reports_mod = importlib.reload(reports_mod)
    text = reports_mod.build_market_report()
    assert "isn't ready" in text


def test_trend_chart_returns_png(modules):
    pytest.importorskip("matplotlib")
    _, reports = modules
    png = reports.build_trend_chart()
    assert png is not None
    assert png[:8] == b"\x89PNG\r\n\x1a\n"  # PNG magic number


def test_tech_demand_chart_returns_png(modules):
    pytest.importorskip("matplotlib")
    _, reports = modules
    png = reports.build_tech_demand_chart("Python")
    assert png is not None
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_tech_demand_chart_unknown_tech(modules):
    _, reports = modules
    assert reports.build_tech_demand_chart("cobol") is None
