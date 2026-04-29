"""
Notion <> Polygon Stock Playbook Sync

Reads tickers from a Notion database, fetches historical prices from Polygon
for 5 days, 1 month, 6 months, and 1 year ago, then writes the prices back
to Notion and stamps a Last Market Sync timestamp.

Designed to be run daily on a schedule (e.g., GitHub Actions).

Required environment variables:
    POLYGON_API_KEY     - Your Polygon.io API key
    NOTION_TOKEN        - Notion integration token (starts with 'secret_' or 'ntn_')
    NOTION_DATABASE_ID  - The 32-character ID of the Stock Playbook database
"""

import os
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

# ---------- Configuration ----------

POLYGON_API_KEY = os.environ.get("POLYGON_API_KEY")
NOTION_TOKEN = os.environ.get("NOTION_TOKEN")
NOTION_DATABASE_ID = os.environ.get("NOTION_DATABASE_ID")

# Notion property names — these must match your database EXACTLY (case-sensitive)
TICKER_PROPERTY = "Ticker"
PRICE_PROPERTIES = {
    "now": "Price Now",
    "5d": "Price 5D Ago",
    "1m": "Price 1M Ago",
    "6m": "Price 6M Ago",
    "1y": "Price 1Y Ago",
}
LAST_SYNC_PROPERTY = "Last Market Sync"

# Polygon API
POLYGON_BASE = "https://api.polygon.io"

# Notion API
NOTION_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

# Rate limiting: free tier is 5 calls/min. We auto-throttle on 429.
INITIAL_DELAY_BETWEEN_POLYGON_CALLS = 0.0  # seconds; bumped automatically if rate-limited
MAX_RETRIES_ON_RATE_LIMIT = 6


# ---------- Helpers ----------

def fail(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def validate_env() -> None:
    missing = [
        name for name, val in [
            ("POLYGON_API_KEY", POLYGON_API_KEY),
            ("NOTION_TOKEN", NOTION_TOKEN),
            ("NOTION_DATABASE_ID", NOTION_DATABASE_ID),
        ] if not val
    ]
    if missing:
        fail(f"Missing environment variables: {', '.join(missing)}")


def lookback_dates(today: Optional[datetime] = None) -> dict[str, str]:
    """Return YYYY-MM-DD strings for each lookback period."""
    today = today or datetime.now(timezone.utc).date()
    if isinstance(today, datetime):
        today = today.date()
    return {
        "now": today.isoformat(),
        "5d": (today - timedelta(days=5)).isoformat(),
        "1m": (today - timedelta(days=30)).isoformat(),
        "6m": (today - timedelta(days=182)).isoformat(),
        "1y": (today - timedelta(days=365)).isoformat(),
    }


# ---------- Polygon ----------

_polygon_delay = INITIAL_DELAY_BETWEEN_POLYGON_CALLS


def polygon_get_close_price(ticker: str, target_date: str) -> Optional[float]:
    """
    Fetch the closing price for a ticker on or near target_date.

    Markets are closed on weekends and holidays, so we use the Aggregates
    endpoint with a small window (target_date back 5 days) and take the
    last available bar. This handles holidays cleanly.

    Returns the close price as a float, or None if no data found.
    """
    global _polygon_delay

    # Look back up to 7 days from target_date to skip weekends/holidays
    target = datetime.strptime(target_date, "%Y-%m-%d").date()
    window_start = (target - timedelta(days=7)).isoformat()
    window_end = target_date

    url = (
        f"{POLYGON_BASE}/v2/aggs/ticker/{ticker.upper()}"
        f"/range/1/day/{window_start}/{window_end}"
    )
    params = {
        "adjusted": "true",
        "sort": "desc",
        "limit": 1,
        "apiKey": POLYGON_API_KEY,
    }

    for attempt in range(MAX_RETRIES_ON_RATE_LIMIT):
        if _polygon_delay > 0:
            time.sleep(_polygon_delay)

        try:
            resp = requests.get(url, params=params, timeout=30)
        except requests.RequestException as e:
            print(f"  Network error for {ticker} @ {target_date}: {e}")
            return None

        if resp.status_code == 429:
            # Rate limited — bump the delay and back off exponentially
            _polygon_delay = max(_polygon_delay, 13.0)  # 5 calls/min => 12s+ between calls
            wait = 15 * (2 ** attempt)
            print(f"  Rate limited by Polygon. Backing off {wait}s and increasing delay to {_polygon_delay}s.")
            time.sleep(wait)
            continue

        if resp.status_code != 200:
            print(f"  Polygon HTTP {resp.status_code} for {ticker} @ {target_date}: {resp.text[:200]}")
            return None

        data = resp.json()
        results = data.get("results") or []
        if not results:
            # No bars in the window — likely a delisted ticker, bad symbol, or pre-IPO date
            return None
        return float(results[0]["c"])

    print(f"  Gave up on {ticker} @ {target_date} after {MAX_RETRIES_ON_RATE_LIMIT} retries.")
    return None


# ---------- Notion ----------

def notion_headers() -> dict:
    return {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def notion_query_all_pages() -> list[dict]:
    """Page through the entire database and return all page objects."""
    pages = []
    url = f"{NOTION_BASE}/databases/{NOTION_DATABASE_ID}/query"
    payload: dict = {"page_size": 100}

    while True:
        resp = requests.post(url, headers=notion_headers(), json=payload, timeout=30)
        if resp.status_code != 200:
            fail(f"Notion query failed: HTTP {resp.status_code} - {resp.text}")
        data = resp.json()
        pages.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        payload["start_cursor"] = data["next_cursor"]

    return pages


def extract_ticker(page: dict) -> Optional[str]:
    """Pull the ticker string out of a Notion page object."""
    prop = page.get("properties", {}).get(TICKER_PROPERTY)
    if not prop:
        return None
    # Ticker is a Text (rich_text) property
    rich_text = prop.get("rich_text", [])
    if not rich_text:
        return None
    text = "".join(rt.get("plain_text", "") for rt in rich_text).strip()
    return text or None


def update_page_prices(page_id: str, prices: dict[str, Optional[float]]) -> bool:
    """Write price fields and Last Market Sync timestamp back to a Notion page."""
    properties: dict = {}

    for key, prop_name in PRICE_PROPERTIES.items():
        value = prices.get(key)
        # Notion accepts None to clear a Number; we leave it as None to make missing data visible
        properties[prop_name] = {"number": value if value is not None else None}

    properties[LAST_SYNC_PROPERTY] = {
        "date": {"start": datetime.now(timezone.utc).isoformat()}
    }

    url = f"{NOTION_BASE}/pages/{page_id}"
    resp = requests.patch(url, headers=notion_headers(), json={"properties": properties}, timeout=30)
    if resp.status_code != 200:
        print(f"  Notion update failed for page {page_id}: HTTP {resp.status_code} - {resp.text[:300]}")
        return False
    return True


# ---------- Main ----------

def main() -> int:
    validate_env()

    print(f"Starting sync at {datetime.now(timezone.utc).isoformat()}")
    dates = lookback_dates()
    print(f"Lookback dates: {dates}")

    print("Fetching pages from Notion...")
    pages = notion_query_all_pages()
    print(f"Found {len(pages)} pages in database.")

    success_count = 0
    skipped_count = 0
    failed_count = 0

    for i, page in enumerate(pages, 1):
        ticker = extract_ticker(page)
        page_id = page["id"]

        if not ticker:
            print(f"[{i}/{len(pages)}] (no ticker) — skipping")
            skipped_count += 1
            continue

        print(f"[{i}/{len(pages)}] {ticker}")

        prices: dict[str, Optional[float]] = {}
        for key, target_date in dates.items():
            price = polygon_get_close_price(ticker, target_date)
            prices[key] = price
            label = f"${price:.2f}" if price is not None else "no data"
            print(f"  {key:>3}: {label}")

        if update_page_prices(page_id, prices):
            success_count += 1
        else:
            failed_count += 1

    print()
    print(f"Done. Success: {success_count}, Skipped (no ticker): {skipped_count}, Failed: {failed_count}")
    return 0 if failed_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
