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

# Earnings (Date-type properties in Notion)
EARNINGS_LAST_PROPERTY = "Last Earnings"
EARNINGS_NEXT_PROPERTY = "Next Earnings"

# Polygon API
POLYGON_BASE = "https://api.polygon.io"

# Notion API
NOTION_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

# Rate limiting: free tier is 5 calls/min. We auto-throttle on 429.
INITIAL_DELAY_BETWEEN_POLYGON_CALLS = 0.0  # seconds; bumped automatically if rate-limited
MAX_RETRIES_ON_RATE_LIMIT = 6

# Earnings endpoint access — flipped to False on first auth error so we don't keep retrying
EARNINGS_AVAILABLE = True


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


def polygon_get_earnings(ticker: str) -> tuple[Optional[str], Optional[str]]:
    """
    Fetch last and next earnings dates for a ticker via the Polygon/Benzinga endpoint.

    Returns (last_earnings_date, next_earnings_date) as ISO YYYY-MM-DD strings,
    or (None, None) if no data found or if the endpoint isn't accessible.

    If the endpoint returns 401/403 (plan doesn't include Benzinga data), this
    function disables itself for the rest of the run via the EARNINGS_AVAILABLE flag.
    """
    global EARNINGS_AVAILABLE, _polygon_delay

    if not EARNINGS_AVAILABLE:
        return (None, None)

    today = datetime.now(timezone.utc).date()
    # Look 1 year back and 1 year forward — generous window to catch any recent/upcoming earnings
    start = (today - timedelta(days=400)).isoformat()
    end = (today + timedelta(days=400)).isoformat()

    url = f"{POLYGON_BASE}/benzinga/v1/earnings"
    params = {
        "ticker": ticker.upper(),
        "date.gte": start,
        "date.lte": end,
        "sort": "date.asc",
        "limit": 50,
        "apiKey": POLYGON_API_KEY,
    }

    for attempt in range(MAX_RETRIES_ON_RATE_LIMIT):
        if _polygon_delay > 0:
            time.sleep(_polygon_delay)

        try:
            resp = requests.get(url, params=params, timeout=30)
        except requests.RequestException as e:
            print(f"  Earnings network error for {ticker}: {e}")
            return (None, None)

        if resp.status_code == 429:
            _polygon_delay = max(_polygon_delay, 13.0)
            wait = 15 * (2 ** attempt)
            print(f"  Rate limited (earnings). Backing off {wait}s.")
            time.sleep(wait)
            continue

        if resp.status_code in (401, 403):
            # Plan doesn't include Benzinga earnings data — disable for rest of run
            print(f"  NOTE: Polygon plan does not include Benzinga earnings access (HTTP {resp.status_code}). Skipping earnings for remaining tickers.")
            EARNINGS_AVAILABLE = False
            return (None, None)

        if resp.status_code != 200:
            # 404 / 4xx for a specific ticker (e.g., no earnings data) — silent skip, not a global problem
            return (None, None)

        data = resp.json()
        results = data.get("results") or []
        if not results:
            return (None, None)

        # Dedupe by date and split into past vs future
        today_iso = today.isoformat()
        seen_dates = set()
        last_date: Optional[str] = None
        next_date: Optional[str] = None
        for item in results:
            d = item.get("date")
            if not d or d in seen_dates:
                continue
            seen_dates.add(d)
            if d < today_iso:
                last_date = d  # results are sorted asc, so the last past date we see is the most recent
            elif d >= today_iso and next_date is None:
                next_date = d  # first future date is the closest upcoming
        return (last_date, next_date)

    print(f"  Gave up on earnings for {ticker} after retries.")
    return (None, None)


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


def update_page_prices(
    page_id: str,
    prices: dict[str, Optional[float]],
    earnings: Optional[tuple[Optional[str], Optional[str]]] = None,
) -> bool:
    """Write price fields, earnings dates, and Last Market Sync timestamp back to a Notion page.

    Notion's API rate limit is ~3 req/sec averaged. We sleep briefly before each
    write to stay comfortably under that, and we retry with exponential backoff
    if we hit a 429 anyway (e.g., another integration sharing the workspace).

    `earnings` is (last_earnings_date, next_earnings_date) as ISO date strings or None.
    Pass None to skip writing earnings entirely (preserves existing values in Notion).
    """
    properties: dict = {}

    for key, prop_name in PRICE_PROPERTIES.items():
        value = prices.get(key)
        # Notion accepts None to clear a Number; we leave it as None to make missing data visible
        properties[prop_name] = {"number": value if value is not None else None}

    if earnings is not None:
        last_earnings, next_earnings = earnings
        # Notion Date property: pass {"start": "YYYY-MM-DD"} or None to clear
        properties[EARNINGS_LAST_PROPERTY] = (
            {"date": {"start": last_earnings}} if last_earnings else {"date": None}
        )
        properties[EARNINGS_NEXT_PROPERTY] = (
            {"date": {"start": next_earnings}} if next_earnings else {"date": None}
        )

    properties[LAST_SYNC_PROPERTY] = {
        "date": {"start": datetime.now(timezone.utc).isoformat()}
    }

    url = f"{NOTION_BASE}/pages/{page_id}"

    # Baseline throttle: ~3 writes/sec keeps us safely under Notion's limit
    time.sleep(0.34)

    for attempt in range(5):
        resp = requests.patch(url, headers=notion_headers(), json={"properties": properties}, timeout=30)
        if resp.status_code == 200:
            return True
        if resp.status_code == 429:
            # Honor Retry-After header if present, otherwise back off exponentially
            retry_after = resp.headers.get("Retry-After")
            wait = float(retry_after) if retry_after else (2 ** attempt) * 2
            print(f"  Notion rate-limited on page {page_id}. Waiting {wait}s and retrying (attempt {attempt + 1}/5).")
            time.sleep(wait)
            continue
        # Non-429 errors are not retried — they indicate a real problem (bad property name, etc.)
        print(f"  Notion update failed for page {page_id}: HTTP {resp.status_code} - {resp.text[:300]}")
        return False

    print(f"  Notion update failed for page {page_id} after 5 retries (persistent rate limiting).")
    return False


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

        # Fetch earnings dates (last & next). Returns (None, None) if disabled or no data.
        earnings = polygon_get_earnings(ticker)
        last_e, next_e = earnings
        if EARNINGS_AVAILABLE:
            print(f"  earnings: last={last_e or '—'}, next={next_e or '—'}")
        # Pass earnings as None (don't touch the columns) if the endpoint is disabled globally;
        # otherwise pass the tuple even if both values are None, so we clear stale data for ETFs etc.
        earnings_arg = earnings if EARNINGS_AVAILABLE else None

        if update_page_prices(page_id, prices, earnings_arg):
            success_count += 1
        else:
            failed_count += 1

    print()
    print(f"Done. Success: {success_count}, Skipped (no ticker): {skipped_count}, Failed: {failed_count}")
    return 0 if failed_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
