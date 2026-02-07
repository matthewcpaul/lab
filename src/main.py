"""Main terminal UI and keyboard handler for Polymarket trading bot."""

import asyncio
import sys
import threading
from datetime import datetime, timezone
from typing import Optional

from pynput import keyboard
from termcolor import colored

from .clob_client import FastClobClient
from .coinbase_feed import CoinbaseFeed
from .config import Config
from .order_executor import OrderExecutor
from .position_manager import ExitReason, Position, PositionManager
from .signal_controller import SignalController
from .websocket_client import PriceStream


class TradingBot:
    """Main trading bot with terminal UI."""

    def __init__(self):
        self.config: Optional[Config] = None
        self.clob_client: Optional[FastClobClient] = None
        self.position_manager: Optional[PositionManager] = None
        self.order_executor: Optional[OrderExecutor] = None
        self.price_stream: Optional[PriceStream] = None
        self.coinbase_feed: Optional[CoinbaseFeed] = None
        self.signal_controller: Optional[SignalController] = None

        self._running = False
        self._in_exit_menu = False
        self._exit_menu_positions: list[Position] = []
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def initialize(self):
        """Initialize all components."""
        print(colored("Initializing trading bot...", "cyan"))

        # Load configuration
        try:
            self.config = Config()
            self.config.load_market()
        except Exception as e:
            print(colored(f"Configuration error: {e}", "red"))
            sys.exit(1)

        print(f"  Market: {self.config.event_title}")
        print(f"  Position size: ${self.config.position_size:.2f}")
        print(f"  Take profit: {self.config.take_profit_pct * 100:.1f}%")
        print(f"  Stop loss: {self.config.stop_loss_pct * 100:.1f}%")

        # Initialize CLOB client
        try:
            self.clob_client = FastClobClient(self.config)
        except Exception as e:
            print(colored(f"CLOB client error: {e}", "red"))
            sys.exit(1)

        # Initialize position manager with exit callback
        self.position_manager = PositionManager(
            self.clob_client,
            self.config,
            on_exit_complete=self._on_exit_complete,
        )

        # Initialize order executor
        self.order_executor = OrderExecutor(
            self.clob_client,
            self.config,
            self.position_manager,
        )

        # Initialize price stream
        token_ids = [self.config.up_token_id, self.config.down_token_id]
        self.price_stream = PriceStream(
            token_ids,
            on_price_update=self._on_price_update,
            on_connect=self._on_ws_connect,
            on_disconnect=self._on_ws_disconnect,
        )

        # Initialize signal controller
        self.signal_controller = SignalController(
            self.clob_client,
            self.config,
            self.order_executor,
            self.position_manager,
        )

        # Initialize Coinbase feed for BTC volatility detection
        self.coinbase_feed = CoinbaseFeed(
            window_ms=self.config.volatility_window_ms,
            threshold=self.config.trigger_threshold,
            cooldown_ms=self.config.signal_cooldown_ms,
            on_signal=self._on_coinbase_signal,
            on_connect=self._on_coinbase_connect,
            on_disconnect=self._on_coinbase_disconnect,
        )

        print(f"  Trigger threshold: {self.config.trigger_threshold * 100:.3f}%")
        print(f"  Signal cooldown: {self.config.signal_cooldown_ms}ms")
        print(f"  Volatility window: {self.config.volatility_window_ms}ms")
        print(f"  Max spread: {self.config.max_spread_pct * 100:.1f}%")

        print(colored("Initialization complete.", "green"))

    def _on_price_update(self, token_id: str, best_bid: float, best_ask: float):
        """Handle price update from WebSocket (silent - only triggers TP/SL checks)."""
        if best_bid is not None:
            self.position_manager.check_exit_conditions(token_id, best_bid)

    def _on_ws_connect(self):
        """Handle WebSocket connection."""
        timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
        print(colored(f"[{timestamp}] WebSocket connected", "green"))

    def _on_ws_disconnect(self):
        """Handle WebSocket disconnection."""
        timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
        print(colored(f"[{timestamp}] WebSocket disconnected, reconnecting...", "yellow"))

    def _on_coinbase_signal(self, direction: str):
        """Handle volatility signal from Coinbase feed."""
        timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
        print(colored(f"[{timestamp}] AUTO-SIGNAL: {direction}", "magenta"))

        # Check conditions via signal controller (but don't execute yet)
        if not self.signal_controller.is_enabled:
            return

        # Check position limit
        token_id = (
            self.config.up_token_id if direction == "UP"
            else self.config.down_token_id
        )
        for pos in self.position_manager.list_open_positions():
            if pos.token_id == token_id:
                print(colored(f"[{timestamp}]   Skipped: position already open", "yellow"))
                return

        # Check spread
        if not self.signal_controller._check_spread(token_id):
            print(colored(f"[{timestamp}]   Skipped: spread too wide", "yellow"))
            return

        # Execute entry and show result
        result = self.order_executor.execute_entry(direction)
        output = self.order_executor.format_entry_result(result)
        print(output)

    def _on_coinbase_connect(self):
        """Handle Coinbase WebSocket connection."""
        timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
        print(colored(f"[{timestamp}] Coinbase feed connected", "green"))

    def _on_coinbase_disconnect(self):
        """Handle Coinbase WebSocket disconnection."""
        timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
        print(colored(f"[{timestamp}] Coinbase feed disconnected, reconnecting...", "yellow"))

    def _toggle_auto_signals(self):
        """Toggle automatic signal handling (kill switch)."""
        timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S")

        if self.signal_controller.is_enabled:
            self.signal_controller.disable_auto()
            self.coinbase_feed.pause()
            print(colored(f"[{timestamp}] Auto-signals PAUSED (manual u/d still works)", "yellow"))
        else:
            self.signal_controller.enable_auto()
            self.coinbase_feed.resume()
            print(colored(f"[{timestamp}] Auto-signals RESUMED", "green"))

    def _on_exit_complete(self, position: Position, reason: ExitReason):
        """Handle position exit completion."""
        output = self.order_executor.format_exit_result(position, reason.value)
        print(output)

    def _on_key_press(self, key):
        """Handle keyboard input."""
        try:
            char = key.char
        except AttributeError:
            return

        if self._in_exit_menu:
            self._handle_exit_menu_input(char)
            return

        if char == "u":
            self._place_entry("UP")
        elif char == "d":
            self._place_entry("DOWN")
        elif char == "k":
            self._toggle_auto_signals()
        elif char == "x":
            self._show_exit_menu()
        elif char == "q":
            self._shutdown()
        elif char == "s":
            self._show_status()

    def _handle_exit_menu_input(self, char: str):
        """Handle input while in exit menu."""
        if char == "0":
            self._in_exit_menu = False
            self._exit_menu_positions = []
            print("Cancelled.")
            return

        try:
            selection = int(char)
            if 1 <= selection <= len(self._exit_menu_positions):
                position = self._exit_menu_positions[selection - 1]
                self._in_exit_menu = False
                self._exit_menu_positions = []

                print(f"Closing position {position.id}...")
                if self.order_executor.execute_exit(position.id):
                    pass  # Exit callback will print result
                else:
                    print(colored("Failed to initiate exit", "red"))
            else:
                print(colored(f"Invalid selection: {selection}", "yellow"))
        except ValueError:
            pass

    def _place_entry(self, direction: str):
        """Place an entry order."""
        timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
        print(f"[{timestamp}] Placing Buy {direction} order...")

        result = self.order_executor.execute_entry(direction)
        output = self.order_executor.format_entry_result(result)
        print(output)

    def _show_exit_menu(self):
        """Display numbered menu of open positions for manual exit."""
        positions = self.position_manager.list_open_positions()

        if not positions:
            print(colored("No open positions to exit", "yellow"))
            return

        self._in_exit_menu = True
        self._exit_menu_positions = positions

        timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
        print(f"\n[{timestamp}] --- Open Positions ---")

        for i, pos in enumerate(positions, 1):
            summary = self.position_manager.get_position_summary(pos)
            print(f"  {i}. [{pos.id}] {summary}")

        print("  0. Cancel")
        print("Enter position number to exit: ", end="", flush=True)

    def _show_status(self):
        """Show current status and positions."""
        timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
        positions = self.position_manager.list_open_positions()

        print(f"\n[{timestamp}] --- Status ---")
        print(f"  Open positions: {len(positions)}")

        if positions:
            total_unrealized = 0.0
            for pos in positions:
                bid = self.clob_client.get_best_bid(pos.token_id)
                if bid:
                    total_unrealized += pos.pnl(bid)
                summary = self.position_manager.get_position_summary(pos)
                print(f"    [{pos.id}] {summary}")
            pnl_color = "green" if total_unrealized >= 0 else "red"
            print(colored(f"  Unrealized P&L: ${total_unrealized:+.2f}", pnl_color))

        realized = self.position_manager.get_total_pnl()
        if realized != 0:
            pnl_color = "green" if realized >= 0 else "red"
            print(colored(f"  Realized P&L: ${realized:+.2f}", pnl_color))

        # Show current prices
        up_bid = self.clob_client.get_best_bid(self.config.up_token_id)
        down_bid = self.clob_client.get_best_bid(self.config.down_token_id)
        print(f"  UP price:   ${up_bid:.2f}" if up_bid else "  UP price:   N/A")
        print(f"  DOWN price: ${down_bid:.2f}" if down_bid else "  DOWN price: N/A")

        # Show Coinbase feed state
        cb_connected = self.coinbase_feed.is_connected if self.coinbase_feed else False
        cb_paused = self.coinbase_feed.is_paused if self.coinbase_feed else True
        auto_enabled = self.signal_controller.is_enabled if self.signal_controller else False

        cb_status = "connected" if cb_connected else "disconnected"
        auto_status = "PAUSED" if not auto_enabled else "active"
        status_color = "green" if (cb_connected and auto_enabled) else "yellow"

        print(colored(f"  Coinbase: {cb_status} | Auto-signals: {auto_status}", status_color))
        print()

    def _shutdown(self):
        """Shutdown the bot."""
        timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
        print(colored(f"\n[{timestamp}] Shutting down...", "yellow"))

        self._running = False
        if self.price_stream:
            self.price_stream.stop()
        if self.coinbase_feed:
            self.coinbase_feed.stop()

    async def run_async(self):
        """Run the bot asynchronously."""
        self._running = True
        self._loop = asyncio.get_event_loop()

        # Start keyboard listener in separate thread
        listener = keyboard.Listener(on_press=self._on_key_press)
        listener.start()

        timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
        print(colored(
            f"\n[{timestamp}] Bot ready. Keys: 'u'=UP, 'd'=DOWN, 'k'=kill switch, "
            f"'x'=exit, 's'=status, 'q'=quit",
            "cyan",
        ))

        # Run both WebSocket feeds concurrently
        try:
            await asyncio.gather(
                self.price_stream.connect(),
                self.coinbase_feed.connect(),
            )
        except asyncio.CancelledError:
            pass
        finally:
            listener.stop()

    def run(self):
        """Run the bot (blocking)."""
        self.initialize()

        try:
            asyncio.run(self.run_async())
        except KeyboardInterrupt:
            self._shutdown()

        # Final summary
        print("\n--- Session Summary ---")
        realized = self.position_manager.get_total_pnl()
        pnl_color = "green" if realized >= 0 else "red"
        print(colored(f"Total P&L: ${realized:+.2f}", pnl_color))

        open_positions = self.position_manager.list_open_positions()
        if open_positions:
            print(colored(
                f"Warning: {len(open_positions)} position(s) still open!",
                "yellow",
            ))


def main():
    """Entry point for trading bot."""
    bot = TradingBot()
    bot.run()


if __name__ == "__main__":
    main()
