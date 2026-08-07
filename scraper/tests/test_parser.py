"""Tests for justjoin.it listing parser."""

import json
from pathlib import Path

import pytest

from scraper.parser import (
    _extract_category,
    _extract_cities,
    _extract_description,
    _extract_salary_variants,
    _extract_skills,
    parse_all_listings,
    parse_listing,
)

FIXTURES_DIR = Path(__file__).parent
SAMPLE_PATH = FIXTURES_DIR / "sample_raw_listing.json"


@pytest.fixture
def sample_listing() -> dict:
    with open(SAMPLE_PATH) as f:
        return json.load(f)


@pytest.fixture
def run_id() -> str:
    return "test_run_001"


class TestParseListing:
    def test_basic_fields(self, sample_listing, run_id):
        result = parse_listing(sample_listing, run_id)
        assert result is not None
        assert result["listing_id"] == "6bb15506-aca9-40b8-85d6-c666d61aa7a8"
        assert result["title"] == "Junior Data Engineer"
        assert result["company_name"] == "Example Corp"
        assert result["category"] == "data"
        assert result["seniority"] == "junior"
        assert result["workplace_type"] == "remote"

    def test_metadata(self, sample_listing, run_id):
        result = parse_listing(sample_listing, run_id)
        assert result["source_run_id"] == run_id
        assert result["date_collected"]

    def test_missing_id_returns_none(self, run_id):
        assert parse_listing({"title": "No ID"}, run_id) is None

    def test_cities(self, sample_listing, run_id):
        result = parse_listing(sample_listing, run_id)
        assert set(result["cities"]) == {"Warszawa", "Kraków"}

    def test_salary_filters_to_original_currency(self, sample_listing, run_id):
        result = parse_listing(sample_listing, run_id)
        variants = result["salary_variants"]
        assert len(variants) == 2
        assert all(v["currency"] == "PLN" for v in variants)

    def test_salary_values(self, sample_listing, run_id):
        result = parse_listing(sample_listing, run_id)
        variants = result["salary_variants"]
        b2b = next(v for v in variants if v["employment_type"] == "b2b")
        assert b2b["salary_min"] == 8000.0
        assert b2b["salary_max"] == 14000.0
        assert b2b["is_gross"] is False
        perm = next(v for v in variants if v["employment_type"] == "permanent")
        assert perm["salary_min"] == 6500.0
        assert perm["salary_max"] == 11000.0
        assert perm["is_gross"] is True

    def test_skills(self, sample_listing, run_id):
        result = parse_listing(sample_listing, run_id)
        assert "Python" in result["required_skills"]
        assert "SQL" in result["required_skills"]
        assert "dbt" in result["nice_to_have_skills"]

    def test_dates(self, sample_listing, run_id):
        result = parse_listing(sample_listing, run_id)
        assert result["posted_date"] == "2026-08-06T10:00:00.0000000Z"
        assert result["expiry_date"] == "2026-09-06T22:59:59.9999999Z"


class TestExtractCities:
    def test_locations_array(self):
        raw = {"locations": [{"city": "Gdańsk"}, {"city": "Wrocław"}]}
        assert set(_extract_cities(raw)) == {"Gdańsk", "Wrocław"}

    def test_fallback_to_city_field(self):
        raw = {"city": "Poznań", "locations": []}
        assert _extract_cities(raw) == ["Poznań"]

    def test_empty(self):
        assert _extract_cities({}) == []

    def test_dedup(self):
        raw = {"locations": [{"city": "Warszawa"}, {"city": "Warszawa"}]}
        assert _extract_cities(raw) == ["Warszawa"]


class TestExtractSalaryVariants:
    def test_filters_conversion_currency(self):
        raw = {
            "employmentTypes": [
                {
                    "from": 10000,
                    "fromPerUnit": 10000,
                    "to": 15000,
                    "toPerUnit": 15000,
                    "currency": "PLN",
                    "currencySource": "original",
                    "type": "b2b",
                    "unit": "Month",
                    "gross": False,
                },
                {
                    "from": 2500,
                    "fromPerUnit": 2500,
                    "to": 3750,
                    "toPerUnit": 3750,
                    "currency": "USD",
                    "currencySource": "conversion",
                    "type": "b2b",
                    "unit": "Month",
                    "gross": False,
                },
            ]
        }
        variants = _extract_salary_variants(raw)
        assert len(variants) == 1
        assert variants[0]["currency"] == "PLN"

    def test_skips_null_salary(self):
        raw = {
            "employmentTypes": [
                {
                    "from": None,
                    "fromPerUnit": None,
                    "to": None,
                    "toPerUnit": None,
                    "currency": "PLN",
                    "currencySource": "original",
                    "type": "permanent",
                    "unit": "month",
                    "gross": True,
                },
            ]
        }
        assert _extract_salary_variants(raw) == []

    def test_empty(self):
        assert _extract_salary_variants({}) == []

    def test_per_unit_values(self):
        raw = {
            "employmentTypes": [
                {
                    "from": 27720,
                    "fromPerUnit": 165.0,
                    "to": 31920,
                    "toPerUnit": 190.0,
                    "currency": "PLN",
                    "currencySource": "original",
                    "type": "b2b",
                    "unit": "Hour",
                    "gross": False,
                },
            ]
        }
        variants = _extract_salary_variants(raw)
        assert variants[0]["salary_min"] == 165.0
        assert variants[0]["salary_max"] == 190.0


class TestExtractSkills:
    def test_with_levels(self):
        raw = {"requiredSkills": [{"name": "Python", "level": 4}, {"name": "SQL", "level": 3}]}
        assert _extract_skills(raw, "requiredSkills") == ["Python", "SQL"]

    def test_empty(self):
        assert _extract_skills({"requiredSkills": []}, "requiredSkills") == []

    def test_missing_key(self):
        assert _extract_skills({}, "requiredSkills") == []


class TestExtractDescription:
    def test_description_key(self):
        assert _extract_description({"description": "We use Python and Spark."}) == (
            "We use Python and Spark."
        )

    def test_body_fallback(self):
        assert _extract_description({"body": "Airflow + dbt stack"}) == "Airflow + dbt stack"

    def test_missing_returns_empty(self):
        assert _extract_description({}) == ""

    def test_blank_returns_empty(self):
        assert _extract_description({"description": "   "}) == ""


class TestExtractCategory:
    def test_dict(self):
        assert _extract_category({"category": {"key": "data", "parentKey": None}}) == "data"

    def test_string(self):
        assert _extract_category({"category": "python"}) == "python"

    def test_missing(self):
        assert _extract_category({}) == ""


class TestParseAllListings:
    def test_batch(self, sample_listing):
        results = parse_all_listings([sample_listing, sample_listing], run_id="batch")
        assert len(results) == 2

    def test_skips_invalid(self, sample_listing):
        results = parse_all_listings([sample_listing, {}, sample_listing], run_id="batch")
        assert len(results) == 2

    def test_generates_run_id(self, sample_listing):
        results = parse_all_listings([sample_listing])
        assert results[0]["source_run_id"]
