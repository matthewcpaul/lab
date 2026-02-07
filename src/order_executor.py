"""Fast order execution with retry logic and partial fill handling."""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from termcolor import colored

from .clob_client import FastClobClient
from .config import Config
from .position_manager import Position, PositionManager


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
    ):
        self.clob_client = clob_client
        self.config = config
        self.position_manager = position_manager

    def execute_entry(self, direction: str, dollar_amount: Optional[float] = None) -> OrderResult:
        """
        Execute a market entry order.

        Args:
            direction: "UP" or "DOWN"
            dollar_amount: Optional override for position size

        Returns:
            OrderResult with execution details
        """
        if dollar_amount is None:
            dollar_amount = self.config.position_size

        # Get token ID based on direction
        token_id = (
            self.config.up_token_id if direction == "UP" else self.config.down_token_id
        )

        # Get best ask for entry
        best_ask = self.clob_client.get_best_ask(token_id)
        if not best_ask:
            return OrderResult(
                success=False,
                direction=direction,
                requested_amount=dollar_amount,
                filled_shares=0.0,
                fill_price=0.0,
                error_msg="No asks available in orderbook",
            )

        # Place market buy order
        result = self.clob_client.place_market_buy(token_id, dollar_amount)

        if result.get("success"):
            filled_shares = result.get("filled", 0.0)
            fill_price = result.get("price", best_ask)

            if filled_shares > 0:
                # Create position with filled amount
                position = self.position_manager.add_position(
                    direction=direction,
                    token_id=token_id,
                    entry_price=fill_price,
                    shares=filled_shares,
                )

                return OrderResult(
                    success=True,
                    direction=direction,
                    requested_amount=dollar_amount,
                    filled_shares=filled_shares,
                    fill_price=fill_price,
                    position=position,
                    partial_fill=(filled_shares < dollar_amount / best_ask * 0.95),
                )
            else:
                return OrderResult(
                    success=False,
                    direction=direction,
                    requested_amount=dollar_amount,
                    filled_shares=0.0,
                    fill_price=0.0,
                    error_msg="Order submitted but no fill received",
                )
        else:
            return OrderResult(
                success=False,
                direction=direction,
                requested_amount=dollar_amount,
                filled_shares=0.0,
                fill_price=0.0,
                error_msg=result.get("errorMsg", "Unknown error"),
            )

    def execute_exit(self, position_id: str) -> bool:
        """
        Execute a manual exit for a position.

        Args:
            position_id: ID of position to exit

        Returns:
            True if exit was initiated
        """
        return self.position_manager.manual_exit(position_id)

    def format_entry_result(self, result: OrderResult) -> str:
        """Format entry result for display."""
        timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S")

        if result.success:
            pos = result.position
            lines = [
                colored(
                    f"[{timestamp}] Bought {result.filled_shares:.2f} shares {result.direction} "
                    f"at ${result.fill_price:.2f}",
                    "green",
                ),
            ]

            if pos:
                lines.append(
                    f"[{timestamp}] TP: ${pos.take_profit_price:.2f} | "
                    f"SL: ${pos.stop_loss_price:.2f}"
                )

            if result.partial_fill:
                lines.append(
                    colored(f"[{timestamp}]   (Partial fill - rest cancelled)", "yellow")
                )

            return "\n".join(lines)
        else:
            return colored(
                f"[{timestamp}] Order failed: {result.error_msg}",
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
                f"[{timestamp}]   P&L: {pnl_sign}${abs(pnl):.2f} ({pnl_pct:+.1f}%)",
                pnl_color,
            )
        else:
            return colored(
                f"[{timestamp}] Position {position.id} closed (no fill data)",
                "yellow",
            )
