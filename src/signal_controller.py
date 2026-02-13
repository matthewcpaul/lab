"""Signal controller for auto-trade execution based on Coinbase volatility signals."""

from typing import TYPE_CHECKING, Optional

from .clob_client import FastClobClient
from .config import Config
from .order_executor import OrderExecutor
from .position_manager import PositionManager

if TYPE_CHECKING:
    from .price_cache import PriceCache


class SignalController:
    """Controls automatic trade execution from volatility signals."""

    def __init__(
        self,
        clob_client: FastClobClient,
        config: Config,
        order_executor: OrderExecutor,
        position_manager: PositionManager,
        price_cache: Optional["PriceCache"] = None,
    ):
        """
        Initialize signal controller.

        Args:
            clob_client: CLOB client for spread checks (fallback)
            config: Configuration object
            order_executor: Order executor for entries
            position_manager: Position manager for position checks
            price_cache: Optional shared PriceCache for low-latency spread checks
        """
        self.clob_client = clob_client
        self.config = config
        self.order_executor = order_executor
        self.position_manager = position_manager
        self.price_cache = price_cache

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

        # Check 3: Spread within max_spread_cents
        if not self._check_spread(token_id):
            return False

        # Execute entry
        result = self.order_executor.execute_entry(direction)
        return result.success

    def _check_spread(self, token_id: str) -> bool:
        """
        Check if spread is within acceptable range.

        Uses PriceCache for low-latency checks (no REST call).
        Falls back to CLOB client REST API if cache is unavailable or stale.

        Args:
            token_id: Token to check

        Returns:
            True if spread is acceptable
        """
        # Try price cache first (no REST call, ~0ms latency)
        if self.price_cache is not None:
            return self.price_cache.is_spread_acceptable(token_id, self.config.max_spread_cents)

        # Fallback to REST API (100-200ms latency)
        best_bid = self.clob_client.get_best_bid(token_id)
        best_ask = self.clob_client.get_best_ask(token_id)

        if best_bid is None or best_ask is None:
            return False

        if best_bid <= 0:
            return False

        spread = best_ask - best_bid
        return spread <= self.config.max_spread_cents * 0.01

    def enable_auto(self):
        """Enable automatic signal handling."""
        self._auto_enabled = True

    def disable_auto(self):
        """Disable automatic signal handling."""
        self._auto_enabled = False

    @property
    def is_enabled(self) -> bool:
        return self._auto_enabled
