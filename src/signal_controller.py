"""Signal controller for auto-trade execution based on Coinbase volatility signals."""

from typing import Optional

from .clob_client import FastClobClient
from .config import Config
from .order_executor import OrderExecutor
from .position_manager import PositionManager


class SignalController:
    """Controls automatic trade execution from volatility signals."""

    def __init__(
        self,
        clob_client: FastClobClient,
        config: Config,
        order_executor: OrderExecutor,
        position_manager: PositionManager,
    ):
        """
        Initialize signal controller.

        Args:
            clob_client: CLOB client for spread checks
            config: Configuration object
            order_executor: Order executor for entries
            position_manager: Position manager for position checks
        """
        self.clob_client = clob_client
        self.config = config
        self.order_executor = order_executor
        self.position_manager = position_manager

        self._auto_enabled = True

    def handle_signal(self, direction: str) -> bool:
        """
        Handle a volatility signal.

        Args:
            direction: "UP" or "DOWN"

        Returns:
            True if order was executed, False if skipped
        """
        # Check 1: Auto-signals enabled
        if not self._auto_enabled:
            return False

        # Check 2: No existing position in this direction
        token_id = (
            self.config.up_token_id if direction == "UP"
            else self.config.down_token_id
        )
        for pos in self.position_manager.list_open_positions():
            if pos.token_id == token_id:
                return False

        # Check 3: Spread within max_spread_pct
        if not self._check_spread(token_id):
            return False

        # Execute entry
        result = self.order_executor.execute_entry(direction)
        return result.success

    def _check_spread(self, token_id: str) -> bool:
        """
        Check if spread is within acceptable range.

        Args:
            token_id: Token to check

        Returns:
            True if spread is acceptable
        """
        best_bid = self.clob_client.get_best_bid(token_id)
        best_ask = self.clob_client.get_best_ask(token_id)

        if best_bid is None or best_ask is None:
            return False

        if best_bid <= 0:
            return False

        spread_pct = (best_ask - best_bid) / best_bid
        return spread_pct <= self.config.max_spread_pct

    def enable_auto(self):
        """Enable automatic signal handling."""
        self._auto_enabled = True

    def disable_auto(self):
        """Disable automatic signal handling."""
        self._auto_enabled = False

    @property
    def is_enabled(self) -> bool:
        return self._auto_enabled
