# Kalshi BTC 15-Minute Trading Bot

A framework for trading Kalshi's 15-minute Bitcoin price markets (binary
contracts that pay $1 if BTC is above/below a strike at expiry, $0
otherwise). It compares a statistical fair-value model to the current market
price and only trades when it finds a large enough edge, sized with
risk-capped Kelly criterion.

**Read this before running anything:**

> No bot can guarantee profit, and anyone who tells you otherwise is selling
> something. Kalshi's market makers are professional and fast; a 15-minute
> BTC binary is a genuinely hard market to have real edge in. This project
> gives you a statistically sound way to *look for* edge and to size and
> risk-manage trades if you find some — it does not hand you a money
> printer. Start in paper mode. Expect to spend real time validating before
> you ever touch live money.

## How it's supposed to work

1. **Price feed** (`src/btc_price_feed.py`) streams live BTC-USD trades from
   Coinbase's public websocket and keeps a rolling realized-volatility
   estimate.
2. **Pricing model** (`src/pricing_model.py`) treats BTC's short-horizon path
   as zero-drift geometric Brownian motion and computes the fair probability
   that BTC finishes above a given strike in the time remaining — this
   probability is also the fair dollar price of the YES contract.
3. **Strategy** (`src/strategy.py`) pulls Kalshi's currently open 15-minute
   BTC markets, computes the model's fair price for each, and compares it to
   Kalshi's live ask. It only signals a trade when the disagreement (edge)
   clears a configurable minimum, because a signal filtered on tiny edges is
   mostly fees and noise.
4. **Risk manager** (`src/risk_manager.py`) sizes each trade with fractional
   Kelly, then hard-caps it at a fixed percentage of bankroll regardless of
   what Kelly says — Kelly sizing is only as trustworthy as the probability
   feeding it, and this model can be wrong. It also enforces a max number of
   concurrent positions and a daily loss limit (kill switch).
5. **Order manager** (`src/order_manager.py`) executes the signal — in paper
   mode it simulates the fill locally and touches nothing external; in live
   mode it places a real limit order via the Kalshi API.
6. **Bot loop** (`src/bot.py`) ties the above together and polls on an
   interval.

## Why this can plausibly have edge (and where it can't)

The model doesn't try to predict *direction* — it only prices probability
consistently from volatility, the same way an options market maker prices a
binary. Edge, if it exists, comes from Kalshi's market not perfectly tracking
realized short-term volatility (e.g., stale quotes right after a vol regime
shift, or retail order flow pushing prices away from fair value). That is a
plausible but unproven edge — you need to validate it against real market
data (see Backtesting below) before trusting it with money.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

### Get Kalshi API credentials

1. Create an account at [kalshi.com](https://kalshi.com) (use the demo
   environment at demo.kalshi.co first — it's fake money on real market
   structure, and this bot defaults to it).
2. Go to Account → API Keys, generate a key. Kalshi gives you an API key ID
   and downloads an RSA private key (PEM file).
3. Put the PEM file somewhere safe (never commit it — `.gitignore` already
   excludes `*.pem` and `.env`) and point `.env` at it:
   ```
   KALSHI_API_KEY_ID=your-key-id
   KALSHI_PRIVATE_KEY_PATH=/path/to/kalshi_private_key.pem
   KALSHI_ENV=demo
   ```

### Find the right market series ticker

Kalshi's BTC 15-minute markets live under a series ticker (check the current
one in Kalshi's markets browser — it changes as Kalshi renames/relaunches
crypto series). Set it in `.env`:

```
BTC_SERIES_TICKER=KXBTCD
```

## Running

```bash
# Safe default: real market data, simulated fills, nothing sent to Kalshi.
MODE=paper python -m src.bot

# Sends real orders. Only do this on KALSHI_ENV=demo until you trust the bot,
# and only on KALSHI_ENV=prod once you've validated it with real money you
# can afford to lose.
MODE=live python -m src.bot
```

Watch the logs. In paper mode you'll see `[PAPER]` trade lines with no
network side effects; in live mode you'll see `[LIVE]` lines confirming
orders actually placed.

## Backtesting — read the limitation

```bash
python -m src.backtest path/to/btc_prices.csv  # columns: timestamp,price
```

**This backtest is not a substitute for paper trading.** Kalshi doesn't
publish free historical order-book data for settled 15-minute markets, so
`src/backtest.py` prices a synthetic market using the *same model* the
strategy trades against (plus injected noise/spread). That validates the
plumbing — sizing, risk limits, settlement math — but a strategy that looks
profitable against its own model is close to circular by construction. It
tells you the code works, not that the edge is real.

To actually validate edge, either:
- Run `MODE=paper` for an extended period and compare the model's fair price
  to Kalshi's actual live quotes before ever going live, or
- Feed real historical Kalshi prices (from their historical trades/candle
  endpoints, where available) into the backtest in place of the synthetic
  quote.

## Configuration reference (`.env`)

| Variable | Meaning |
|---|---|
| `MODE` | `paper`, `live`, or use `src/backtest.py` directly |
| `KALSHI_API_KEY_ID` / `KALSHI_PRIVATE_KEY_PATH` | API auth |
| `KALSHI_ENV` | `demo` or `prod` |
| `BANKROLL_USD` | Bankroll used for position sizing |
| `MAX_RISK_PER_TRADE` | Hard cap on risk per trade, as a fraction of bankroll |
| `MAX_CONCURRENT_POSITIONS` | Max simultaneous open positions |
| `DAILY_LOSS_LIMIT_USD` | Kill switch: stop opening new trades after this much realized loss in a day |
| `KELLY_FRACTION` | Fraction of full Kelly to use (0.25 = quarter-Kelly, conservative) |
| `MIN_EDGE_CENTS` | Minimum model-vs-market disagreement (in cents) required to trade |
| `BTC_SERIES_TICKER` | Kalshi series ticker for the BTC 15-min markets |

## Tests

```bash
pytest tests/ -v
```

## Realistic expectations

- Fees, slippage, and adverse selection (you tend to get filled when the
  market is about to move against you) all eat into any edge this finds.
  `MIN_EDGE_CENTS` exists to filter out trades too small to survive that.
- Start with a bankroll you are fully prepared to lose. Quarter-Kelly with a
  2% hard per-trade cap is deliberately conservative, not aggressive —
  loosen it only after you have real evidence (not backtest numbers) that
  the model has edge.
- Treat the daily loss limit as non-negotiable. If it fires, stop and figure
  out why before restarting the bot.

## What's not built yet (ideas for extending this)

- Order-book-aware execution (posting inside the spread instead of hitting
  the ask) to reduce the cost paid per trade.
- A live model-vs-market divergence logger to build a real edge dataset over
  time, independent of the (circular) synthetic backtest.
- Multi-exchange spot price consensus (Coinbase + Binance + Kraken) instead
  of a single feed, to reduce feed-specific noise.
- Automatic position settlement polling (currently `settle_position` must be
  called manually / wired into a scheduler once you confirm Kalshi's
  settlement endpoint behavior for your account).
