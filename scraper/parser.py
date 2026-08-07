"""
Parser for justjoin.it API response data.

Flattens and normalizes the raw API response into a consistent schema
suitable for upload to the bronze layer.

API response fields per listing (verified 2026-08-07):
- guid: unique listing ID
- slug: URL-friendly identifier
- title: job title
- companyName: company name
- category: {key, parentKey}
- experienceLevel: junior/mid/senior/manager
- workplaceType: remote/hybrid/office
- workingTime: full_time/part_time
- city: primary city
- locations: [{city, street, latitude, longitude, slug}]
- employmentTypes: [{from, to, currency, currencySource, type, unit, gross}]
- requiredSkills: [{name, level}]
- niceToHaveSkills: [{name, level}] (often empty)
- languages: [{code, level}]
- publishedAt: ISO timestamp
- lastPublishedAt: ISO timestamp
- expiredAt: ISO timestamp
- applyMethod: external/internal
- applyUrl: application URL
- isPromoted, isSuperOffer: boolean flags
- hybridWorkSchedule: null or schedule info
"""

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


def parse_listing(raw: dict[str, Any], run_id: str) -> dict[str, Any] | None:
    """
    Parse a single raw API listing into a normalized schema.

    Args:
        raw: Raw listing dictionary from the API.
        run_id: Unique identifier for this scrape run.

    Returns:
        Normalized listing dict, or None if the listing is unparseable.
    """
    try:
        listing_id = raw.get("guid") or raw.get("slug")
        if not listing_id:
            logger.warning("Listing missing guid/slug — skipping")
            return None

        parsed = {
            # Identifiers
            "listing_id": str(listing_id),
            "slug": raw.get("slug", ""),
            "title": raw.get("title", ""),
            "apply_url": raw.get("applyUrl", ""),
            "apply_method": raw.get("applyMethod", ""),
            # Company
            "company_name": _extract_company_name(raw),
            # Classification
            "category": _extract_category(raw),
            "seniority": raw.get("experienceLevel", ""),
            "workplace_type": raw.get("workplaceType", ""),
            "working_time": raw.get("workingTime", ""),
            # Location (can be multiple cities)
            "cities": _extract_cities(raw),
            # Salary (multiple variants possible per listing)
            "salary_variants": _extract_salary_variants(raw),
            # Skills (structured tags with levels)
            "required_skills": _extract_skills(raw, "requiredSkills"),
            "nice_to_have_skills": _extract_skills(raw, "niceToHaveSkills"),
            # Languages
            "languages": raw.get("languages", []),
            # Dates
            "posted_date": raw.get("publishedAt", ""),
            "last_published_date": raw.get("lastPublishedAt", ""),
            "expiry_date": raw.get("expiredAt", ""),
            # Flags
            "is_promoted": raw.get("isPromoted", False),
            "is_super_offer": raw.get("isSuperOffer", False),
            "is_remote_interview": raw.get("isRemoteInterview", False),
            # Metadata (added at collection time)
            "date_collected": datetime.now(timezone.utc).isoformat(),
            "source_run_id": run_id,
        }

        return parsed

    except Exception as e:
        listing_id = raw.get("guid", raw.get("slug", "unknown"))
        logger.error(f"Failed to parse listing {listing_id}: {e}")
        return None


def _extract_company_name(raw: dict[str, Any]) -> str:
    """Extract company name."""
    return raw.get("companyName", "")


def _extract_category(raw: dict[str, Any]) -> str:
    """Extract category key from nested or flat structure."""
    category = raw.get("category", "")
    if isinstance(category, dict):
        return category.get("key", "")
    return str(category) if category else ""


def _extract_cities(raw: dict[str, Any]) -> list[str]:
    """
    Extract city list from listing.
    A listing can map to multiple cities via the 'locations' array.
    """
    locations = raw.get("locations", [])
    if locations:
        return list({loc.get("city", "") for loc in locations if loc.get("city")})

    # Fallback to single city field
    city = raw.get("city", "")
    return [city] if city else []


def _extract_salary_variants(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Extract salary information from employmentTypes array.

    justjoin.it provides salary in multiple currencies (PLN + conversions).
    We keep only the original currency (currencySource == "original") and
    filter out null salary entries.
    """
    variants = []
    employment_types = raw.get("employmentTypes", [])

    for emp in employment_types:
        # Only keep original currency entries (not conversions)
        if emp.get("currencySource") != "original":
            continue

        salary_from = emp.get("from")
        salary_to = emp.get("to")

        # Skip entries with no salary disclosed
        if salary_from is None and salary_to is None:
            continue

        variants.append({
            "employment_type": emp.get("type", ""),
            "salary_min": emp.get("fromPerUnit") or emp.get("from"),
            "salary_max": emp.get("toPerUnit") or emp.get("to"),
            "currency": emp.get("currency", "PLN"),
            "unit": emp.get("unit", "month"),
            "is_gross": emp.get("gross", True),
        })

    return variants


def _extract_skills(raw: dict[str, Any], key: str) -> list[str]:
    """Extract skill names from the skills array."""
    skills = raw.get(key, [])
    if not skills:
        return []
    return [s.get("name", "") for s in skills if isinstance(s, dict) and s.get("name")]


def parse_all_listings(
    raw_listings: list[dict[str, Any]],
    run_id: str | None = None,
) -> list[dict[str, Any]]:
    """
    Parse all raw listings into normalized format.

    Args:
        raw_listings: List of raw listing dicts from the API.
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
