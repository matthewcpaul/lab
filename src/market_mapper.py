"""Market mapper for finding current Bitcoin Up/Down hourly markets."""

import json
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

import requests
from termcolor import colored

from .config import save_market_map


GAMMA_API_URL = "https://gamma-api.polymarket.com/events"
CLOB_API_URL = "https://clob.polymarket.com"

# Eastern Time zone (Polymarket uses ET)
ET = ZoneInfo("America/New_York")


def get_hourly_slug(dt: datetime) -> str:
    """Construct the slug for an hourly Bitcoin Up or Down market.

    Example: "bitcoin-up-or-down-january-20-5pm-et"
    """
    month = dt.strftime("%B").lower()
    day = dt.day
    hour = dt.hour

    # Convert to 12-hour format
    if hour == 0:
        hour_str = "12am"
    elif hour < 12:
        hour_str = f"{hour}am"
    elif hour == 12:
        hour_str = "12pm"
    else:
        hour_str = f"{hour - 12}pm"

    return f"bitcoin-up-or-down-{month}-{day}-{hour_str}-et"


def fetch_event_by_slug(slug: str) -> Optional[dict]:
    """Fetch an event from Polymarket by its slug."""
    headers = {
        "Accept": "application/json",
        "User-Agent": "PolymarketBot/1.0"
    }
    params = {"slug": slug}

    try:
        response = requests.get(GAMMA_API_URL, params=params, headers=headers, timeout=30)
        response.raise_for_status()
        events = response.json()
        return events[0] if events else None
    except requests.exceptions.RequestException as e:
        print(colored(f"  Error fetching {slug}: {e}", "red"))
        return None


def extract_market_from_event(event: dict) -> Optional[dict]:
    """Extract market data from an event response."""
    markets = event.get("markets", [])
    if not markets:
        return None

    market = markets[0]
    question = market.get("question", "")

    # Parse clobTokenIds (may be string or list)
    clob_token_ids_raw = market.get("clobTokenIds", "[]")
    if isinstance(clob_token_ids_raw, str):
        try:
            clob_token_ids = json.loads(clob_token_ids_raw)
        except json.JSONDecodeError:
            return None
    else:
        clob_token_ids = clob_token_ids_raw or []

    if len(clob_token_ids) < 2:
        return None

    # For Bitcoin Up/Down: first token is "Up", second is "Down"
    return {
        "title": question,
        "up_token_id": clob_token_ids[0],
        "down_token_id": clob_token_ids[1],
        "slug": event.get("slug", ""),
        "expiration": market.get("endDate"),
    }


def run_mapper() -> dict:
    """Main mapper function - finds current market and saves mapping."""
    print(colored("=" * 50, "cyan"))
    print(colored("POLYMARKET MARKET MAPPER", "cyan"))
    print(colored("=" * 50, "cyan"))

    # Get current time in ET
    now = datetime.now(ET)
    current_hour = now.replace(minute=0, second=0, microsecond=0)

    print(f"\nCurrent time: {now.strftime('%Y-%m-%d %I:%M %p')} ET")

    # Build slug for current hourly market
    slug = get_hourly_slug(current_hour)
    time_str = current_hour.strftime('%I %p')

    print(f"\nFetching: {slug} ({time_str})...", end=" ")

    event = fetch_event_by_slug(slug)
    if not event:
        print(colored("Not found!", "red"))
        return None

    market = extract_market_from_event(event)
    if not market:
        print(colored("No market data!", "red"))
        return None

    print(colored("Found!", "green"))
    print(f"  Title: {market['title']}")

    # Build market map
    market_map = {
        "event_slug": market["slug"],
        "event_title": market["title"],
        "up_token_id": market["up_token_id"],
        "down_token_id": market["down_token_id"],
        "mapped_at": datetime.now(ET).isoformat(),
    }

    # Save to file
    save_market_map(market_map)
    print(colored("\nMarket map saved to market_map.json", "green"))
    print(f"  UP token:   {market['up_token_id'][:20]}...")
    print(f"  DOWN token: {market['down_token_id'][:20]}...")

    return market_map


if __name__ == "__main__":
    run_mapper()
