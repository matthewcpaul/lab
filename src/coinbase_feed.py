"""Coinbase WebSocket feed for BTC price volatility detection."""

import asyncio
import json
from collections import deque
from typing import Awaitable, Callable, Optional, Union

import websockets
from websockets.exceptions import ConnectionClosed

# Signal callback type: can be sync or async
SignalCallback = Union[Callable[[str], None], Callable[[str], Awaitable[None]]]


class RollingWindow:
    """Rolling window of price ticks for volatility calculation."""

    def __init__(self, window_ms: int):
        """
        Initialize rolling window.

        Args:
            window_ms: Window size in milliseconds
        """
        self.window_ms = window_ms
        self._data: deque[tuple[float, float]] = deque()  # (time_ms, price)

    def add(self, time_ms: float, price: float):
        """
        Add a price tick and evict expired entries.

        Args:
            time_ms: Exchange timestamp in milliseconds
            price: Trade price
        """
        # Evict entries older than window
        cutoff = time_ms - self.window_ms
        while self._data and self._data[0][0] < cutoff:
            self._data.popleft()

        self._data.append((time_ms, price))

    def get_pct_change(self) -> Optional[float]:
        """
        Calculate percentage change from oldest to newest price.

        Returns:
            Percentage change as decimal (e.g., 0.001 = 0.1%), or None if insufficient data
        """
        if len(self._data) < 2:
            return None

        oldest_price = self._data[0][1]
        newest_price = self._data[-1][1]

        if oldest_price <= 0:
            return None

        return (newest_price - oldest_price) / oldest_price

    def clear(self):
        """Clear all data from the window."""
        self._data.clear()

    def __len__(self) -> int:
        return len(self._data)


class CoinbaseFeed:
    """Coinbase WebSocket feed for BTC-USD match detection."""

    WS_URL = "wss://ws-feed.exchange.coinbase.com"
    PRODUCT_ID = "BTC-USD"

    def __init__(
        self,
        window_ms: int,
        threshold: float,
        cooldown_ms: int,
        on_signal: SignalCallback,
        on_connect: Optional[Callable[[], None]] = None,
        on_disconnect: Optional[Callable[[], None]] = None,
    ):
        """
        Initialize Coinbase feed.

        Args:
            window_ms: Rolling window size in milliseconds
            threshold: Trigger threshold as decimal (e.g., 0.00015 = 0.015%)
            cooldown_ms: Minimum milliseconds between signals
            on_signal: Callback(direction) when signal fires ("UP" or "DOWN").
                       Can be sync or async function.
            on_connect: Optional callback when connected
            on_disconnect: Optional callback when disconnected
        """
        self.window = RollingWindow(window_ms)
        self.threshold = threshold
        self.cooldown_ms = cooldown_ms
        self.on_signal = on_signal
        self.on_connect = on_connect
        self.on_disconnect = on_disconnect

        self._last_signal_time: float = 0
        self._last_signal_data: Optional[dict] = None  # Snapshot when signal fires (for data logging)
        self._running = False
        self._paused = False
        self._ws = None

        # Async queue for non-blocking signal processing
        self._signal_queue: asyncio.Queue[str] = asyncio.Queue()
        self._signal_processor_task: Optional[asyncio.Task] = None

    async def connect(self):
        """Connect to Coinbase WebSocket and start processing matches."""
        self._running = True

        # Start signal processor task (runs in background, consumes queue)
        self._signal_processor_task = asyncio.create_task(self._process_signals())

        while self._running:
            try:
                async with websockets.connect(self.WS_URL) as ws:
                    self._ws = ws

                    # Clear window on connect (stale data)
                    self.window.clear()
                    self._last_signal_time = 0

                    # Subscribe to matches channel
                    subscribe_msg = {
                        "type": "subscribe",
                        "product_ids": [self.PRODUCT_ID],
                        "channels": ["matches"],
                    }
                    await ws.send(json.dumps(subscribe_msg))

                    if self.on_connect:
                        self.on_connect()

                    # Process messages
                    async for message in ws:
                        if not self._running:
                            break
                        self._process_message(message)

            except ConnectionClosed:
                if self.on_disconnect:
                    self.on_disconnect()
                if self._running:
                    # Clear window on disconnect
                    self.window.clear()
                    await asyncio.sleep(1)
            except Exception:
                if self._running:
                    self.window.clear()
                    await asyncio.sleep(1)

        self._ws = None

        # Cancel signal processor task
        if self._signal_processor_task:
            self._signal_processor_task.cancel()
            try:
                await self._signal_processor_task
            except asyncio.CancelledError:
                pass

    def stop(self):
        """Stop the feed."""
        self._running = False

    def pause(self):
        """Pause signal generation (still processes data)."""
        self._paused = True

    def resume(self):
        """Resume signal generation."""
        self._paused = False

    @property
    def is_paused(self) -> bool:
        return self._paused

    @property
    def is_connected(self) -> bool:
        return self._ws is not None

    def _process_message(self, message: str):
        """Process incoming WebSocket message."""
        try:
            data = json.loads(message)
            if data.get("type") == "match":
                self._handle_match(data)
        except json.JSONDecodeError:
            pass

    def _handle_match(self, data: dict):
        """Handle a match (trade) message."""
        # Extract price and exchange timestamp
        try:
            price = float(data.get("price", 0))
            time_str = data.get("time", "")

            if price <= 0 or not time_str:
                return

            # Parse ISO timestamp to milliseconds
            # Format: "2024-01-15T12:30:45.123456Z"
            time_ms = self._parse_timestamp(time_str)
            if time_ms is None:
                return

            # Add to rolling window
            self.window.add(time_ms, price)

            # Check for volatility signal
            self._check_signal(time_ms)

        except (ValueError, TypeError):
            pass

    def _parse_timestamp(self, time_str: str) -> Optional[float]:
        """Parse ISO timestamp to milliseconds since epoch."""
        try:
            from datetime import datetime, timezone

            # Handle both formats: with and without microseconds
            if "." in time_str:
                # Truncate to 6 decimal places if longer
                parts = time_str.replace("Z", "").split(".")
                if len(parts[1]) > 6:
                    parts[1] = parts[1][:6]
                time_str = f"{parts[0]}.{parts[1]}Z"
                dt = datetime.strptime(time_str, "%Y-%m-%dT%H:%M:%S.%fZ")
            else:
                dt = datetime.strptime(time_str, "%Y-%m-%dT%H:%M:%SZ")

            dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp() * 1000

        except (ValueError, TypeError):
            return None

    def _check_signal(self, current_time_ms: float):
        """Check if volatility threshold crossed and queue signal (non-blocking)."""
        if self._paused:
            return

        pct_change = self.window.get_pct_change()
        if pct_change is None:
            return

        # Check threshold
        if abs(pct_change) < self.threshold:
            return

        # Check cooldown
        if current_time_ms - self._last_signal_time < self.cooldown_ms:
            return

        # Queue signal (non-blocking - doesn't wait for processing)
        direction = "UP" if pct_change > 0 else "DOWN"
        self._last_signal_time = current_time_ms

        # Stash window snapshot for data logging (rolling window ticks)
        self._last_signal_data = {
            "pct_change": pct_change,
            "direction": direction,
            "window_ticks": [{"time_ms": t, "price": p} for t, p in self.window._data],
            "signal_time_ms": current_time_ms,
        }

        # put_nowait is non-blocking - if queue is full, raises QueueFull
        # We use an unbounded queue so this won't happen in practice
        try:
            self._signal_queue.put_nowait(direction)
        except asyncio.QueueFull:
            pass  # Drop signal if queue somehow fills up

    async def _process_signals(self):
        """Background task that consumes signal queue and calls callback."""
        while self._running:
            try:
                # Wait for next signal (blocks until available)
                direction = await self._signal_queue.get()

                # Call the signal callback (supports both sync and async)
                result = self.on_signal(direction)
                if asyncio.iscoroutine(result):
                    await result

                self._signal_queue.task_done()

            except asyncio.CancelledError:
                break
            except Exception:
                # Don't let callback errors kill the processor
                pass
