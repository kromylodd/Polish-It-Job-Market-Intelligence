"""Unit tests for the justjoin.it listing parser."""

import json
from pathlib import Path

import pytest

from scraper.parser import (
    parse_listing,
    parse_all_listings,
    _extract_cities,
    _extract_salary_variants,
    _extract_skills,
    _extract_company_name,
    _extract_category,
)

FIXTURES_DIR = Path(__file__).parent
SAMPLE_LISTING_PATH = FIXTURES_DIR / "sample_raw_listing.json"


@pytest.fixture
def sample_listing() -> dict:
    """Load the sample raw listing fixture."""
    with open(SAMPLE_LISTING_PATH) as f:
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
        assert result["working_time"] == "full_time"

    def test_metadata_fields(self, sample_listing, run_id):
        result = parse_listing(sample_listing, run_id)

        assert result["source_run_id"] == run_id
        assert result["date_collected"]  # non-empty ISO timestamp

    def test_returns_none_for_missing_id(self, run_id):
        raw = {"title": "No ID listing"}
        result = parse_listing(raw, run_id)
        assert result is None

    def test_cities_extraction(self, sample_listing, run_id):
        result = parse_listing(sample_listing, run_id)
        assert set(result["cities"]) == {"Warszawa", "Kraków"}

    def test_salary_variants_only_original_currency(self, sample_listing, run_id):
        result = parse_listing(sample_listing, run_id)
        variants = result["salary_variants"]

        # Should only have PLN (original), not USD (conversion)
        assert len(variants) == 2
        assert all(v["currency"] == "PLN" for v in variants)

    def test_salary_values(self, sample_listing, run_id):
        result = parse_listing(sample_listing, run_id)
        variants = result["salary_variants"]

        b2b = next(v for v in variants if v["employment_type"] == "b2b")
        assert b2b["salary_min"] == 8000.0
        assert b2b["salary_max"] == 14000.0
        assert b2b["currency"] == "PLN"
        assert b2b["is_gross"] is False

        perm = next(v for v in variants if v["employment_type"] == "permanent")
        assert perm["salary_min"] == 6500.0
        assert perm["salary_max"] == 11000.0
        assert perm["is_gross"] is True

    def test_skills_extraction(self, sample_listing, run_id):
        result = parse_listing(sample_listing, run_id)
        assert "Python" in result["required_skills"]
        assert "SQL" in result["required_skills"]
        assert "Apache Spark" in result["required_skills"]
        assert "dbt" in result["nice_to_have_skills"]
        assert "Airflow" in result["nice_to_have_skills"]

    def test_flags(self, sample_listing, run_id):
        result = parse_listing(sample_listing, run_id)
        assert result["is_promoted"] is False
        assert result["is_super_offer"] is False
        assert result["is_remote_interview"] is True

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

    def test_deduplication(self):
        raw = {"locations": [{"city": "Warszawa"}, {"city": "Warszawa"}]}
        assert _extract_cities(raw) == ["Warszawa"]


class TestExtractSalaryVariants:
    def test_filters_to_original_currency_only(self):
        raw = {
            "employmentTypes": [
                {"from": 10000, "fromPerUnit": 10000, "to": 15000, "toPerUnit": 15000, "currency": "PLN", "currencySource": "original", "type": "b2b", "unit": "Month", "gross": False},
                {"from": 2500, "fromPerUnit": 2500, "to": 3750, "toPerUnit": 3750, "currency": "USD", "currencySource": "conversion", "type": "b2b", "unit": "Month", "gross": False},
            ]
        }
        variants = _extract_salary_variants(raw)
        assert len(variants) == 1
        assert variants[0]["currency"] == "PLN"
        assert variants[0]["salary_min"] == 10000

    def test_skips_null_salary(self):
        raw = {
            "employmentTypes": [
                {"from": None, "fromPerUnit": None, "to": None, "toPerUnit": None, "currency": "PLN", "currencySource": "original", "type": "permanent", "unit": "month", "gross": True},
            ]
        }
        variants = _extract_salary_variants(raw)
        assert variants == []

    def test_empty_employment_types(self):
        raw = {"employmentTypes": []}
        assert _extract_salary_variants(raw) == []

    def test_no_employment_types(self):
        raw = {}
        assert _extract_salary_variants(raw) == []

    def test_uses_per_unit_values(self):
        """For hourly rates, fromPerUnit/toPerUnit has the per-unit rate."""
        raw = {
            "employmentTypes": [
                {"from": 27720, "fromPerUnit": 165.0, "to": 31920, "toPerUnit": 190.0, "currency": "PLN", "currencySource": "original", "type": "b2b", "unit": "Hour", "gross": False},
            ]
        }
        variants = _extract_salary_variants(raw)
        assert variants[0]["salary_min"] == 165.0
        assert variants[0]["salary_max"] == 190.0
        assert variants[0]["unit"] == "Hour"


class TestExtractSkills:
    def test_dict_format_with_levels(self):
        raw = {"requiredSkills": [{"name": "Python", "level": 4}, {"name": "SQL", "level": 3}]}
        result = _extract_skills(raw, "requiredSkills")
        assert result == ["Python", "SQL"]

    def test_empty_skills(self):
        raw = {"requiredSkills": []}
        assert _extract_skills(raw, "requiredSkills") == []

    def test_missing_key(self):
        raw = {}
        assert _extract_skills(raw, "requiredSkills") == []


class TestExtractCategory:
    def test_dict_format(self):
        raw = {"category": {"key": "data", "parentKey": None}}
        assert _extract_category(raw) == "data"

    def test_string_format(self):
        raw = {"category": "python"}
        assert _extract_category(raw) == "python"

    def test_missing(self):
        assert _extract_category({}) == ""


class TestExtractCompanyName:
    def test_flat_field(self):
        assert _extract_company_name({"companyName": "Acme"}) == "Acme"

    def test_missing(self):
        assert _extract_company_name({}) == ""


class TestParseAllListings:
    def test_batch_parse(self, sample_listing):
        listings = [sample_listing, sample_listing]
        results = parse_all_listings(listings, run_id="batch_001")
        assert len(results) == 2

    def test_skips_invalid(self, sample_listing):
        listings = [sample_listing, {"no_id": True}, sample_listing]
        results = parse_all_listings(listings, run_id="batch_002")
        assert len(results) == 2

    def test_generates_run_id_if_none(self, sample_listing):
        results = parse_all_listings([sample_listing])
        assert len(results) == 1
        assert results[0]["source_run_id"]  # non-empty
