#!/usr/bin/env python3
"""Entry point for market mapper - finds current BTC hourly market."""

import sys
from pathlib import Path

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent))

from src.market_mapper import run_mapper


if __name__ == "__main__":
    result = run_mapper()
    if not result:
        sys.exit(1)
