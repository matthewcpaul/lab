"""WebSocket client for real-time price streaming from Polymarket CLOB."""

import asyncio
import json
from typing import TYPE_CHECKING, Callable, Optional

import websockets
from websockets.exceptions import ConnectionClosed

if TYPE_CHECKING:
    from .price_cache import PriceCache


class PriceStream:
    """Real-time price streaming via WebSocket."""

    WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

    def __init__(
        self,
        token_ids: list[str],
        on_price_update: Callable[[str, float, float], None],
        on_connect: Optional[Callable[[], None]] = None,
        on_disconnect: Optional[Callable[[], None]] = None,
        price_cache: Optional["PriceCache"] = None,
    ):
        """
        Initialize price stream.

        Args:
            token_ids: List of token IDs to subscribe to
            on_price_update: Callback(token_id, best_bid, best_ask) on each update
            on_connect: Optional callback when connected
            on_disconnect: Optional callback when disconnected
            price_cache: Optional shared PriceCache to update on every price message
        """
        self.token_ids = token_ids
        self.on_price_update = on_price_update
        self.on_connect = on_connect
        self.on_disconnect = on_disconnect
        self.price_cache = price_cache

        # Current prices cache (internal, for backward compatibility)
        self.prices: dict[str, dict] = {}
        for token_id in token_ids:
            self.prices[token_id] = {"bid": None, "ask": None}

        self._running = False
        self._ws = None
        self._book_debug_logged = False  # One-time debug log for book snapshots

    async def connect(self):
        """Connect to WebSocket and start streaming prices."""
        self._running = True

        while self._running:
            try:
                async with websockets.connect(self.WS_URL) as ws:
                    self._ws = ws

                    if self.on_connect:
                        self.on_connect()

                    # Subscribe to each token's market data
                    for token_id in self.token_ids:
                        subscribe_msg = {
                            "type": "market",
                            "assets_ids": [token_id],
                        }
                        await ws.send(json.dumps(subscribe_msg))

                    # Process messages
                    async for message in ws:
                        if not self._running:
                            break
                        self._process_message(message)

            except ConnectionClosed:
                if self.on_disconnect:
                    self.on_disconnect()
                if self._running:
                    # Reconnect after brief delay
                    await asyncio.sleep(1)
            except Exception as e:
                if self._running:
                    await asyncio.sleep(1)

        self._ws = None

    def stop(self):
        """Stop the price stream."""
        self._running = False

    def _process_message(self, message: str):
        """Process incoming WebSocket message."""
        try:
            data = json.loads(message)
            self._handle_market_data(data)
        except json.JSONDecodeError:
            pass

    def _handle_market_data(self, data: dict):
        """Handle market data update from WebSocket."""
        # The CLOB WebSocket sends orderbook updates
        # Format varies but typically includes asset_id and book data

        if isinstance(data, list):
            # Batch of updates
            for item in data:
                self._process_single_update(item)
        else:
            self._process_single_update(data)

    def _process_single_update(self, data: dict):
        """Process a single market data update."""
        # Handle price_change events (most common and most useful)
        if data.get("event_type") == "price_change":
            self._handle_price_change(data)
            return

        # Extract token ID from various possible formats
        asset_id = data.get("asset_id") or data.get("market") or data.get("token_id")

        if not asset_id or asset_id not in self.prices:
            return

        # Extract best bid/ask from the update
        best_bid = None
        best_ask = None

        # Handle orderbook snapshot
        if "book" in data:
            bids = data["book"].get("bids", [])
            asks = data["book"].get("asks", [])
            best_bid, best_ask = self._extract_best_from_arrays(bids, asks)
        elif "bids" in data and "asks" in data:
            bids = data.get("bids", [])
            asks = data.get("asks", [])
            best_bid, best_ask = self._extract_best_from_arrays(bids, asks)
        elif "price" in data:
            # Single price/trade update
            price = float(data["price"])
            side = data.get("side", "").upper()
            if side == "BUY":
                best_bid = price
            elif side == "SELL":
                best_ask = price

        # Update cache and notify
        self._update_prices(asset_id, best_bid, best_ask)

    def _extract_best_from_arrays(
        self, bids: list, asks: list
    ) -> tuple[Optional[float], Optional[float]]:
        """
        Extract best bid and best ask from orderbook arrays.

        Auto-detects sort order by comparing first and last elements,
        so it works regardless of whether the API sends ascending or descending.
        """
        best_bid = None
        best_ask = None

        if bids:
            first_bid = float(bids[0].get("price", 0))
            last_bid = float(bids[-1].get("price", 0))
            # Best bid = highest price
            best_bid = max(first_bid, last_bid)

            # One-time debug log to verify sort order
            if not self._book_debug_logged and len(bids) > 1:
                sort_dir = "ascending" if first_bid < last_bid else "descending"
                print(f"  [DEBUG] WS bids: {sort_dir} (first={first_bid}, last={last_bid}, best={best_bid})")

        if asks:
            first_ask = float(asks[0].get("price", 0))
            last_ask = float(asks[-1].get("price", 0))
            # Best ask = lowest price
            best_ask = min(first_ask, last_ask)

            if not self._book_debug_logged and len(asks) > 1:
                sort_dir = "ascending" if first_ask < last_ask else "descending"
                print(f"  [DEBUG] WS asks: {sort_dir} (first={first_ask}, last={last_ask}, best={best_ask})")

        if bids and asks and not self._book_debug_logged:
            self._book_debug_logged = True

        return best_bid, best_ask

    def _handle_price_change(self, data: dict):
        """Handle price_change event which includes best_bid/best_ask directly."""
        price_changes = data.get("price_changes", [])
        for change in price_changes:
            asset_id = change.get("asset_id")
            if not asset_id or asset_id not in self.prices:
                continue

            # price_change events include best_bid and best_ask directly
            best_bid = None
            best_ask = None

            if "best_bid" in change:
                best_bid = float(change["best_bid"])
            if "best_ask" in change:
                best_ask = float(change["best_ask"])

            self._update_prices(asset_id, best_bid, best_ask)

    def _update_prices(self, asset_id: str, best_bid: Optional[float], best_ask: Optional[float]):
        """Update price cache and notify callback."""
        if asset_id not in self.prices:
            return

        updated = False
        if best_bid is not None and best_bid > 0:
            self.prices[asset_id]["bid"] = best_bid
            updated = True
        if best_ask is not None and best_ask > 0:
            self.prices[asset_id]["ask"] = best_ask
            updated = True

        if updated:
            current = self.prices[asset_id]

            # Update shared price cache if available (for low-latency reads)
            if self.price_cache is not None:
                self.price_cache.update(asset_id, current["bid"], current["ask"])

            self.on_price_update(
                asset_id,
                current["bid"],
                current["ask"],
            )

    def get_best_bid(self, token_id: str) -> Optional[float]:
        """Get cached best bid for a token."""
        return self.prices.get(token_id, {}).get("bid")

    def get_best_ask(self, token_id: str) -> Optional[float]:
        """Get cached best ask for a token."""
        return self.prices.get(token_id, {}).get("ask")


class MockPriceStream:
    """Mock price stream for testing without live connection."""

    def __init__(self, token_ids: list[str], on_price_update: Callable):
        self.token_ids = token_ids
        self.on_price_update = on_price_update
        self.prices = {tid: {"bid": 0.50, "ask": 0.51} for tid in token_ids}
        self._running = False

    async def connect(self):
        """Simulate connection with periodic updates."""
        self._running = True
        import random

        while self._running:
            await asyncio.sleep(1)
            for token_id in self.token_ids:
                # Random walk
                delta = random.uniform(-0.01, 0.01)
                bid = max(0.01, min(0.99, self.prices[token_id]["bid"] + delta))
                ask = bid + 0.01
                self.prices[token_id] = {"bid": bid, "ask": ask}
                self.on_price_update(token_id, bid, ask)

    def stop(self):
        self._running = False

    def get_best_bid(self, token_id: str) -> Optional[float]:
        return self.prices.get(token_id, {}).get("bid")

    def get_best_ask(self, token_id: str) -> Optional[float]:
        return self.prices.get(token_id, {}).get("ask")
