"""Tests for the shared tolerance-based matching logic in telegram_bot.filters."""

from telegram_bot.filters import filter_listings, match_listing


def _cfg(**overrides) -> dict:
    """A config with all dimensions inactive (empty) plus tolerance=0 by default."""
    base = {
        "seniorities": [],
        "technologies": [],
        "categories": [],
        "workplace_types": [],
        "employment_types": [],
        "salary_min": 0,
        "cities": [],
        "tolerance": 0,
    }
    base.update(overrides)
    return base


def test_no_active_filters_matches_everything():
    matches, mismatches = match_listing({"title": "anything"}, _cfg())
    assert matches is True
    assert mismatches == 0


def test_seniority_strict_mismatch():
    listing = {"seniority": "senior"}
    matches, mismatches = match_listing(listing, _cfg(seniorities=["junior"], tolerance=0))
    assert matches is False
    assert mismatches == 1


def test_seniority_match_is_case_insensitive():
    listing = {"seniority": "Junior"}
    matches, _ = match_listing(listing, _cfg(seniorities=["junior"]))
    assert matches is True


def test_tolerance_allows_one_mismatch():
    # Wants junior + Python, listing is senior (1 mismatch) but has Python.
    listing = {"seniority": "senior", "technologies": ["Python"]}
    cfg = _cfg(seniorities=["junior"], technologies=["Python"], tolerance=1)
    matches, mismatches = match_listing(listing, cfg)
    assert mismatches == 1
    assert matches is True


def test_tolerance_zero_rejects_same_listing():
    listing = {"seniority": "senior", "technologies": ["Python"]}
    cfg = _cfg(seniorities=["junior"], technologies=["Python"], tolerance=0)
    matches, mismatches = match_listing(listing, cfg)
    assert mismatches == 1
    assert matches is False


def test_technology_intersection_case_insensitive():
    listing = {"technologies": ["python", "docker"]}
    matches, _ = match_listing(listing, _cfg(technologies=["Python"]))
    assert matches is True

    no_match, mm = match_listing({"technologies": ["Go"]}, _cfg(technologies=["Python"]))
    assert no_match is False
    assert mm == 1


def test_technology_pulls_from_skill_fields():
    listing = {"required_skills": ["Python"], "nice_to_have_skills": ["AWS"]}
    matches, _ = match_listing(listing, _cfg(technologies=["aws"]))
    assert matches is True


def test_salary_undisclosed_is_benefit_of_doubt():
    # No salary info -> should NOT count as a mismatch.
    matches, mismatches = match_listing({}, _cfg(salary_min=10000, tolerance=0))
    assert mismatches == 0
    assert matches is True


def test_salary_below_minimum_mismatches():
    listing = {"salary_max": 5000}
    matches, mismatches = match_listing(listing, _cfg(salary_min=10000, tolerance=0))
    assert mismatches == 1
    assert matches is False


def test_salary_from_variants():
    listing = {"salary_variants": [{"salary_max": 20000}, {"salary_max": 8000}]}
    # Max across variants is 20000 -> meets a 10000 minimum.
    matches, mismatches = match_listing(listing, _cfg(salary_min=10000, tolerance=0))
    assert mismatches == 0
    assert matches is True


def test_employment_from_variants():
    listing = {"salary_variants": [{"employment_type": "b2b"}]}
    matches, _ = match_listing(listing, _cfg(employment_types=["b2b"]))
    assert matches is True

    no_match, mm = match_listing(listing, _cfg(employment_types=["permanent"]))
    assert no_match is False
    assert mm == 1


def test_cities_accepts_string_or_list():
    cfg = _cfg(cities=["Warszawa"])
    assert match_listing({"cities": "Warszawa"}, cfg)[0] is True
    assert match_listing({"cities": ["Kraków", "Warszawa"]}, cfg)[0] is True
    assert match_listing({"cities": ["Gdańsk"]}, cfg)[0] is False


def test_filter_listings_returns_only_matches():
    listings = [
        {"seniority": "junior"},
        {"seniority": "senior"},
        {"seniority": "junior"},
    ]
    result = filter_listings(listings, _cfg(seniorities=["junior"], tolerance=0))
    assert len(result) == 2
    assert all(item["seniority"] == "junior" for item in result)
