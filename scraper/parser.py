"""
Parser for justjoin.it API response.

Normalizes raw API listings into a flat schema for the bronze layer.
Filters salary to original currency only, extracts per-unit rates.
"""

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


def parse_listing(raw: dict[str, Any], run_id: str) -> dict[str, Any] | None:
    """Parse single listing into normalized schema. Returns None if unparseable."""
    try:
        listing_id = raw.get("guid") or raw.get("slug")
        if not listing_id:
            logger.warning("Listing missing guid/slug — skipping")
            return None

        return {
            "listing_id": str(listing_id),
            "slug": raw.get("slug", ""),
            "title": raw.get("title", ""),
            "apply_url": raw.get("applyUrl", ""),
            "apply_method": raw.get("applyMethod", ""),
            "company_name": _extract_company_name(raw),
            "category": _extract_category(raw),
            "seniority": raw.get("experienceLevel", ""),
            "workplace_type": raw.get("workplaceType", ""),
            "working_time": raw.get("workingTime", ""),
            "cities": _extract_cities(raw),
            "salary_variants": _extract_salary_variants(raw),
            "required_skills": _extract_skills(raw, "requiredSkills"),
            "nice_to_have_skills": _extract_skills(raw, "niceToHaveSkills"),
            "languages": raw.get("languages", []),
            "posted_date": raw.get("publishedAt", ""),
            "last_published_date": raw.get("lastPublishedAt", ""),
            "expiry_date": raw.get("expiredAt", ""),
            "is_promoted": raw.get("isPromoted", False),
            "is_super_offer": raw.get("isSuperOffer", False),
            "is_remote_interview": raw.get("isRemoteInterview", False),
            "date_collected": datetime.now(timezone.utc).isoformat(),
            "source_run_id": run_id,
        }
    except Exception as e:
        listing_id = raw.get("guid", raw.get("slug", "unknown"))
        logger.error(f"Failed to parse listing {listing_id}: {e}")
        return None


def _extract_company_name(raw: dict[str, Any]) -> str:
    return raw.get("companyName", "")


def _extract_category(raw: dict[str, Any]) -> str:
    category = raw.get("category", "")
    if isinstance(category, dict):
        return category.get("key", "")
    return str(category) if category else ""


def _extract_cities(raw: dict[str, Any]) -> list[str]:
    """Extract unique cities from locations array, fallback to city field."""
    locations = raw.get("locations", [])
    if locations:
        return list({loc.get("city", "") for loc in locations if loc.get("city")})
    city = raw.get("city", "")
    return [city] if city else []


def _extract_salary_variants(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract salary variants, keeping only original currency (not conversions)."""
    variants = []
    for emp in raw.get("employmentTypes", []):
        if emp.get("currencySource") != "original":
            continue
        if emp.get("from") is None and emp.get("to") is None:
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
    """Extract skill names from skills array."""
    skills = raw.get(key, [])
    if not skills:
        return []
    return [s.get("name", "") for s in skills if isinstance(s, dict) and s.get("name")]


def parse_all_listings(
    raw_listings: list[dict[str, Any]],
    run_id: str | None = None,
) -> list[dict[str, Any]]:
    """Parse batch of listings. Skips unparseable ones."""
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

    logger.info(f"Parsed {len(parsed)}/{len(parsed) + failed} listings")
    return parsed
