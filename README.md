# Hyperliquid Market Maker Bot

A sophisticated automated market maker for Hyperliquid perpetual futures, featuring adaptive strategies, risk management, and real-time monitoring.

## 🎯 Overview

This bot provides liquidity on Hyperliquid's builder DEX markets by placing layered limit orders on both sides of the orderbook. It earns maker rebates (~0.02% per fill) while managing inventory risk through dynamic spread adjustments and position limits.

### Key Features

- **📊 Layered Order Placement**: Multiple price levels per side for maximum volume and rebates
- **🧠 Adaptive Strategies**: Auto-adjusts to market conditions
  - Volatility-based spread widening
  - Fill rate optimization (tighten/widen based on adverse selection)
  - Adverse selection detection and protection
  - Time-of-day adjustments (wider during US market hours)
  - Funding rate alpha (bias adjustment based on funding)
- **🛡️ Risk Management**
  - Inventory limits with dynamic adjustment
  - Unrealized loss monitoring with emergency flatten
  - Session drawdown limits
  - Funding rate flush (flatten before hourly funding)
- **📱 Telegram Integration**: Real-time position/order updates every 15 minutes
- **📈 Performance Tracking**: Volume, fills, estimated rebates

## ⚠️ Risk Warning

**This bot trades with real money. Use at your own risk.**

- Start with small position sizes (`max_inventory`)
- Monitor the bot closely, especially during initial runs
- Understand that market making can result in losses during adverse market conditions
- Ensure you have sufficient margin for position sizes
- The bot can accumulate directional exposure if one-sided fills occur

## 📋 Requirements

- Python 3.8+
- Hyperliquid account with API access
- Sufficient collateral for trading

## 🚀 Installation

1. **Clone the repository**
```bash
git clone <your-repo-url>
cd tradexyz
```

2. **Create virtual environment**
```bash
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
```

3. **Install dependencies**
```bash
pip install -r requirements.txt
```

4. **Set up Hyperliquid API access**

   You need two pieces of information:
   - **Main wallet address**: Your wallet shown in Hyperliquid UI (e.g., `0x96ba...`)
   - **API private key**: Generated when you create API access in Hyperliquid

   The API key acts as an "agent" that signs transactions on behalf of your main wallet.

5. **Approve the API agent** (ONE-TIME SETUP)

   On the Hyperliquid website, open **More → API**, generate an API wallet and authorize it.
   Never give your main wallet's private key to a script; the bot only needs the API wallet key.

6. **Configure the bot**

   Edit `config.json`:
   ```json
   {
     "symbol": "xyz:XYZ100",
     "max_inventory": 1.5,
     "order_size": 0.25,
     "num_layers": 3,
     "layer_spacing_bps": 10
   }
   ```

   Put your credentials in a `.env` file (already ignored by git), not in `config.json`:
   ```bash
   PRIVATE_KEY=0xYOUR_API_PRIVATE_KEY_HERE
   ACCOUNT_ADDRESS=0xYOUR_MAIN_WALLET_ADDRESS_HERE
   TELEGRAM_BOT_TOKEN=
   TELEGRAM_CHAT_ID=
   ```

7. **Set up Telegram notifications** (Optional)

   See [TELEGRAM_SETUP.md](TELEGRAM_SETUP.md) for detailed instructions.

## ⚙️ Configuration Guide

### Core Settings

| Parameter | Description | Example |
|-----------|-------------|---------|
| `symbol` | Market to trade (format: `dex:coin`) | `"xyz:XYZ100"` |
| `max_inventory` | Max position size (in contracts) | `1.5` |
| `order_size` | Size per order layer | `0.25` |
| `num_layers` | Number of orders per side | `3` |
| `layer_spacing_bps` | Price spacing between layers (bps) | `10` |
| `session_drawdown_limit` | Max loss before shutdown | `-500` |

### Spread Configuration

```json
{
  "spread": {
    "base_bps": 20,                    // Base spread (20 bps = 0.2%)
    "inventory_skew_bps": 8,           // Skew quotes based on position
    "orderbook_skew_bps": 4,           // Skew based on orderbook imbalance
    "long_bias_bps": 8,                // Directional bias (positive = long)
    "quote_refresh_threshold_bps": 15  // Refresh when price moves >15 bps
  }
}
```

### Adaptive Strategy

```json
{
  "adaptive": {
    "volatility_lookback": 100,              // Samples for vol calculation
    "baseline_volatility_bps": 50,           // Expected normal volatility
    "adverse_selection_threshold": -0.002,   // P&L threshold for detection
    "adverse_selection_lookback": 10,        // Recent fills to analyze
    "fill_rate_high_threshold": 0.8,         // >80% fill rate = widen spread
    "fill_rate_low_threshold": 0.2,          // <20% fill rate = tighten spread
    "fill_rate_lookback_minutes": 15,        // Window for fill rate calc
    "fill_rate_spread_mult_max": 2.0,        // Max spread multiplier
    "fill_rate_spread_mult_min": 0.7         // Min spread multiplier
  }
}
```

### Risk Management

```json
{
  "risk": {
    "max_unrealized_loss_pct": 0.03,      // Emergency flatten at 3% loss
    "emergency_flatten_enabled": true
  }
}
```

### Funding Management

```json
{
  "funding": {
    "flush_lead_seconds": 300,   // Flatten 5min before funding
    "resume_delay_seconds": 10   // Wait 10s after funding before resuming
  }
}
```

## 🎮 Usage

### Start the Bot

```bash
python market_maker.py
```

### Monitor Logs

The bot creates detailed logs in the `logs/` directory:

- `logs/main.log` - General operations, quote updates, adaptive adjustments
- `logs/fills.log` - All fills with P&L tracking and performance metrics
- `logs/errors.log` - Errors and warnings
- `logs/risk.log` - Position monitoring and risk events

## 🏗️ Architecture

### Main Components

1. **HyperliquidClient**: API wrapper for orders, WebSocket subscriptions
2. **MarketMaker**: Core strategy logic, quote calculation, risk management
3. **Adaptive Systems**:
   - Volatility tracker
   - Adverse selection detector
   - Fill rate optimizer
   - Dynamic inventory limiter

### Async Tasks (9 concurrent)

1. `heartbeat_loop` - Optional health monitoring
2. `adaptive_update_loop` - Updates volatility, fill rate, etc. (every 5s)
3. `orderbook_listener` - WebSocket for orderbook updates
4. `fills_listener` - WebSocket for fill notifications
5. `oracle_listener` - WebSocket for oracle price
6. `quoting_loop` - Places/refreshes orders (every 0.25s)
7. `funding_flush_loop` - Monitors funding epochs
8. `risk_monitor` - Tracks P&L and unrealized losses (every 1s)
9. `telegram_update_loop` - Sends status updates (every 15min)

### Order Flow

```
1. Fetch mid price from orderbook
2. Calculate adaptive spread (base * multipliers)
3. Apply skews (inventory, orderbook, basis, long bias)
4. Compute bid/ask quotes
5. Check inventory limits (side gating)
6. Place layered orders (post-only)
7. Track fills → update inventory → check limits
8. Refresh when price moves >threshold
```

## 📊 Adaptive Spread System

The bot dynamically adjusts spreads based on:

### 1. Volatility (Auto-Baseline)
- Wider spread when volatility > baseline
- Auto-calculates baseline from 1-hour rolling window

### 2. Fill Rate
- **High fill rate (>80%)** → Widen spread up to 2x (being picked off)
- **Low fill rate (<20%)** → Tighten spread down to 0.7x (not competitive)
- **Sweet spot (20-80%)** → Neutral (1.0x)

### 3. Adverse Selection
- Detects when recent fills are unprofitable
- Widens spread progressively (up to 3x)

### 4. Time of Day
- Wider spread during US market hours (9:30-16:00 ET)

### 5. Orderbook Spread
- Legacy volatility guard: widen when exchange spread is abnormally wide

### Example Log Output

```
Adaptive state: vol=35.00bps baseline=50.00bps adv_sel=1.00x max_inv=1.50
funding_boost=0.00bps | fill_rate=45.2% spread_mult=1.00x
```

## 🔧 Troubleshooting

### Orders Not Being Placed

**Issue**: `Order REJECTED: Post only order would have immediately matched`

**Solution**: This is normal. The closest layer was too aggressive and would cross the spread. The bot will retry on the next cycle with a safer price.

### Position Exceeds Max Inventory

**Cause**: Orders filled on both sides before cancellation

**Solution**:
- The bot has inventory side gating to prevent this
- Increase `max_inventory` if you want larger positions
- Check logs for `bid_en` and `ask_en` flags

### Duplicate Orders on Startup

**Issue**: Old orders from previous run not cancelled

**Solution**:
- Bot now cancels all existing orders on startup
- If you see duplicates, manually cancel in UI before restarting
- Wait 60+ seconds between restarts to allow API to update

### Bot Shows Wrong Inventory

**Fixed**: Bot now filters historical fills and syncs position from API on startup

### Telegram Not Working

See [TELEGRAM_SETUP.md](TELEGRAM_SETUP.md)

## 📈 Performance Metrics

The bot tracks:

- **Total Fills**: Number of maker fills
- **Volume Traded**: Total notional volume (USD)
- **Estimated Rebates**: Maker rebates earned (~0.02% of volume)
- **Realized P&L**: Closed position P&L
- **Fill Rate**: Orders filled vs placed (15-min window)

### Example Fill Log

```
Fill B sz=0.2500 px=25410.0000 notional=6352.50 inv=0.2500 pnl=0.0000 entry=25410.0000 |
Session: 1 fills, $6,352.50 volume, $1.27 est. rebates
```

## 🔐 Security Notes

- **Never commit your private keys** to version control
- The `private_key` in config is for the API agent, not your main wallet
- Keep your main wallet private key secure (only needed for agent approval)
- Telegram bot tokens are sensitive - revoke and regenerate if exposed
- Use environment variables for production deployments

## 📝 Files Overview

- `market_maker.py` - Main bot implementation
- `config.json` - Configuration (no secrets; credentials come from `.env`)
- `get_telegram_chat_id.py` - Telegram setup helper
- `TELEGRAM_SETUP.md` - Telegram setup guide

## 🚦 Production Checklist

Before running with real money:

- [ ] Test with minimum `order_size` (0.01-0.1)
- [ ] Set conservative `max_inventory` (0.5-1.0)
- [ ] Set tight `session_drawdown_limit` (-100 to -500)
- [ ] Enable Telegram notifications
- [ ] Monitor logs for first 30 minutes
- [ ] Verify orders appear correctly in UI
- [ ] Check fills are being tracked properly
- [ ] Test funding flush behavior (wait for hourly funding)
- [ ] Ensure emergency flatten works (manually trigger unrealized loss)

## 🤝 Contributing

Issues and pull requests welcome! Please test thoroughly before submitting.

## ⚖️ License & Disclaimer

This software is provided "as is" without warranty of any kind. Trading cryptocurrencies involves substantial risk of loss. The authors are not responsible for any losses incurred while using this software.

**Use at your own risk.**

## 📚 Further Reading

- [Hyperliquid Documentation](https://hyperliquid.gitbook.io/)
- [TELEGRAM_SETUP.md](TELEGRAM_SETUP.md) - Telegram integration guide

---

**Happy Market Making! 📊💰**

*Remember: The goal is consistent rebates and inventory management, not predicting price direction.*
