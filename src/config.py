"""Configuration loader for Polymarket trading bot."""

import json
import os
from pathlib import Path
from dotenv import load_dotenv


# Project root directory
PROJECT_ROOT = Path(__file__).parent.parent


def load_env_config() -> dict:
    """Load environment variables from .env file."""
    env_path = PROJECT_ROOT / ".env"
    load_dotenv(env_path)

    # Only private key and funder address are required
    # API credentials are derived automatically from private key
    required_vars = [
        "FUNDER_ADDRESS",
        "PRIVATE_KEY",
    ]

    optional_vars = [
        "POLYMARKET_API_KEY",
        "POLYMARKET_API_SECRET",
        "POLYMARKET_PASSPHRASE",
    ]

    config = {}
    missing = []

    for var in required_vars:
        value = os.getenv(var)
        if not value:
            missing.append(var)
        config[var.lower()] = value

    # Load optional vars (won't error if missing)
    for var in optional_vars:
        config[var.lower()] = os.getenv(var)

    if missing:
        raise ValueError(f"Missing required environment variables: {', '.join(missing)}")

    return config


def load_trading_params() -> dict:
    """Load trading parameters from config_params.json."""
    config_path = PROJECT_ROOT / "config_params.json"

    if not config_path.exists():
        raise FileNotFoundError(f"Trading config not found: {config_path}")

    with open(config_path, "r") as f:
        params = json.load(f)

    required_params = ["position_size", "take_profit_pct", "stop_loss_pct"]
    for param in required_params:
        if param not in params:
            raise ValueError(f"Missing required parameter: {param}")

    return params


def load_market_map() -> dict:
    """Load market mapping from market_map.json."""
    map_path = PROJECT_ROOT / "market_map.json"

    if not map_path.exists():
        raise FileNotFoundError(
            f"Market map not found: {map_path}\n"
            "Run 'python run_mapper.py' first to generate it."
        )

    with open(map_path, "r") as f:
        market_map = json.load(f)

    required_fields = ["up_token_id", "down_token_id", "event_title"]
    for field in required_fields:
        if field not in market_map:
            raise ValueError(f"Missing required field in market_map.json: {field}")

    return market_map


def save_market_map(market_map: dict) -> None:
    """Save market mapping to market_map.json."""
    map_path = PROJECT_ROOT / "market_map.json"

    with open(map_path, "w") as f:
        json.dump(market_map, f, indent=2)


class Config:
    """Unified configuration object."""

    def __init__(self):
        self.env = load_env_config()
        self.params = load_trading_params()
        self.market = None  # Loaded separately when needed

    def load_market(self):
        """Load market mapping."""
        self.market = load_market_map()

    @property
    def api_key(self) -> str:
        return self.env["polymarket_api_key"]

    @property
    def api_secret(self) -> str:
        return self.env["polymarket_api_secret"]

    @property
    def passphrase(self) -> str:
        return self.env["polymarket_passphrase"]

    @property
    def funder_address(self) -> str:
        return self.env["funder_address"]

    @property
    def private_key(self) -> str:
        return self.env["private_key"]

    @property
    def position_size(self) -> float:
        return self.params["position_size"]

    @property
    def take_profit_pct(self) -> float:
        return self.params["take_profit_pct"]

    @property
    def stop_loss_pct(self) -> float:
        return self.params["stop_loss_pct"]

    @property
    def trigger_threshold(self) -> float:
        return self.params.get("trigger_threshold", 0.00015)

    @property
    def signal_cooldown_ms(self) -> int:
        return self.params.get("signal_cooldown_ms", 2000)

    @property
    def volatility_window_ms(self) -> int:
        return self.params.get("volatility_window_ms", 500)

    @property
    def max_spread_cents(self) -> int:
        return self.params.get("max_spread_cents", 1)

    @property
    def stale_position_sec(self) -> float:
        """Seconds after which a position is considered stale (activates trailing stop)."""
        return self.params.get("stale_position_sec", 5.0)

    @property
    def price_cache_stale_ms(self) -> int:
        """Milliseconds after which cached prices are considered stale."""
        return self.params.get("price_cache_stale_ms", 5000)

    @property
    def slippage_cents(self) -> int:
        """Cents of slippage tolerance for FAK order fill reliability."""
        return self.params.get("slippage_cents", 2)

    @property
    def reconnect_cooldown_ms(self) -> int:
        """Milliseconds to skip signals after a Polymarket WS reconnect."""
        return self.params.get("reconnect_cooldown_ms", 5000)

    @property
    def up_token_id(self) -> str:
        if not self.market:
            raise ValueError("Market not loaded. Call load_market() first.")
        return self.market["up_token_id"]

    @property
    def down_token_id(self) -> str:
        if not self.market:
            raise ValueError("Market not loaded. Call load_market() first.")
        return self.market["down_token_id"]

    @property
    def event_title(self) -> str:
        if not self.market:
            raise ValueError("Market not loaded. Call load_market() first.")
        return self.market["event_title"]
