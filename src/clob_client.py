"""CLOB API wrapper for Polymarket order operations."""

import time
from decimal import Decimal, ROUND_DOWN
from typing import Optional

import httpx
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType
from py_clob_client.constants import POLYGON
from py_clob_client.http_helpers import helpers as _clob_http_helpers

from .config import Config


class FastClobClient:
    """Fast CLOB client using L2 API authentication."""

    CLOB_HOST = "https://clob.polymarket.com"

    def __init__(self, config: Config):
        self.config = config

        # Initialize client with private key for signing
        # signature_type=2 (GNOSIS_SAFE) for MetaMask browser extension users
        self.client = ClobClient(
            host=self.CLOB_HOST,
            key=config.private_key,
            chain_id=POLYGON,
            funder=config.funder_address,
            signature_type=2,
        )

        # Derive API credentials from private key (ensures they match)
        self.client.set_api_creds(self.client.derive_api_key())

        # Replace the library's default httpx.Client with optimized settings.
        # Default keepalive_expiry is only 5s; after idle periods the connection
        # drops and each request pays ~300-400ms for TCP+TLS+HTTP/2 setup.
        # Setting keepalive_expiry=600 (10 min) keeps the connection warm.
        _clob_http_helpers._http_client = httpx.Client(
            http2=True,
            timeout=httpx.Timeout(10.0, connect=5.0),
            limits=httpx.Limits(
                max_keepalive_connections=5,
                max_connections=10,
                keepalive_expiry=600,
            ),
        )

    def warm_up(self):
        """Pre-establish the HTTP/2 connection to Polymarket.

        Makes a lightweight GET /time call so the TCP+TLS+HTTP/2 handshake
        happens before the first real trade, avoiding ~300-400ms extra latency
        on the very first order.
        """
        try:
            self.client.get_server_time()
        except Exception:
            pass  # Non-critical; connection will be established on first real call

    def get_order_book(self, token_id: str) -> dict:
        """Get full order book for a token."""
        return self.client.get_order_book(token_id)

    def get_best_ask(self, token_id: str) -> Optional[float]:
        """Get best ask price for entry orders."""
        book = self.get_order_book(token_id)
        # OrderBookSummary has asks/bids as attributes
        # IMPORTANT: asks are sorted descending (worst=0.99 to best=lowest)
        # So best ask (lowest price) is at the END of the array
        asks = book.asks if hasattr(book, "asks") else []
        if asks:
            best_ask = asks[-1]  # Last element = lowest price = best ask
            # Handle both object and dict formats
            if hasattr(best_ask, "price"):
                return float(best_ask.price)
            elif isinstance(best_ask, dict):
                return float(best_ask["price"])
        return None

    def get_best_bid(self, token_id: str) -> Optional[float]:
        """Get best bid price for exit orders."""
        book = self.get_order_book(token_id)
        # OrderBookSummary has asks/bids as attributes
        # IMPORTANT: bids are sorted ascending (worst=0.01 to best=highest)
        # So best bid (highest price) is at the END of the array
        bids = book.bids if hasattr(book, "bids") else []
        if bids:
            best_bid = bids[-1]  # Last element = highest price = best bid
            # Handle both object and dict formats
            if hasattr(best_bid, "price"):
                return float(best_bid.price)
            elif isinstance(best_bid, dict):
                return float(best_bid["price"])
        return None

    def get_midpoint(self, token_id: str) -> Optional[float]:
        """Get midpoint price."""
        best_ask = self.get_best_ask(token_id)
        best_bid = self.get_best_bid(token_id)
        if best_ask and best_bid:
            return (best_ask + best_bid) / 2
        return best_ask or best_bid

    def _clean_order_amounts(self, size: float, price: float) -> tuple[float, float]:
        """
        Ensure size × price has at most 2 decimal places.

        For market orders, maker_amount (size × price) must have ≤2 decimals.
        We adjust size down if necessary to satisfy this constraint.

        If no valid size exists at the given price (common with small amounts
        like 0.10 shares at prices like $0.52), we try adjusting the price
        by -$0.01 increments until we find a price where a valid size exists.
        This ensures small remaining amounts from partial fills can still be sold.

        Returns:
            Tuple of (adjusted_size, price) as floats
        """
        d_original_size = Decimal(str(size)).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
        d_price = Decimal(str(price)).quantize(Decimal("0.01"), rounding=ROUND_DOWN)

        # Try the given price first, then adjust price down by 1 cent at a time
        max_price_adjustments = 5  # At most 5 cents worse
        for _ in range(max_price_adjustments + 1):
            if d_price <= 0:
                break

            d_size = d_original_size
            while d_size > 0:
                product = d_size * d_price
                if product == product.quantize(Decimal("0.01")):
                    return float(d_size), float(d_price)
                d_size -= Decimal("0.01")

            # No valid size at this price, try 1 cent lower
            d_price -= Decimal("0.01")

        return 0.0, float(Decimal(str(price)).quantize(Decimal("0.01"), rounding=ROUND_DOWN))

    def place_market_buy(
        self, token_id: str, dollar_amount: float, price: Optional[float] = None,
        slippage_cents: int = 0,
    ) -> dict:
        """
        Place a market buy order (FAK at best ask).

        Args:
            token_id: The token to buy
            dollar_amount: Amount in dollars to spend
            price: Optional price to use (skips REST call if provided)
            slippage_cents: Cents of slippage tolerance added to limit price

        Returns:
            Order result dict with 'success', 'orderID', etc.
        """
        # Use provided price or fetch from REST API
        best_ask = price if price is not None else self.get_best_ask(token_id)
        if not best_ask:
            return {"success": False, "errorMsg": "No asks available"}

        d_original_price = Decimal(str(best_ask)).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
        d_amount = Decimal(str(dollar_amount))

        # 1. Whole shares at original ask (before slippage)
        whole_size = int((d_amount / d_original_price).to_integral_value(rounding=ROUND_DOWN))
        if whole_size < 1:
            whole_size = 1

        # 2. Slippage only affects limit price, not share count
        limit_price = best_ask
        if slippage_cents > 0:
            limit_price = min(best_ask + slippage_cents / 100, 0.99)
        d_limit = Decimal(str(limit_price)).quantize(Decimal("0.01"), rounding=ROUND_DOWN)

        # 3. Cap USDC commitment to whole_size * original_ask.
        #    This bounds the fill: at the original ask, we receive at most
        #    whole_size shares. Price improvement may still produce a small
        #    fractional part, but never more than whole_size total.
        target_usdc = Decimal(str(whole_size)) * d_original_price
        capped_size = float((target_usdc / d_limit).quantize(Decimal("0.01"), rounding=ROUND_DOWN))

        # Try the slippage limit price first. If _clean_order_amounts can't
        # find a valid pair (or drops the price below the original ask, which
        # would make the FAK pointless), fall back to exact whole shares at
        # the original ask -- that product (int × 2dp) always has ≤2 decimals.
        size, price = self._clean_order_amounts(capped_size, float(d_limit))
        if size <= 0 or price < float(d_original_price):
            size, price = float(whole_size), float(d_original_price)

        if size <= 0:
            return {"success": False, "errorMsg": "Calculated size is zero"}

        order_args = OrderArgs(
            token_id=token_id,
            price=price,
            size=size,
            side="BUY",
        )

        try:
            signed_order = self.client.create_order(order_args)
            result = self.client.post_order(signed_order, OrderType.FAK)
            return self._parse_order_result(result, size, price, token_id, "BUY")
        except Exception as e:
            return {"success": False, "errorMsg": str(e)}

    def place_market_sell(
        self, token_id: str, shares: float, price: Optional[float] = None,
        slippage_cents: int = 0,
    ) -> dict:
        """
        Place a market sell order (FAK at best bid or specified price).

        Args:
            token_id: The token to sell
            shares: Number of shares to sell
            price: Optional price override (uses best bid if not specified)
            slippage_cents: Cents of slippage tolerance subtracted from limit price

        Returns:
            Order result dict with 'success', 'orderID', etc.
        """
        if price is None:
            price = self.get_best_bid(token_id)
        if not price:
            return {"success": False, "errorMsg": "No bids available"}

        # Apply slippage tolerance for FAK fill reliability
        if slippage_cents > 0:
            price = max(price - slippage_cents / 100, 0.01)

        # Use Decimal math to ensure product has ≤2 decimals
        clean_size, clean_price = self._clean_order_amounts(shares, price)

        if clean_size <= 0:
            return {"success": False, "errorMsg": "Calculated size is zero"}

        order_args = OrderArgs(
            token_id=token_id,
            price=clean_price,
            size=clean_size,
            side="SELL",
        )

        try:
            signed_order = self.client.create_order(order_args)
            result = self.client.post_order(signed_order, OrderType.FAK)
            return self._parse_order_result(result, clean_size, clean_price, token_id, "SELL")
        except Exception as e:
            return {"success": False, "errorMsg": str(e)}

    def place_limit_buy(self, token_id: str, shares: float, price: float) -> dict:
        """
        Place a GTC limit buy order.

        Args:
            token_id: The token to buy
            shares: Number of shares to buy
            price: Limit price

        Returns:
            Order result dict
        """
        # Use Decimal math to ensure product has ≤2 decimals
        clean_size, clean_price = self._clean_order_amounts(shares, price)

        if clean_size <= 0:
            return {"success": False, "errorMsg": "Calculated size is zero"}

        order_args = OrderArgs(
            token_id=token_id,
            price=clean_price,
            size=clean_size,
            side="BUY",
        )

        try:
            signed_order = self.client.create_order(order_args)
            result = self.client.post_order(signed_order, OrderType.GTC)
            return self._parse_order_result(result, clean_size, clean_price, token_id, "BUY")
        except Exception as e:
            return {"success": False, "errorMsg": str(e)}

    def place_limit_sell(self, token_id: str, shares: float, price: float) -> dict:
        """
        Place a GTC limit sell order (resting order on the book).

        Used for dust cleanup: leaves a sell order that can fill later
        when someone wants to buy at that price.

        Args:
            token_id: The token to sell
            shares: Number of shares to sell
            price: Limit price

        Returns:
            Order result dict
        """
        # Use Decimal math to ensure product has ≤2 decimals
        clean_size, clean_price = self._clean_order_amounts(shares, price)

        if clean_size <= 0:
            return {"success": False, "errorMsg": "Calculated size is zero"}

        order_args = OrderArgs(
            token_id=token_id,
            price=clean_price,
            size=clean_size,
            side="SELL",
        )

        try:
            signed_order = self.client.create_order(order_args)
            result = self.client.post_order(signed_order, OrderType.GTC)
            return self._parse_order_result(result, clean_size, clean_price, token_id, "SELL")
        except Exception as e:
            return {"success": False, "errorMsg": str(e)}

    def cancel_order(self, order_id: str) -> dict:
        """Cancel an open order."""
        try:
            result = self.client.cancel(order_id)
            return {"success": True, "result": result}
        except Exception as e:
            return {"success": False, "errorMsg": str(e)}

    def cancel_all_orders(self) -> dict:
        """Cancel all open orders."""
        try:
            result = self.client.cancel_all()
            return {"success": True, "result": result}
        except Exception as e:
            return {"success": False, "errorMsg": str(e)}

    def get_open_orders(self) -> list:
        """Get all open orders."""
        try:
            return self.client.get_orders()
        except Exception:
            return []

    def get_order_details(self, order_id: str) -> Optional[dict]:
        """Get details of a specific order including fill status."""
        try:
            return self.client.get_order(order_id)
        except Exception:
            return None

    def get_recent_trades(self, limit: int = 10) -> list:
        """Get recent trades for this account."""
        try:
            trades = self.client.get_trades()
            if isinstance(trades, list):
                return trades[:limit]
            return []
        except Exception:
            return []

    def _get_fill_from_trades(self, order_id: str, token_id: str, side: str) -> Optional[dict]:
        """
        Find fill details from recent trades matching an order.

        Returns dict with 'filled' (size) and 'price' if found.
        """
        trades = self.get_recent_trades(20)

        total_filled = 0.0
        total_value = 0.0

        for trade in trades:
            # Match by taker_order_id or check if it's our trade for this token
            taker_id = trade.get("taker_order_id", "")
            trade_asset = trade.get("asset_id", "")
            trade_side = trade.get("side", "").upper()

            # Check if this trade matches our order
            if order_id and order_id in taker_id:
                size = float(trade.get("size", 0))
                price = float(trade.get("price", 0))
                total_filled += size
                total_value += size * price
            elif trade_asset == token_id and trade_side == side:
                # Fallback: match by token and side for recent trades
                size = float(trade.get("size", 0))
                price = float(trade.get("price", 0))
                total_filled += size
                total_value += size * price
                break  # Only take the most recent matching trade

        if total_filled > 0:
            avg_price = total_value / total_filled
            return {"filled": total_filled, "price": avg_price}

        return None

    def _parse_order_result(
        self,
        result: dict,
        requested_size: float,
        submitted_price: float,
        token_id: str = "",
        side: str = "",
    ) -> dict:
        """
        Parse order result into standardized format.

        First checks for trades in the immediate response (FAK orders return
        fills directly). Falls back to querying order details if needed.
        """
        if isinstance(result, dict):
            success = result.get("success", False) or result.get("status") == "matched"
            order_id = result.get("orderID") or result.get("order_id")
            error_msg = result.get("errorMsg") or result.get("error")

            filled = 0.0
            fill_price = submitted_price

            # Check for takingAmount/makingAmount in response (Polymarket FAK fill data)
            # For BUY: takingAmount = shares received, makingAmount = USDC spent
            # For SELL: takingAmount = USDC received, makingAmount = shares sold
            taking = result.get("takingAmount")
            making = result.get("makingAmount")

            if taking and making:
                try:
                    taking_val = float(taking)
                    making_val = float(making)
                    if taking_val > 0 and making_val > 0:
                        if side == "BUY":
                            # BUY: taking=shares, making=USDC
                            filled = taking_val
                            fill_price = making_val / taking_val
                        else:
                            # SELL: taking=USDC, making=shares
                            filled = making_val
                            fill_price = taking_val / making_val
                        # Round price to 2 decimals
                        fill_price = round(fill_price, 2)
                except (ValueError, TypeError, ZeroDivisionError):
                    pass

            # If no fill data from takingAmount/makingAmount, poll for fill data.
            # The exchange has already matched this order, so fill data should
            # be available quickly. Use fast polling to minimize exit latency.
            # (Reduced from 3 attempts with 0.5/1.0/1.5s sleeps = 3.0s total
            #  to 2 attempts with 0/0.25s sleeps = 0.25s total.)
            if filled == 0 and success and order_id:
                for attempt in range(2):
                    if attempt > 0:
                        time.sleep(0.25)

                    # Try to get fill details from order status
                    order_details = self.get_order_details(order_id)
                    if order_details:
                        # Check various possible field names for fill data
                        if "size_matched" in order_details:
                            filled = float(order_details["size_matched"])
                        elif "sizeMatched" in order_details:
                            filled = float(order_details["sizeMatched"])
                        elif "matched_amount" in order_details:
                            filled = float(order_details["matched_amount"])

                        # Get actual fill price if available
                        if "average_price" in order_details:
                            fill_price = float(order_details["average_price"])
                        elif "price" in order_details and filled > 0:
                            # Only trust "price" field if we also got fill data
                            fill_price = float(order_details["price"])

                    if filled > 0:
                        break

                    # Check recent trades as fallback on last attempt (extra REST call)
                    if attempt == 1 and filled == 0 and token_id:
                        trade_fill = self._get_fill_from_trades(order_id, token_id, side)
                        if trade_fill:
                            filled = trade_fill["filled"]
                            fill_price = trade_fill["price"]

                # If no fill data found, assume full fill at submitted price
                if filled == 0 and success:
                    filled = requested_size

            return {
                "success": success,
                "orderID": order_id,
                "filled": filled,
                "requested": requested_size,
                "price": fill_price,
                "errorMsg": error_msg,
                "raw": result,
            }

        # String result (usually an error)
        return {
            "success": False,
            "errorMsg": str(result),
            "filled": 0.0,
            "requested": requested_size,
            "price": submitted_price,
        }
