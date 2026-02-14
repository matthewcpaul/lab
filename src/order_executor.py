"""Fast order execution with retry logic and partial fill handling."""

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

from termcolor import colored

from .clob_client import FastClobClient
from .config import Config
from .position_manager import Position, PositionManager

if TYPE_CHECKING:
    from .price_cache import PriceCache


@dataclass
class OrderResult:
    """Result of an order execution."""

    success: bool
    direction: str
    requested_amount: float
    filled_shares: float
    fill_price: float
    position: Optional[Position] = None
    error_msg: Optional[str] = None
    partial_fill: bool = False
    timing_ms: dict = field(default_factory=dict)
    position_id: Optional[str] = None
    limit_price: Optional[float] = None
    requested_shares: Optional[float] = None
    order_id: Optional[str] = None


class OrderExecutor:
    """
    Fast order execution with:
    - Immediate market orders
    - Partial fill acceptance on entry
    - Position tracking integration
    """

    def __init__(
        self,
        clob_client: FastClobClient,
        config: Config,
        position_manager: PositionManager,
        price_cache: Optional["PriceCache"] = None,
        data_logger: Optional[object] = None,
    ):
        self.clob_client = clob_client
        self.config = config
        self.position_manager = position_manager
        self.price_cache = price_cache
        self.data_logger = data_logger
        self.coinbase_feed = None  # Set after CoinbaseFeed init for btc_price logging

    def execute_entry(self, direction: str, dollar_amount: Optional[float] = None) -> OrderResult:
        """
        Execute a market entry order.

        Args:
            direction: "UP" or "DOWN"
            dollar_amount: Optional override for position size

        Returns:
            OrderResult with execution details
        """
        t_start = time.perf_counter()

        if dollar_amount is None:
            dollar_amount = self.config.position_size

        # Get token ID based on direction
        token_id = (
            self.config.up_token_id if direction == "UP" else self.config.down_token_id
        )

        # Get best ask for entry and best bid for TP/SL calculation
        # Use price cache for low-latency reads, fallback to REST
        t_cache = time.perf_counter()
        if self.price_cache is not None:
            best_ask = self.price_cache.get_best_ask(token_id)
            best_bid = self.price_cache.get_best_bid(token_id)
            age = self.price_cache.get_age_ms(token_id)
            age_str = f"{age:.0f}ms" if age is not None else "N/A"
            print(f"   [DEBUG] Prices from cache (age: {age_str})")
        else:
            best_ask = self.clob_client.get_best_ask(token_id)
            best_bid = self.clob_client.get_best_bid(token_id)
            print("   [DEBUG] Prices from REST (no cache)")
        cache_read_ms = (time.perf_counter() - t_cache) * 1000

        if not best_ask:
            total_ms = (time.perf_counter() - t_start) * 1000
            timing = {"cache_read_ms": round(cache_read_ms, 1), "total_ms": round(total_ms, 1)}
            order_result = OrderResult(
                success=False,
                direction=direction,
                requested_amount=dollar_amount,
                filled_shares=0.0,
                fill_price=0.0,
                error_msg="No asks available in orderbook",
                timing_ms=timing,
            )
            self._log_entry(order_result, token_id, best_bid, best_ask)
            return order_result

        # Pre-generate position ID for correlation across entry/exit logs
        position_id = str(uuid.uuid4())[:8]

        # Calculate the limit price that will be submitted to the exchange
        limit_price = best_ask
        if self.config.slippage_cents > 0:
            limit_price = min(best_ask + self.config.slippage_cents / 100, 0.99)

        # Place market buy order (pass cached price to avoid REST call)
        # The CLOB client internally does: create_order (ECDSA signing) + post_order (HTTP)
        t_order = time.perf_counter()
        result = self.clob_client.place_market_buy(
            token_id, dollar_amount, price=best_ask,
            slippage_cents=self.config.slippage_cents,
        )
        order_ms = (time.perf_counter() - t_order) * 1000

        # Capture order metadata for logging
        order_id = result.get("orderID")
        requested_shares_submitted = result.get("requested")

        # Parse response
        t_parse = time.perf_counter()
        if result.get("success"):
            filled_shares = result.get("filled", 0.0)
            fill_price = result.get("price", best_ask)

            if filled_shares > 0:
                # Create position with filled amount
                # Pass entry_bid for TP/SL calculation (what we'd get if we sold now)
                position = self.position_manager.add_position(
                    direction=direction,
                    token_id=token_id,
                    entry_price=fill_price,
                    shares=filled_shares,
                    entry_bid=best_bid,
                    position_id=position_id,
                )
                parse_ms = (time.perf_counter() - t_parse) * 1000
                total_ms = (time.perf_counter() - t_start) * 1000
                timing = {
                    "cache_read_ms": round(cache_read_ms, 1),
                    "order_ms": round(order_ms, 1),
                    "parse_ms": round(parse_ms, 1),
                    "total_ms": round(total_ms, 1),
                }

                order_result = OrderResult(
                    success=True,
                    direction=direction,
                    requested_amount=dollar_amount,
                    filled_shares=filled_shares,
                    fill_price=fill_price,
                    position=position,
                    partial_fill=(filled_shares < dollar_amount / best_ask * 0.95),
                    timing_ms=timing,
                    position_id=position_id,
                    limit_price=limit_price,
                    requested_shares=requested_shares_submitted,
                    order_id=order_id,
                )
                self._log_entry(order_result, token_id, best_bid, best_ask)
                return order_result
            else:
                parse_ms = (time.perf_counter() - t_parse) * 1000
                total_ms = (time.perf_counter() - t_start) * 1000
                timing = {
                    "cache_read_ms": round(cache_read_ms, 1),
                    "order_ms": round(order_ms, 1),
                    "parse_ms": round(parse_ms, 1),
                    "total_ms": round(total_ms, 1),
                }
                order_result = OrderResult(
                    success=False,
                    direction=direction,
                    requested_amount=dollar_amount,
                    filled_shares=0.0,
                    fill_price=0.0,
                    error_msg="Order submitted but no fill received",
                    timing_ms=timing,
                    position_id=position_id,
                    limit_price=limit_price,
                    requested_shares=requested_shares_submitted,
                    order_id=order_id,
                )
                self._log_entry(order_result, token_id, best_bid, best_ask)
                return order_result
        else:
            parse_ms = (time.perf_counter() - t_parse) * 1000
            total_ms = (time.perf_counter() - t_start) * 1000
            timing = {
                "cache_read_ms": round(cache_read_ms, 1),
                "order_ms": round(order_ms, 1),
                "parse_ms": round(parse_ms, 1),
                "total_ms": round(total_ms, 1),
            }
            order_result = OrderResult(
                success=False,
                direction=direction,
                requested_amount=dollar_amount,
                filled_shares=0.0,
                fill_price=0.0,
                error_msg=result.get("errorMsg", "Unknown error"),
                timing_ms=timing,
                position_id=position_id,
                limit_price=limit_price,
                requested_shares=requested_shares_submitted,
                order_id=order_id,
            )
            self._log_entry(order_result, token_id, best_bid, best_ask)
            return order_result

    def _log_entry(
        self,
        result: OrderResult,
        token_id: str,
        best_bid: Optional[float],
        best_ask: Optional[float],
    ) -> None:
        """Log entry event to data logger (non-blocking)."""
        if not self.data_logger:
            return
        spread_snapshot = None
        if best_bid is not None and best_ask is not None and best_bid > 0:
            spread_snapshot = {
                "token_id": token_id,
                "best_bid": best_bid,
                "best_ask": best_ask,
                "spread_cents": round((best_ask - best_bid) * 100),
            }
        # Get BTC price at fill time from Coinbase feed
        btc_price = None
        if self.coinbase_feed is not None:
            btc_price = self.coinbase_feed.latest_price or None

        self.data_logger.log({
            "type": "entry",
            "position_id": result.position_id,
            "direction": result.direction,
            "token_id": token_id,
            "requested_amount": result.requested_amount,
            "limit_price": result.limit_price,
            "requested_shares": result.requested_shares,
            "filled_shares": result.filled_shares,
            "fill_price": result.fill_price,
            "order_id": result.order_id,
            "partial_fill": result.partial_fill,
            "success": result.success,
            "error_msg": result.error_msg,
            "btc_price_at_fill": btc_price,
            "polymarket_spread": spread_snapshot,
            "timing_ms": result.timing_ms if result.timing_ms else None,
        })

    async def execute_exit(self, position_id: str) -> bool:
        """
        Execute a manual exit for a position.

        Args:
            position_id: ID of position to exit

        Returns:
            True if exit was initiated
        """
        return await self.position_manager.manual_exit(position_id)

    def format_entry_result(self, result: OrderResult) -> str:
        """Format entry result for display."""
        timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S")

        if result.success:
            pos = result.position
            lines = [
                colored(
                    f"[{timestamp}] Bought {result.filled_shares:.2f} shares {result.direction} "
                    f"at ${result.fill_price:.2f}",
                    "cyan",
                ),
            ]

            if pos:
                lines.append(
                    f"[{timestamp}] TP: ${pos.take_profit_price:.2f} | "
                    f"SL: ${pos.stop_loss_price:.2f}"
                )

            return "\n".join(lines)
        else:
            # Make FAK "no liquidity" errors concise and yellow (like spread warnings)
            error_msg = result.error_msg or "Unknown error"
            if "no orders found to match with FAK" in error_msg:
                return colored(
                    f"[{timestamp}] Skipped: no liquidity at price",
                    "yellow",
                )
            return colored(
                f"[{timestamp}] Order failed: {error_msg}",
                "red",
            )

    def format_exit_result(
        self,
        position: Position,
        reason: str,
    ) -> str:
        """Format exit P&L summary. Individual sells are logged inline by position_manager."""
        timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S")

        if position.exit_price:
            pnl = (position.exit_price - position.entry_price) * position.shares
            pnl_pct = ((position.exit_price - position.entry_price) / position.entry_price) * 100
            pnl_color = "green" if pnl >= 0 else "red"
            pnl_sign = "+" if pnl >= 0 else "-"

            return colored(
                f"[{timestamp}] P&L: {pnl_sign}${abs(pnl):.2f} ({pnl_pct:+.1f}%)",
                pnl_color,
            )
        else:
            return colored(
                f"[{timestamp}] Position {position.id} closed (no fill data)",
                "yellow",
            )
