"""
Scraper for justjoin.it — fetches job listing pages and extracts
embedded React Server Component (RSC) flight payloads.

justjoin.it is a Next.js App Router site. Job data is embedded in
<script> tags as RSC flight payloads, not served via a public API.
Approach: plain HTTP GET + parse embedded JSON from HTML response.
"""

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://justjoin.it"
SEARCH_URL = f"{BASE_URL}/job-offers/all-locations/all"

# Request config
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7",
}

# Respectful scraping
REQUEST_DELAY_SECONDS = 2.0
MAX_RETRIES = 3
RETRY_BACKOFF_FACTOR = 2.0


def create_session() -> requests.Session:
    """Create a requests session with default headers."""
    session = requests.Session()
    session.headers.update(HEADERS)
    return session


def fetch_page(session: requests.Session, url: str) -> str | None:
    """Fetch a single page with retries and backoff."""
    for attempt in range(MAX_RETRIES):
        try:
            response = session.get(url, timeout=30)
            response.raise_for_status()
            return response.text
        except requests.RequestException as e:
            wait_time = RETRY_BACKOFF_FACTOR ** attempt
            logger.warning(
                f"Request failed (attempt {attempt + 1}/{MAX_RETRIES}): {e}. "
                f"Retrying in {wait_time}s..."
            )
            time.sleep(wait_time)

    logger.error(f"Failed to fetch {url} after {MAX_RETRIES} attempts")
    return None


def extract_rsc_payload(html: str) -> list[dict[str, Any]]:
    """
    Extract job listing data from the RSC flight payload embedded in the HTML.

    justjoin.it embeds structured listing data inside <script> tags as part
    of Next.js RSC (React Server Component) flight format. This function
    locates and parses that payload.

    TODO: Implement after inspecting a live response to identify the exact
    script tag pattern and JSON structure. The plan notes this is a genuinely
    different parsing pattern from Silesia's GraphQL JSON flattening.
    """
    # Placeholder — implement after verifying live response structure
    # Expected approach:
    # 1. Find <script> tags containing RSC flight data
    # 2. Parse the flight payload format (typically newline-delimited JSON chunks)
    # 3. Extract the listing data objects from the parsed payload
    raise NotImplementedError(
        "RSC payload extraction not yet implemented. "
        "Inspect a live justjoin.it response first to identify the exact "
        "script tag pattern and data structure."
    )


def scrape_listings(
    max_pages: int = 50,
    delay: float = REQUEST_DELAY_SECONDS,
) -> list[dict[str, Any]]:
    """
    Scrape job listings from justjoin.it.

    Args:
        max_pages: Maximum number of pages to scrape.
        delay: Delay between requests in seconds (respectful scraping).

    Returns:
        List of raw listing dictionaries.
    """
    session = create_session()
    all_listings: list[dict[str, Any]] = []

    logger.info(f"Starting scrape — max {max_pages} pages, {delay}s delay")

    # TODO: Implement pagination logic after verifying URL pattern
    # Expected: justjoin.it uses query params or path segments for pagination
    # Verify: total listings vs returned listings (OLX cap lesson)
    page = 1
    while page <= max_pages:
        url = f"{SEARCH_URL}?page={page}"  # TODO: verify pagination param
        logger.info(f"Fetching page {page}: {url}")

        html = fetch_page(session, url)
        if html is None:
            logger.error(f"Stopping at page {page} due to fetch failure")
            break

        listings = extract_rsc_payload(html)
        if not listings:
            logger.info(f"No listings found on page {page} — end of results")
            break

        all_listings.extend(listings)
        logger.info(f"Page {page}: extracted {len(listings)} listings (total: {len(all_listings)})")

        page += 1
        time.sleep(delay)

    logger.info(f"Scrape complete: {len(all_listings)} total listings")
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
    listings = scrape_listings()
    if listings:
        save_raw_output(listings)
