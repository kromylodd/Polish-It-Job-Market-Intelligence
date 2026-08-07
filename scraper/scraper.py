"""
Scraper for justjoin.it — fetches job listings via the internal JSON API.

Discovery: justjoin.it has an internal API at /api/candidate-api/offers
that returns structured JSON with cursor-based pagination.
No RSC payload parsing needed — this is a clean REST-like endpoint.

API details (verified 2026-08-07):
- Endpoint: https://justjoin.it/api/candidate-api/offers
- Pagination: `from` parameter (offset-based), 10 results per page (hard cap)
- Filters: categories[]=<key>, experienceLevel[]=<level>, etc.
- Total results cap: 10,000 (analogous to OLX's 1000-result cap — documented)
- No auth required for read access
"""

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

logger = logging.getLogger(__name__)

# API configuration
BASE_URL = "https://justjoin.it/api/candidate-api/offers"
RESULTS_PER_PAGE = 10  # Hard-capped by the API, cannot be increased
MAX_TOTAL_RESULTS = 10_000  # API cap — document this in README like OLX's cap

# Request config
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

# Respectful scraping
REQUEST_DELAY_SECONDS = 1.0
MAX_RETRIES = 3
RETRY_BACKOFF_FACTOR = 2.0


def create_session() -> requests.Session:
    """Create a requests session with default headers."""
    session = requests.Session()
    session.headers.update(HEADERS)
    return session


def fetch_page(
    session: requests.Session,
    from_offset: int = 0,
    categories: list[str] | None = None,
) -> dict[str, Any] | None:
    """
    Fetch a single page of listings from the API.

    Args:
        session: Requests session.
        from_offset: Pagination offset.
        categories: Optional list of category keys to filter by.

    Returns:
        API response as dict, or None on failure.
    """
    params: dict[str, Any] = {
        "perPage": RESULTS_PER_PAGE,
        "from": from_offset,
    }

    # Add category filters
    if categories:
        for cat in categories:
            params.setdefault("categories[]", [])
            if isinstance(params["categories[]"], list):
                params["categories[]"].append(cat)
            else:
                params["categories[]"] = [params["categories[]"], cat]

    for attempt in range(MAX_RETRIES):
        try:
            response = session.get(BASE_URL, params=params, timeout=30)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            wait_time = RETRY_BACKOFF_FACTOR ** attempt
            logger.warning(
                f"Request failed (attempt {attempt + 1}/{MAX_RETRIES}): {e}. "
                f"Retrying in {wait_time}s..."
            )
            time.sleep(wait_time)

    logger.error(f"Failed to fetch offset {from_offset} after {MAX_RETRIES} attempts")
    return None


def scrape_listings(
    categories: list[str] | None = None,
    max_listings: int | None = None,
    delay: float = REQUEST_DELAY_SECONDS,
) -> list[dict[str, Any]]:
    """
    Scrape all job listings from justjoin.it API.

    Args:
        categories: Optional category filter (e.g., ["data", "python"]).
                    If None, scrapes all categories.
        max_listings: Maximum number of listings to collect. None = all available.
        delay: Delay between requests in seconds (respectful scraping).

    Returns:
        List of raw listing dictionaries from the API.
    """
    session = create_session()
    all_listings: list[dict[str, Any]] = []

    effective_max = min(max_listings or MAX_TOTAL_RESULTS, MAX_TOTAL_RESULTS)
    logger.info(
        f"Starting scrape — categories={categories}, "
        f"max_listings={effective_max}, delay={delay}s"
    )

    from_offset = 0
    total_available = None

    while from_offset < effective_max:
        response_data = fetch_page(session, from_offset=from_offset, categories=categories)

        if response_data is None:
            logger.error(f"Stopping at offset {from_offset} due to fetch failure")
            break

        listings = response_data.get("data", [])
        meta = response_data.get("meta", {})

        if total_available is None:
            total_available = meta.get("totalItems", 0)
            logger.info(f"API reports {total_available} total items")

            # OLX cap lesson: verify the claimed total is reachable
            if total_available >= MAX_TOTAL_RESULTS:
                logger.warning(
                    f"⚠️ totalItems={total_available} hits the API cap of {MAX_TOTAL_RESULTS}. "
                    f"Some listings may be inaccessible. Document this limitation."
                )

        if not listings:
            logger.info(f"No listings at offset {from_offset} — end of results")
            break

        all_listings.extend(listings)
        logger.info(
            f"Offset {from_offset}: fetched {len(listings)} listings "
            f"(total collected: {len(all_listings)})"
        )

        # Check if there's a next page
        next_info = meta.get("next", {})
        next_cursor = next_info.get("cursor") if next_info else None

        if next_cursor is None:
            logger.info("No next cursor — reached end of results")
            break

        from_offset = next_cursor

        # Stop if we've collected enough
        if len(all_listings) >= effective_max:
            logger.info(f"Reached max_listings cap ({effective_max})")
            break

        time.sleep(delay)

    logger.info(f"Scrape complete: {len(all_listings)} total listings collected")
    return all_listings


def save_raw_output(listings: list[dict[str, Any]], output_dir: str = "data") -> Path:
    """
    Save raw scraped listings to a JSON file with metadata.

    Args:
        listings: List of raw listing dictionaries.
        output_dir: Directory to save output files.

    Returns:
        Path to the saved file.
    """
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"raw_listings_{timestamp}.json"
    filepath = output_path / filename

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

    # Scrape all categories (plan: collect everything, filter at mart level)
    listings = scrape_listings(
        categories=None,  # All categories
        max_listings=None,  # Collect all available (up to API cap)
        delay=1.0,
    )

    if listings:
        filepath = save_raw_output(listings)
        print(f"Done: {len(listings)} listings saved to {filepath}")
    else:
        print("No listings collected")
