# Polymarket Trading Bot

A Python terminal application for fast manual trading on Polymarket's Bitcoin Up/Down hourly markets with automatic take-profit and stop-loss exits.

## Project Structure

```
├── .env.example           # Template for API credentials
├── .gitignore             # Ignores .env, market_map.json, __pycache__
├── config_params.json     # Trading parameters (position size, TP/SL)
├── requirements.txt       # Python dependencies
├── run_mapper.py          # Entry point: finds current BTC hourly market
├── run_bot.py             # Entry point: runs the trading bot
└── src/
    ├── __init__.py
    ├── config.py          # Configuration loader
    ├── market_mapper.py   # Gamma API market discovery
    ├── clob_client.py     # CLOB API wrapper for orders
    ├── websocket_client.py # Real-time price streaming
    ├── position_manager.py # Position tracking and TP/SL logic
    ├── order_executor.py  # Fast order placement
    └── main.py            # Terminal UI and keyboard handler
```

## Setup

1. **Copy `.env.example` to `.env`** and fill in your Polymarket credentials:
   ```bash
   cp .env.example .env
   ```

2. **Install dependencies** (requires Python 3.11+):
   ```bash
   pip3.11 install -r requirements.txt
   ```

3. **Run the market mapper** to find the current BTC hourly market:
   ```bash
   python3.11 run_mapper.py
   ```

4. **Run the bot**:
   ```bash
   python3.11 run_bot.py
   ```

## Keyboard Controls

| Key | Action |
|-----|--------|
| `u` | Buy UP |
| `d` | Buy DOWN |
| `x` | Show exit menu for open positions |
| `s` | Show status |
| `q` | Quit |

## Features

- Multiple independent positions
- Automatic TP/SL monitoring via WebSocket
- Partial fill acceptance on entry, aggressive retry on exit
- Events-only display (no price spam)
- Colored terminal output

## Configuration

Edit `config_params.json` to adjust trading parameters:

```json
{
  "position_size": 10.00,
  "take_profit_pct": 0.03,
  "stop_loss_pct": 0.03
}
```
