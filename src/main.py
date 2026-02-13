"""Main terminal UI and keyboard handler for Polymarket trading bot."""

import asyncio
import sys
import termios
import threading
import tty
from datetime import datetime, timezone
from typing import Optional

from termcolor import colored

from .clob_client import FastClobClient
from .coinbase_feed import CoinbaseFeed
from .config import Config
from .data_logger import DataLogger
from .order_executor import OrderExecutor
from .position_manager import ExitReason, Position, PositionManager
from .price_cache import PriceCache
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
        self.data_logger = DataLogger()

        self._running = False
        self._shutdown_done = False
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
            self.clob_client.warm_up()  # Pre-establish HTTP/2 connection
        except Exception as e:
            print(colored(f"CLOB client error: {e}", "red"))
            sys.exit(1)

        # Initialize price cache (WebSocket keeps it fresh; used by executor, signal controller, and position manager)
        self.price_cache = PriceCache(stale_ms=self.config.price_cache_stale_ms)

        # Initialize position manager with exit callback
        self.position_manager = PositionManager(
            self.clob_client,
            self.config,
            on_exit_complete=self._on_exit_complete,
            data_logger=self.data_logger,
            price_cache=self.price_cache,
        )

        # Initialize order executor
        self.order_executor = OrderExecutor(
            self.clob_client,
            self.config,
            self.position_manager,
            price_cache=self.price_cache,
            data_logger=self.data_logger,
        )

        # Initialize price stream
        token_ids = [self.config.up_token_id, self.config.down_token_id]
        self.price_stream = PriceStream(
            token_ids,
            on_price_update=self._on_price_update,
            on_connect=self._on_ws_connect,
            on_disconnect=self._on_ws_disconnect,
            price_cache=self.price_cache,
        )

        # Initialize signal controller
        self.signal_controller = SignalController(
            self.clob_client,
            self.config,
            self.order_executor,
            self.position_manager,
            price_cache=self.price_cache,
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
        print(f"  Max spread: {self.config.max_spread_cents}c")

        # Log session start for data capture
        self.data_logger.log({
            "type": "session_start",
            "config": {
                "position_size": self.config.position_size,
                "take_profit_pct": self.config.take_profit_pct,
                "stop_loss_pct": self.config.stop_loss_pct,
                "trigger_threshold": self.config.trigger_threshold,
                "signal_cooldown_ms": self.config.signal_cooldown_ms,
                "volatility_window_ms": self.config.volatility_window_ms,
                "max_spread_cents": self.config.max_spread_cents,
                "stale_position_sec": self.config.stale_position_sec,
                "price_cache_stale_ms": self.config.price_cache_stale_ms,
            },
            "market": {
                "event_title": self.config.event_title,
                "up_token_id": self.config.up_token_id,
                "down_token_id": self.config.down_token_id,
            },
        })

        print(colored("Initialization complete.", "green"))

    def _get_spread_snapshot(self, token_id: str) -> dict | None:
        """Get Polymarket bid/ask/spread for logging."""
        if not self.price_cache:
            return None
        snapshot = self.price_cache.get(token_id)
        if not snapshot or snapshot.best_bid is None or snapshot.best_ask is None:
            return None
        spread_cents = round((snapshot.best_ask - snapshot.best_bid) * 100) if snapshot.best_bid > 0 else None
        return {
            "token_id": token_id,
            "best_bid": snapshot.best_bid,
            "best_ask": snapshot.best_ask,
            "spread_cents": spread_cents,
        }

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

    async def _on_coinbase_signal(self, direction: str):
        """Handle volatility signal from Coinbase feed."""
        timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
        print(colored(f"[{timestamp}] AUTO-SIGNAL: {direction}", "magenta"))

        token_id = (
            self.config.up_token_id if direction == "UP"
            else self.config.down_token_id
        )

        # Get signal data and Polymarket spread snapshot for logging
        signal_data = getattr(self.coinbase_feed, "_last_signal_data", None)
        spread_snapshot = self._get_spread_snapshot(token_id)

        # Determine outcome
        outcome = "executed"
        if not self.signal_controller.is_enabled:
            outcome = "skipped_disabled"
            print(colored(f"[{timestamp}] Skipped: auto-signals disabled", "yellow"))
        elif any(pos.token_id == token_id for pos in self.position_manager.list_open_positions()):
            outcome = "skipped_position_open"
            print(colored(f"[{timestamp}] Skipped: position already open", "yellow"))
        elif not self.signal_controller._check_spread(token_id):
            outcome = "skipped_spread_wide"
            print(colored(f"[{timestamp}] Skipped: spread too wide", "yellow"))

        # Log signal event (always, including skipped)
        self.data_logger.log({
            "type": "signal",
            "direction": direction,
            "outcome": outcome,
            "pct_change": signal_data["pct_change"] if signal_data else None,
            "threshold": self.config.trigger_threshold,
            "window_ticks": signal_data["window_ticks"] if signal_data else [],
            "signal_time_ms": signal_data["signal_time_ms"] if signal_data else None,
            "polymarket_spread": spread_snapshot,
        })

        if outcome != "executed":
            return

        # Execute entry in thread executor (non-blocking for event loop)
        loop = self._loop or asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None, self.order_executor.execute_entry, direction
        )
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

    def _on_key_press(self, char: str):
        """Handle keyboard input."""
        if not char:
            return

        if self._in_exit_menu:
            self._handle_exit_menu_input(char)
            return

        if char == "u":
            asyncio.run_coroutine_threadsafe(
                self._place_entry_async("UP"),
                self._loop,
            )
        elif char == "d":
            asyncio.run_coroutine_threadsafe(
                self._place_entry_async("DOWN"),
                self._loop,
            )
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
                future = asyncio.run_coroutine_threadsafe(
                    self.order_executor.execute_exit(position.id),
                    self._loop,
                )
                try:
                    if not future.result(timeout=30):
                        print(colored("Failed to initiate exit", "red"))
                except Exception:
                    print(colored("Failed to initiate exit", "red"))
            else:
                print(colored(f"Invalid selection: {selection}", "yellow"))
        except ValueError:
            pass

    async def _place_entry_async(self, direction: str):
        """Place an entry order (runs on event loop, entry I/O in executor)."""
        timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
        print(f"[{timestamp}] Placing Buy {direction} order...")

        loop = self._loop or asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None, self.order_executor.execute_entry, direction
        )
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

        # Trade stats
        stats = self.position_manager.get_trade_stats()
        if stats["total"] > 0:
            wr_color = "green" if stats["win_rate"] >= 50 else "red"
            print(f"  Trades: {stats['total']}  |  W: {stats['wins']}  L: {stats['losses']}  BE: {stats['breakevens']}")
            print(colored(f"  Win rate: {stats['win_rate']:.1f}%", wr_color))

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
        """Shutdown the bot (idempotent)."""
        if self._shutdown_done:
            return
        self._shutdown_done = True

        timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
        print(colored(f"\n[{timestamp}] Shutting down...", "yellow"))

        self._running = False
        if self.price_stream:
            self.price_stream.stop()
        if self.coinbase_feed:
            self.coinbase_feed.stop()

        # Log session end and close data logger
        if self.position_manager:
            realized = self.position_manager.get_total_pnl()
            open_positions = self.position_manager.list_open_positions()
            trade_count = len([p for p in self.position_manager.positions.values() if p.exit_reason is not None])
            self.data_logger.log({
                "type": "session_end",
                "total_realized_pnl": realized,
                "trade_count": trade_count,
                "open_positions_remaining": len(open_positions),
            })
        self.data_logger.close()

    def _stdin_reader(self):
        """Read single keystrokes from stdin in raw mode (runs in thread)."""
        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            while self._running:
                ch = sys.stdin.read(1)
                if ch:
                    self._on_key_press(ch)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

    async def run_async(self):
        """Run the bot asynchronously."""
        self._running = True
        self._loop = asyncio.get_event_loop()

        # Start keyboard reader in separate thread
        reader_thread = threading.Thread(target=self._stdin_reader, daemon=True)
        reader_thread.start()

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

    def run(self):
        """Run the bot (blocking)."""
        self.initialize()

        try:
            asyncio.run(self.run_async())
        except KeyboardInterrupt:
            pass  # _shutdown already called via 'q' or will be called below
        finally:
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
