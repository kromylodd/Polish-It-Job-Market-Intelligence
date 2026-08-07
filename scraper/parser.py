"""
Parser for justjoin.it raw listing data.

Flattens and normalizes the raw RSC payload into a consistent schema
suitable for upload to the bronze layer.

Fields extracted per listing (from plan):
- listing_id, slug, title, apply_url
- company_name
- category (Backend, Frontend, Data, DevOps, etc.)
- seniority (Junior, Mid, Senior, Expert)
- employment_types (B2B, UoP, mandate) with salary per type
- workplace_type (remote, hybrid, onsite)
- cities (list — a listing can map to multiple cities)
- salary_min, salary_max, currency, is_gross
- required_skills, nice_to_have_skills (structured tags)
- description (free text — for supplementary tech extraction)
- posted_date, expiry_date
- date_collected, source_run_id (added at collection time)
"""

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


def parse_listing(raw: dict[str, Any], run_id: str) -> dict[str, Any] | None:
    """
    Parse a single raw listing into a normalized schema.

    Args:
        raw: Raw listing dictionary from RSC payload.
        run_id: Unique identifier for this scrape run.

    Returns:
        Normalized listing dict, or None if the listing is unparseable.
    """
    try:
        # TODO: Map actual field names after inspecting live RSC payload
        # The structure below is based on documented justjoin.it data model
        # fields — actual key names need verification against a real response.

        listing_id = raw.get("id") or raw.get("slug")
        if not listing_id:
            logger.warning("Listing missing id/slug — skipping")
            return None

        parsed = {
            # Identifiers
            "listing_id": str(listing_id),
            "slug": raw.get("slug", ""),
            "title": raw.get("title", ""),
            "apply_url": raw.get("applyUrl", raw.get("apply_url", "")),
            # Company
            "company_name": _extract_company_name(raw),
            # Classification
            "category": raw.get("category", raw.get("marker_icon", "")),
            "seniority": raw.get("experienceLevel", raw.get("experience_level", "")),
            "workplace_type": raw.get("workplaceType", raw.get("workplace_type", "")),
            # Location (can be multiple cities)
            "cities": _extract_cities(raw),
            # Salary (multiple variants possible per listing)
            "salary_variants": _extract_salary_variants(raw),
            # Skills (structured tags)
            "required_skills": _extract_skills(raw, "requiredSkills", "skills"),
            "nice_to_have_skills": _extract_skills(raw, "niceToHaveSkills", "nice_to_have_skills"),
            # Description (free text for supplementary tech extraction)
            "description": raw.get("body", raw.get("description", "")),
            # Dates
            "posted_date": raw.get("publishedAt", raw.get("published_at", "")),
            "expiry_date": raw.get("expiresAt", raw.get("expires_at", "")),
            # Metadata (added at collection time)
            "date_collected": datetime.now(timezone.utc).isoformat(),
            "source_run_id": run_id,
        }

        return parsed

    except Exception as e:
        listing_id = raw.get("id", raw.get("slug", "unknown"))
        logger.error(f"Failed to parse listing {listing_id}: {e}")
        return None


def _extract_company_name(raw: dict[str, Any]) -> str:
    """Extract company name from nested or flat structure."""
    if "companyName" in raw:
        return raw["companyName"]
    if "company_name" in raw:
        return raw["company_name"]
    company = raw.get("company", {})
    if isinstance(company, dict):
        return company.get("name", "")
    return ""


def _extract_cities(raw: dict[str, Any]) -> list[str]:
    """Extract city list from listing (a listing can map to multiple cities)."""
    # Multiple possible structures — check after live inspection
    if "multilocation" in raw and raw["multilocation"]:
        return [loc.get("city", "") for loc in raw["multilocation"] if loc.get("city")]
    if "city" in raw:
        return [raw["city"]] if raw["city"] else []
    if "locations" in raw:
        return [loc.get("city", "") for loc in raw["locations"] if loc.get("city")]
    return []


def _extract_salary_variants(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Extract salary information. justjoin.it often has multiple salary
    variants per listing (e.g., B2B rate and UoP rate on the same posting).
    """
    variants = []

    employment_types = raw.get("employmentTypes", raw.get("employment_types", []))
    if not employment_types:
        # Single salary structure fallback
        salary = raw.get("salary", {})
        if salary:
            variants.append({
                "employment_type": raw.get("employment_type", ""),
                "salary_min": salary.get("from", salary.get("min")),
                "salary_max": salary.get("to", salary.get("max")),
                "currency": salary.get("currency", "PLN"),
                "is_gross": salary.get("isGross", salary.get("is_gross", True)),
            })
        return variants

    for emp_type in employment_types:
        salary = emp_type.get("salary", {})
        if salary:
            variants.append({
                "employment_type": emp_type.get("type", ""),
                "salary_min": salary.get("from", salary.get("min")),
                "salary_max": salary.get("to", salary.get("max")),
                "currency": salary.get("currency", "PLN"),
                "is_gross": salary.get("isGross", salary.get("is_gross", True)),
            })

    return variants


def _extract_skills(raw: dict[str, Any], *keys: str) -> list[str]:
    """Extract skill tags from one of several possible field names."""
    for key in keys:
        value = raw.get(key)
        if value:
            if isinstance(value, list):
                # Could be list of strings or list of dicts
                return [
                    s.get("name", s) if isinstance(s, dict) else str(s)
                    for s in value
                ]
    return []


def parse_all_listings(
    raw_listings: list[dict[str, Any]],
    run_id: str | None = None,
) -> list[dict[str, Any]]:
    """
    Parse all raw listings into normalized format.

    Args:
        raw_listings: List of raw listing dicts from scraper.
        run_id: Optional run ID. Generated if not provided.

    Returns:
        List of successfully parsed listings.
    """
    if run_id is None:
        run_id = hashlib.md5(
            datetime.now(timezone.utc).isoformat().encode()
        ).hexdigest()[:12]

    parsed = []
    failed = 0

    for raw in raw_listings:
        result = parse_listing(raw, run_id)
        if result is not None:
            parsed.append(result)
        else:
            failed += 1

    logger.info(f"Parsed {len(parsed)} listings successfully, {failed} failed")
    return parsed
