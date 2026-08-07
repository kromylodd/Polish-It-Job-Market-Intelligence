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
        assert result["listing_id"] == "test-listing-001"
        assert result["title"] == "Junior Data Engineer"
        assert result["company_name"] == "Example Corp"
        assert result["category"] == "Data"
        assert result["seniority"] == "junior"
        assert result["workplace_type"] == "remote"

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
        assert result["cities"] == ["Warszawa", "Kraków"]

    def test_salary_variants(self, sample_listing, run_id):
        result = parse_listing(sample_listing, run_id)
        variants = result["salary_variants"]

        assert len(variants) == 2
        b2b = variants[0]
        assert b2b["employment_type"] == "b2b"
        assert b2b["salary_min"] == 8000
        assert b2b["salary_max"] == 14000
        assert b2b["currency"] == "PLN"

    def test_skills_extraction(self, sample_listing, run_id):
        result = parse_listing(sample_listing, run_id)
        assert "Python" in result["required_skills"]
        assert "SQL" in result["required_skills"]
        assert "dbt" in result["nice_to_have_skills"]


class TestExtractCities:
    def test_multilocation(self):
        raw = {"multilocation": [{"city": "Gdańsk"}, {"city": "Wrocław"}]}
        assert _extract_cities(raw) == ["Gdańsk", "Wrocław"]

    def test_single_city(self):
        raw = {"city": "Poznań"}
        assert _extract_cities(raw) == ["Poznań"]

    def test_empty(self):
        assert _extract_cities({}) == []

    def test_locations_format(self):
        raw = {"locations": [{"city": "Łódź"}, {"city": "Katowice"}]}
        assert _extract_cities(raw) == ["Łódź", "Katowice"]


class TestExtractSalaryVariants:
    def test_employment_types_format(self):
        raw = {
            "employmentTypes": [
                {"type": "b2b", "salary": {"from": 10000, "to": 15000, "currency": "PLN", "isGross": False}},
            ]
        }
        variants = _extract_salary_variants(raw)
        assert len(variants) == 1
        assert variants[0]["salary_min"] == 10000
        assert variants[0]["salary_max"] == 15000

    def test_no_salary(self):
        raw = {}
        assert _extract_salary_variants(raw) == []


class TestExtractSkills:
    def test_dict_format(self):
        raw = {"requiredSkills": [{"name": "Python"}, {"name": "SQL"}]}
        result = _extract_skills(raw, "requiredSkills")
        assert result == ["Python", "SQL"]

    def test_string_format(self):
        raw = {"skills": ["Python", "SQL"]}
        result = _extract_skills(raw, "skills")
        assert result == ["Python", "SQL"]

    def test_fallback_keys(self):
        raw = {"skills": ["Terraform"]}
        result = _extract_skills(raw, "requiredSkills", "skills")
        assert result == ["Terraform"]


class TestExtractCompanyName:
    def test_flat_field(self):
        assert _extract_company_name({"companyName": "Acme"}) == "Acme"

    def test_nested_company(self):
        assert _extract_company_name({"company": {"name": "Acme"}}) == "Acme"

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
