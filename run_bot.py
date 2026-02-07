#!/usr/bin/env python3
"""Entry point for Polymarket trading bot."""

import sys
from pathlib import Path

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent))

from src.main import main


if __name__ == "__main__":
    main()
