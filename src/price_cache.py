"""Thread-safe price cache for Polymarket WebSocket prices."""

import threading
import time
from dataclasses import dataclass
from typing import Optional


@dataclass
class PriceSnapshot:
    """A snapshot of bid/ask prices for a token."""

    token_id: str
    best_bid: Optional[float]
    best_ask: Optional[float]
    timestamp_ms: float  # Unix timestamp in milliseconds


class PriceCache:
    """
    Thread-safe cache for real-time Polymarket prices.

    Updated by WebSocket client, read by signal controller and order executor.
    Provides staleness detection to reject prices older than configured threshold.
    """

    def __init__(self, stale_ms: int = 5000):
        """
        Initialize price cache.

        Args:
            stale_ms: Prices older than this are considered stale (default 5 seconds)
        """
        self._stale_ms = stale_ms
        self._prices: dict[str, PriceSnapshot] = {}
        self._lock = threading.Lock()

    def update(self, token_id: str, best_bid: Optional[float], best_ask: Optional[float]):
        """
        Update cached prices for a token.

        Args:
            token_id: Token ID
            best_bid: Best bid price (or None if unchanged)
            best_ask: Best ask price (or None if unchanged)
        """
        now_ms = time.time() * 1000

        with self._lock:
            existing = self._prices.get(token_id)

            # Merge with existing values if only partial update
            if existing:
                if best_bid is None:
                    best_bid = existing.best_bid
                if best_ask is None:
                    best_ask = existing.best_ask

            self._prices[token_id] = PriceSnapshot(
                token_id=token_id,
                best_bid=best_bid,
                best_ask=best_ask,
                timestamp_ms=now_ms,
            )

    def get(self, token_id: str) -> Optional[PriceSnapshot]:
        """
        Get price snapshot for a token.

        Returns:
            PriceSnapshot if available and not stale, None otherwise
        """
        with self._lock:
            snapshot = self._prices.get(token_id)
            if snapshot is None:
                return None

            # Check staleness
            now_ms = time.time() * 1000
            age_ms = now_ms - snapshot.timestamp_ms
            if age_ms > self._stale_ms:
                return None

            return snapshot

    def get_best_bid(self, token_id: str) -> Optional[float]:
        """
        Get best bid price for a token.

        Returns:
            Best bid price if available and not stale, None otherwise
        """
        snapshot = self.get(token_id)
        if snapshot is None:
            return None
        return snapshot.best_bid

    def get_best_ask(self, token_id: str) -> Optional[float]:
        """
        Get best ask price for a token.

        Returns:
            Best ask price if available and not stale, None otherwise
        """
        snapshot = self.get(token_id)
        if snapshot is None:
            return None
        return snapshot.best_ask

    def is_spread_acceptable(self, token_id: str, max_spread_cents: int) -> bool:
        """
        Check if the spread for a token is within acceptable range.

        Args:
            token_id: Token to check
            max_spread_cents: Maximum acceptable spread in cents (e.g., 1 = 1 cent)

        Returns:
            True if spread is acceptable and prices are fresh, False otherwise
        """
        snapshot = self.get(token_id)
        if snapshot is None:
            return False

        if snapshot.best_bid is None or snapshot.best_ask is None:
            return False

        if snapshot.best_bid <= 0:
            return False

        spread = snapshot.best_ask - snapshot.best_bid
        return spread <= max_spread_cents * 0.01

    def is_stale(self, token_id: str) -> bool:
        """
        Check if cached prices for a token are stale.

        Args:
            token_id: Token to check

        Returns:
            True if prices are stale or missing, False if fresh
        """
        return self.get(token_id) is None

    def get_age_ms(self, token_id: str) -> Optional[float]:
        """
        Get the age of cached prices in milliseconds.

        Args:
            token_id: Token to check

        Returns:
            Age in milliseconds, or None if no cached price
        """
        with self._lock:
            snapshot = self._prices.get(token_id)
            if snapshot is None:
                return None

            now_ms = time.time() * 1000
            return now_ms - snapshot.timestamp_ms
