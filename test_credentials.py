#!/usr/bin/env python3
"""Test script to verify Polymarket credentials."""

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType
from py_clob_client.constants import POLYGON
from dotenv import load_dotenv
import os
import json

load_dotenv()

private_key = os.getenv("PRIVATE_KEY")
funder_address = os.getenv("FUNDER_ADDRESS")

print("Testing Polymarket credentials...")
print(f"Funder (proxy wallet) from .env: {funder_address}")

# Initialize client with proxy wallet as funder
# signature_type=0: EOA (funder IS the MetaMask address)
# signature_type=1: POLY_PROXY (Magic Link/Google login)
# signature_type=2: GNOSIS_SAFE (MetaMask browser extension login - most common)
client = ClobClient(
    host="https://clob.polymarket.com",
    key=private_key,
    chain_id=POLYGON,
    funder=funder_address,
    signature_type=2,  # GNOSIS_SAFE for MetaMask browser extension users
)

# Check what address the private key derives to
derived_address = client.get_address()
print(f"Address derived from private key: {derived_address}")
print(f"\nNote: These SHOULD be different - funder is your Polymarket proxy wallet,")
print(f"      private key is your MetaMask wallet that controls it.")

# Try to derive API credentials
print("\nDeriving API credentials...")
try:
    creds = client.derive_api_key()
    client.set_api_creds(creds)
    print(f"API Key: {creds.api_key[:20]}...")
    print("Credentials derived and set successfully!")
except Exception as e:
    print(f"Error deriving credentials: {e}")
    exit(1)

# Load market map to get real token IDs
print("\nLoading market map...")
try:
    with open("market_map.json", "r") as f:
        market = json.load(f)
    up_token = market["up_token_id"]
    print(f"UP token: {up_token[:30]}...")
except Exception as e:
    print(f"Could not load market_map.json: {e}")
    up_token = None

# Try fetching order book with real token
if up_token:
    print("\nTesting order book fetch...")
    try:
        book = client.get_order_book(up_token)
        bids = book.bids if hasattr(book, 'bids') else []
        asks = book.asks if hasattr(book, 'asks') else []
        print(f"Order book: {len(bids)} bids, {len(asks)} asks")
        if asks:
            best_ask = asks[0]
            price = best_ask.price if hasattr(best_ask, 'price') else best_ask.get('price')
            print(f"Best ask: {price}")
    except Exception as e:
        print(f"Error fetching order book: {e}")

# Try creating (but not posting) an order to test signing
if up_token:
    print("\nTesting order creation (will NOT post)...")
    try:
        order_args = OrderArgs(
            token_id=up_token,
            price=0.01,  # Very low price, won't fill
            size=1.0,
            side="BUY",
        )
        signed_order = client.create_order(order_args)
        print(f"Order signed successfully!")
        print(f"Order ID: {signed_order.get('orderID', 'N/A') if isinstance(signed_order, dict) else 'created'}")
    except Exception as e:
        print(f"Error creating order: {e}")

# Try actually posting a small limit order (at very low price so it won't fill)
if up_token:
    print("\nTesting order POST (small limit buy at $0.01 - won't fill)...")
    try:
        order_args = OrderArgs(
            token_id=up_token,
            price=0.01,
            size=1.0,
            side="BUY",
        )
        signed_order = client.create_order(order_args)
        result = client.post_order(signed_order, OrderType.GTC)
        print(f"Order posted successfully!")
        print(f"Result: {result}")

        # Cancel it immediately
        if isinstance(result, dict) and result.get("orderID"):
            print(f"Cancelling order {result['orderID']}...")
            cancel_result = client.cancel(result["orderID"])
            print(f"Cancelled: {cancel_result}")
    except Exception as e:
        print(f"Error posting order: {e}")

print("\nDone!")
