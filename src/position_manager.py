"""Position manager for tracking open positions and TP/SL exits."""

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Optional

from termcolor import colored

from .clob_client import FastClobClient
from .config import Config


class PositionStatus(Enum):
    OPEN = "OPEN"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"


class ExitReason(Enum):
    TAKE_PROFIT = "TAKE_PROFIT"
    STOP_LOSS = "STOP_LOSS"
    STALE_BREAKEVEN = "STALE_BREAKEVEN"
    MANUAL = "MANUAL"


@dataclass
class Position:
    """Represents an open trading position."""

    id: str
    direction: str  # "UP" or "DOWN"
    token_id: str
    entry_price: float
    shares: float
    entry_time: datetime
    take_profit_price: float
    stop_loss_price: float
    status: PositionStatus = PositionStatus.OPEN
    exit_price: Optional[float] = None
    exit_time: Optional[datetime] = None
    exit_reason: Optional[ExitReason] = None

    # Stale position tracking for trailing stop
    peak_price_since_entry: Optional[float] = None  # Highest price seen
    is_stale: bool = False  # True after stale_position_sec without TP
    stale_since_ms: Optional[float] = None  # When position became stale

    @property
    def cost_basis(self) -> float:
        """Total cost of the position."""
        return self.entry_price * self.shares

    def pnl(self, current_price: float) -> float:
        """Calculate unrealized P&L at current price."""
        return (current_price - self.entry_price) * self.shares

    def pnl_pct(self, current_price: float) -> float:
        """Calculate unrealized P&L percentage."""
        return ((current_price - self.entry_price) / self.entry_price) * 100


class PositionManager:
    """Manages open positions and triggers TP/SL exits."""

    def __init__(
        self,
        clob_client: FastClobClient,
        config: Config,
        on_exit_complete: Optional[Callable[[Position, ExitReason], None]] = None,
        data_logger: Optional[object] = None,
    ):
        self.clob_client = clob_client
        self.config = config
        self.on_exit_complete = on_exit_complete
        self.data_logger = data_logger

        self.positions: dict[str, Position] = {}
        self._exit_lock = asyncio.Lock()

    def add_position(
        self,
        direction: str,
        token_id: str,
        entry_price: float,
        shares: float,
        entry_bid: float = None,
    ) -> Position:
        """
        Create and track a new position.

        Args:
            direction: "UP" or "DOWN"
            token_id: Token ID
            entry_price: Price we paid (ASK)
            shares: Number of shares
            entry_bid: Current BID at entry time; used for SL only. TP is from entry_price.
        """
        position_id = str(uuid.uuid4())[:8]

        # TP from entry_price (ask): target is above what we paid
        tp_price = round(entry_price * (1 + self.config.take_profit_pct), 2)

        # SL from bid: triggers when bid drops below threshold
        sl_reference = entry_bid if entry_bid and entry_bid > 0 else entry_price
        sl_price = round(sl_reference * (1 - self.config.stop_loss_pct), 2)

        # Guards: TP at least one tick above entry; SL at least one tick below reference
        if tp_price <= entry_price:
            tp_price = round(entry_price + 0.01, 2)
        if sl_price >= sl_reference:
            sl_price = round(sl_reference - 0.01, 2)

        position = Position(
            id=position_id,
            direction=direction,
            token_id=token_id,
            entry_price=entry_price,
            shares=shares,
            entry_time=datetime.now(timezone.utc),
            take_profit_price=tp_price,
            stop_loss_price=sl_price,
        )

        self.positions[position_id] = position
        return position

    def get_position(self, position_id: str) -> Optional[Position]:
        """Get position by ID."""
        return self.positions.get(position_id)

    def list_open_positions(self) -> list[Position]:
        """Return list of open positions."""
        return [p for p in self.positions.values() if p.status == PositionStatus.OPEN]

    def check_exit_conditions(self, token_id: str, current_bid: float):
        """
        Check if any position should exit.
        Called on every price update (silently).

        Exit conditions checked:
        1. Take profit: current_bid >= take_profit_price
        2. Stop loss: current_bid <= stop_loss_price
        3. Stale breakeven: position is stale AND current_bid >= entry_price
           (If stale but underwater, wait for recovery to breakeven or stop loss)
        """
        if current_bid is None:
            return

        now_ms = time.time() * 1000

        for pos in list(self.positions.values()):
            if pos.token_id != token_id or pos.status != PositionStatus.OPEN:
                continue

            # Check take profit
            if current_bid >= pos.take_profit_price:
                asyncio.create_task(
                    self._async_trigger_exit(pos, ExitReason.TAKE_PROFIT, current_bid)
                )
                continue

            # Check stop loss
            if current_bid <= pos.stop_loss_price:
                asyncio.create_task(
                    self._async_trigger_exit(pos, ExitReason.STOP_LOSS, current_bid)
                )
                continue

            # Check if position became stale (no TP within stale_position_sec)
            entry_time_ms = pos.entry_time.timestamp() * 1000
            position_age_sec = (now_ms - entry_time_ms) / 1000.0

            if not pos.is_stale and position_age_sec >= self.config.stale_position_sec:
                pos.is_stale = True
                pos.stale_since_ms = now_ms

            # Stale breakeven exit: if stale AND can exit at breakeven or better
            # If underwater, wait for price to recover or hit stop loss
            # Note: entry_price is the ASK we paid, but we sell at BID. We need
            # bid >= entry + 1 tick to actually break even after spread cost.
            if pos.is_stale and current_bid >= pos.entry_price + 0.01:
                asyncio.create_task(
                    self._async_trigger_exit(pos, ExitReason.STALE_BREAKEVEN, current_bid)
                )

    async def _async_trigger_exit(
        self,
        position: Position,
        reason: ExitReason,
        trigger_price: float,
    ):
        """Async wrapper for trigger_exit to run in task."""
        async with self._exit_lock:
            if position.status != PositionStatus.OPEN:
                return  # Already being closed
            position.status = PositionStatus.CLOSING  # prevent re-entry
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self.trigger_exit, position, reason, trigger_price)

    # Dust threshold: remaining shares worth less than this are not worth
    # aggressively retrying via FAK. A GTC limit sell is placed instead.
    DUST_THRESHOLD_DOLLARS = 0.05

    def trigger_exit(self, position: Position, reason: ExitReason, trigger_bid: Optional[float] = None):
        """
        Execute exit order with aggressive retry.

        Phase 1: Fast retry loop (no delay between attempts).
        Phase 2: If remaining shares are above dust threshold, persistent
                 cleanup with 1s delays. If below dust threshold, place a
                 GTC limit sell and move on immediately.

        Logs each partial fill inline as it happens.
        """
        if position.status == PositionStatus.CLOSED:
            return

        position.status = PositionStatus.CLOSING

        # Capture spread at trigger time for data logging
        spread_at_trigger = self._get_spread_snapshot(position.token_id)

        remaining = position.shares
        total_filled = 0.0
        fill_prices = []

        # Log trigger and sell intent
        timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
        reason_colors = {"TAKE_PROFIT": "green", "STOP_LOSS": "red", "STALE_BREAKEVEN": "cyan", "MANUAL": "yellow"}
        print(colored(
            f"[{timestamp}] {reason.value} triggered",
            reason_colors.get(reason.value, "white"),
        ))
        print(f"[{timestamp}] Placing Sell {position.direction} order...")

        # --- Phase 1: Fast aggressive retry ---
        remaining, total_filled, fill_prices = self._exit_sell_loop(
            position.token_id, remaining, total_filled, fill_prices,
            direction=position.direction,
            max_attempts=20, max_consecutive_failures=5, delay_on_fail=0.3,
        )

        # --- Phase 2 or Dust cleanup ---
        if remaining > 0.01:
            best_bid = self.clob_client.get_best_bid(position.token_id) or 0
            remaining_value = remaining * best_bid

            if remaining_value >= self.DUST_THRESHOLD_DOLLARS:
                # Worth retrying aggressively
                remaining, total_filled, fill_prices = self._exit_sell_loop(
                    position.token_id, remaining, total_filled, fill_prices,
                    direction=position.direction,
                    max_attempts=15, max_consecutive_failures=10, delay_on_fail=1.0,
                )

            # If still remaining after phase 2 (or skipped phase 2 for dust),
            # place a GTC limit sell so it fills eventually
            if remaining > 0.01 and best_bid > 0:
                self._place_dust_gtc(position, remaining, best_bid)

        # --- Finalize position ---
        if total_filled > 0:
            # Calculate weighted average exit price
            total_value = sum(qty * price for qty, price in fill_prices)
            position.exit_price = total_value / total_filled
            # Update shares to reflect what was actually sold
            position.shares = total_filled
        else:
            # No fills at all - use current best bid for P&L calculation
            position.exit_price = self.clob_client.get_best_bid(position.token_id) or 0

        position.status = PositionStatus.CLOSED
        position.exit_time = datetime.now(timezone.utc)
        position.exit_reason = reason

        # Capture spread at fill time and log exit event
        spread_at_fill = self._get_spread_snapshot(position.token_id)
        self._log_exit(position, reason, fill_prices, spread_at_trigger, spread_at_fill, trigger_bid)

        # Notify callback (prints P&L summary)
        if self.on_exit_complete:
            self.on_exit_complete(position, reason)

    def _get_spread_snapshot(self, token_id: str) -> Optional[dict]:
        """Get Polymarket bid/ask/spread for logging."""
        best_bid = self.clob_client.get_best_bid(token_id)
        best_ask = self.clob_client.get_best_ask(token_id)
        if best_bid is None or best_ask is None or best_bid <= 0:
            return None
        return {
            "token_id": token_id,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "spread_cents": round((best_ask - best_bid) * 100),
        }

    def _log_exit(
        self,
        position: Position,
        reason: ExitReason,
        fill_prices: list[tuple[float, float]],
        spread_at_trigger: Optional[dict],
        spread_at_fill: Optional[dict],
        trigger_bid: Optional[float] = None,
    ) -> None:
        """Log exit event to data logger (non-blocking)."""
        if not self.data_logger:
            return
        pnl_dollars = 0.0
        pnl_pct = 0.0
        if position.exit_price and position.entry_price > 0:
            pnl_dollars = (position.exit_price - position.entry_price) * position.shares
            pnl_pct = ((position.exit_price - position.entry_price) / position.entry_price) * 100
        fill_details = [{"qty": q, "price": p} for q, p in fill_prices]
        event = {
            "type": "exit",
            "position_id": position.id,
            "direction": position.direction,
            "token_id": position.token_id,
            "reason": reason.value,
            "entry_price": position.entry_price,
            "exit_price": position.exit_price,
            "shares": position.shares,
            "pnl_dollars": pnl_dollars,
            "pnl_pct": pnl_pct,
            "fill_details": fill_details,
            "polymarket_spread_at_trigger": spread_at_trigger,
            "polymarket_spread_at_fill": spread_at_fill,
        }
        if trigger_bid is not None:
            event["trigger_bid"] = trigger_bid
        self.data_logger.log(event)

    def _place_dust_gtc(self, position: Position, remaining: float, best_bid: float):
        """Place a GTC limit sell for dust shares that couldn't be sold via FAK."""
        timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
        dust_value = remaining * best_bid

        result = self.clob_client.place_limit_sell(
            position.token_id,
            remaining,
            best_bid,
        )

        if result.get("success"):
            sell_price = result.get("price", best_bid)
            print(colored(
                f"[{timestamp}] Placed GTC sell for {remaining:.2f} shares "
                f"{position.direction} at ${sell_price:.2f} (~${dust_value:.2f} dust)",
                "yellow",
            ))
        else:
            error = result.get("errorMsg", "unknown")
            print(colored(
                f"[{timestamp}] Could not place GTC for {remaining:.2f} dust shares: {error}",
                "red",
            ))

    def _exit_sell_loop(
        self,
        token_id: str,
        remaining: float,
        total_filled: float,
        fill_prices: list[tuple[float, float]],
        direction: str = "",
        max_attempts: int = 20,
        max_consecutive_failures: int = 5,
        delay_on_fail: float = 0.3,
    ) -> tuple[float, float, list[tuple[float, float]]]:
        """
        Attempt to sell remaining shares in a retry loop.
        Logs each fill and retry inline.

        Args:
            token_id: Token to sell
            remaining: Shares still to sell
            total_filled: Running total of filled shares (accumulated across phases)
            fill_prices: Running list of (qty, price) tuples
            direction: "UP" or "DOWN" for log messages
            max_attempts: Max sell attempts
            max_consecutive_failures: Give up after this many consecutive failures
            delay_on_fail: Seconds to sleep between failed attempts

        Returns:
            Updated (remaining, total_filled, fill_prices)
        """
        attempt = 0
        consecutive_failures = 0

        while remaining > 0.01 and attempt < max_attempts and consecutive_failures < max_consecutive_failures:
            attempt += 1

            # Get fresh best bid each attempt
            best_bid = self.clob_client.get_best_bid(token_id)

            if best_bid is None or best_bid <= 0:
                consecutive_failures += 1
                time.sleep(delay_on_fail)
                continue

            result = self.clob_client.place_market_sell(
                token_id,
                remaining,
                price=best_bid,
            )

            if result.get("success"):
                filled = result.get("filled", 0.0)
                fill_price = result.get("price", best_bid)

                if filled > 0:
                    total_filled += filled
                    fill_prices.append((filled, fill_price))
                    remaining -= filled
                    consecutive_failures = 0  # Reset on any fill

                    # Log the fill
                    timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
                    print(colored(
                        f"[{timestamp}] Sold {filled:.2f} shares {direction} at ${fill_price:.2f}",
                        "cyan",
                    ))

                    # If partial, log retry and immediately try again
                    if remaining > 0.01:
                        print(colored(
                            f"[{timestamp}] {remaining:.2f} shares unfilled, retrying Sell {direction} order...",
                            "yellow",
                        ))
                        continue
                else:
                    consecutive_failures += 1
                    time.sleep(delay_on_fail)
            else:
                consecutive_failures += 1
                time.sleep(delay_on_fail)

        return remaining, total_filled, fill_prices

    async def manual_exit(self, position_id: str) -> bool:
        """
        Manually close a position.

        Returns:
            True if exit was initiated, False if position not found or already closing
        """
        position = self.positions.get(position_id)
        if not position or position.status != PositionStatus.OPEN:
            return False

        position.status = PositionStatus.CLOSING
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self.trigger_exit, position, ExitReason.MANUAL, None)
        return True

    def get_total_pnl(self) -> float:
        """Calculate total realized P&L from closed positions."""
        total = 0.0
        for pos in self.positions.values():
            if pos.status == PositionStatus.CLOSED and pos.exit_price:
                total += (pos.exit_price - pos.entry_price) * pos.shares
        return total

    def get_position_summary(self, position: Position, current_bid: Optional[float] = None) -> str:
        """Get formatted summary string for a position."""
        if current_bid is None:
            current_bid = self.clob_client.get_best_bid(position.token_id)

        if current_bid:
            pnl = position.pnl(current_bid)
            pnl_pct = position.pnl_pct(current_bid)
            pnl_color = "green" if pnl >= 0 else "red"

            return (
                f"{position.direction} {position.shares:.2f} @ ${position.entry_price:.2f} "
                f"-> ${current_bid:.2f} "
                f"({colored(f'{pnl_pct:+.1f}%', pnl_color)})"
            )
        else:
            return (
                f"{position.direction} {position.shares:.2f} @ ${position.entry_price:.2f} "
                f"(price unavailable)"
            )
