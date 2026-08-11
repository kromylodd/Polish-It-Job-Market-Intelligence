"""
justjoin.it scraper.

Uses the internal JSON API at /api/candidate-api/offers.
Cursor-based pagination, 10 results per page (hard cap), 10k total cap.
"""

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://justjoin.it/api/candidate-api/offers"
RESULTS_PER_PAGE = 10
MAX_TOTAL_RESULTS = 10_000

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

# Delay between page requests. Lowered from 1.0s → 0.5s now that 429
# `Retry-After` handling is in place (roughly halves the ~1000-page run);
# override via REQUEST_DELAY_SECONDS if the API starts pushing back.
REQUEST_DELAY_SECONDS = float(os.environ.get("REQUEST_DELAY_SECONDS", "0.5"))
MAX_RETRIES = 3
RETRY_BACKOFF_FACTOR = 2.0


def create_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(HEADERS)
    return session


# Upper bound so a hostile/misconfigured Retry-After can't stall the job for hours.
MAX_RETRY_AFTER_SECONDS = 120.0


def _parse_retry_after(response: requests.Response) -> float:
    """Parse a 429 Retry-After header (seconds form), with a sane fallback/cap."""
    raw = response.headers.get("Retry-After", "")
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        seconds = REQUEST_DELAY_SECONDS * 5
    return max(0.0, min(seconds, MAX_RETRY_AFTER_SECONDS))


def fetch_page(
    session: requests.Session,
    from_offset: int = 0,
    categories: list[str] | None = None,
) -> dict[str, Any] | None:
    """Fetch a single page of listings."""
    params: dict[str, Any] = {
        "perPage": RESULTS_PER_PAGE,
        "from": from_offset,
    }

    if categories:
        params["categories[]"] = list(categories)

    for attempt in range(MAX_RETRIES):
        try:
            response = session.get(BASE_URL, params=params, timeout=30)
            # Respect explicit rate limiting from the API.
            if response.status_code == 429:
                retry_after = _parse_retry_after(response)
                logger.warning(
                    "Rate limited (429) at offset %s; waiting %.1fs (attempt %d/%d)",
                    from_offset,
                    retry_after,
                    attempt + 1,
                    MAX_RETRIES,
                )
                if attempt < MAX_RETRIES - 1:
                    time.sleep(retry_after)
                    continue
                return None
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            logger.warning(f"Attempt {attempt + 1}/{MAX_RETRIES} failed: {e}")
            # Back off between attempts, but don't sleep after the final one.
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_BACKOFF_FACTOR**attempt)

    logger.error(f"Failed to fetch offset {from_offset}")
    return None


def scrape_listings(
    categories: list[str] | None = None,
    max_listings: int | None = None,
    delay: float = REQUEST_DELAY_SECONDS,
) -> list[dict[str, Any]]:
    """Scrape job listings. Returns list of raw API response dicts."""
    session = create_session()
    all_listings: list[dict[str, Any]] = []
    effective_max = min(max_listings or MAX_TOTAL_RESULTS, MAX_TOTAL_RESULTS)

    logger.info(f"Scraping: categories={categories}, max={effective_max}")

    from_offset = 0
    total_available: int | None = None

    while from_offset < effective_max:
        response_data = fetch_page(session, from_offset=from_offset, categories=categories)
        if response_data is None:
            break

        listings = response_data.get("data", [])
        meta = response_data.get("meta", {})

        if total_available is None:
            total_available = meta.get("totalItems", 0)
            logger.info(f"Total available: {total_available}")
            if total_available >= MAX_TOTAL_RESULTS:
                logger.warning(f"Total hits API cap of {MAX_TOTAL_RESULTS}")

        if not listings:
            break

        all_listings.extend(listings)
        logger.info(f"Offset {from_offset}: +{len(listings)} (total: {len(all_listings)})")

        # Offset-based pagination: advance by the number of items actually
        # returned. (The API ignores perPage and hard-caps pages at 10 items,
        # so we can't assume a fixed stride.)
        from_offset += len(listings)

        if len(all_listings) >= effective_max:
            break
        if total_available and from_offset >= total_available:
            break

        time.sleep(delay)

    logger.info(f"Done: {len(all_listings)} listings collected")
    return all_listings


def save_raw_output(listings: list[dict[str, Any]], output_dir: str = "data") -> Path:
    """Save listings to a fixed-name JSON file (overwritten each run).

    Using a fixed filename avoids accumulating hundreds of timestamped files in
    the Databricks Volume and eliminates spurious file-arrival triggers from
    multiple unique filenames.
    """
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    filepath = output_path / "raw_listings_latest.json"

    output = {
        "metadata": {
            "source": "justjoin.it",
            "api_endpoint": BASE_URL,
            "date_collected": datetime.now(timezone.utc).isoformat(),
            "total_listings": len(listings),
        },
        "listings": listings,
    }

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    logger.info(f"Saved {len(listings)} listings to {filepath}")
    return filepath


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    from scraper.parser import parse_all_listings

    raw_listings = scrape_listings()
    if raw_listings:
        # Parse raw API data into the normalized schema that matches the bronze
        # ingest schema. The raw API uses different field names (guid/companyName/etc.)
        # and the bronze layer expects parsed output (listing_id/company_name/etc.).
        parsed = parse_all_listings(raw_listings)
        logger.info(f"Parsed {len(parsed)}/{len(raw_listings)} listings")
        save_raw_output(parsed)
