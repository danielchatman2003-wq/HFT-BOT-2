# Kalshi 15-Minute Crypto Up/Down HFT Bot

An event-driven trading bot for Kalshi's 15-minute crypto **Up/Down** markets
(default series: `KXBTC15M`, "Bitcoin Up or Down"). Each window, Kalshi lists
a binary contract that pays $1 if the settlement value ends **above the
"price to beat"** set at window open, $0 otherwise — and settles it on the
**CME CF Bitcoin Real-Time Index (BRTI)**: specifically, the *mean of the
final 60 one-second BRTI prints* before close.

This bot's entire premise is to price that contract slightly better and
slightly faster than the market by:

1. **Replicating the BRTI in real time** from the same constituent exchange
   order books CF Benchmarks uses (published methodology: consolidated book →
   mid price-volume curve → utilized depth capped at 0.5% deviation, min
   1 BTC → exponentially weighted mid with λ = 10.3), instead of watching a
   single exchange's last trade.
2. **Pricing the settlement average correctly.** A 60-second average is less
   volatile than a point close (variance `σ²(a + w/3)`, not `σ²(a + w)`),
   and inside the final minute part of the average is *already realized* —
   the bot tracks the locked-in ticks and reprices as certainty accrues,
   which is exactly when markets are most often mispriced.
3. **Reacting on events, not polls**: Kalshi order book deltas over
   websocket, 1 Hz index ticks, fills — with maker quotes and taker sweeps
   driven off a single async event loop.

**Read this before running anything:**

> No bot can guarantee profit. Kalshi's crypto market makers are
> professional and fast, fees on crypto series are meaningful, and a
> 15-minute binary is a genuinely hard market to beat. "HFT" here means
> event-driven, sub-second *reaction* — order placement still crosses the
> public internet to Kalshi's REST API (tens of ms at best) under
> rate limits (~10 writes/sec on the basic tier). Nothing in this repo is
> co-located-CME-speed, and nobody trading through the public API is.
> Start in paper mode, expect to lose your assumed edge to fees and adverse
> selection, and validate for a long time before risking real money.
> This is not financial advice; trade only what you can afford to lose.

## Architecture

```
 Coinbase ─┐                                        ┌──────────────┐
 Kraken   ─┤  L2 books   ┌─────────────────┐  1 Hz  │ EWMA vol     │
 Bitstamp ─┼────────────▶│ BRTI estimator  │───────▶│ (per-√sec)   │
 Gemini   ─┘             │ (CF methodology)│        └──────┬───────┘
                         └────────┬────────┘               │
                                  │ index ticks +          │ σ
                                  │ settlement-window sum  │
                                  ▼                        ▼
 Kalshi ws ──────────────▶ ┌──────────────────────────────────┐
 (orderbook_delta,         │  UpDownStrategy                  │
  trade, fill)             │  fair = P(60s avg > strike)      │
                           │  taker: IOC when edge > fee+min  │
                           │  maker: post-only quotes, vol-   │
                           │  adaptive spread, inventory skew │
                           └───────────────┬──────────────────┘
                                           │ place/cancel (rate-limited)
                              ┌────────────┴────────────┐
                              │ LiveExecution           │   MODE=live
                              │ PaperExecution          │   MODE=paper
                              └────────────┬────────────┘
                                           ▼
                              RiskManager: Kelly-capped sizing,
                              position/notional caps, daily loss
                              kill switch, staleness gates
```

Module map:

| Path | What it does |
|---|---|
| `src/brti/feeds/` | Reconnecting public L2 feeds: Coinbase, Kraken, Bitstamp, Gemini |
| `src/brti/index.py` | CF Real-Time-Index replica + 1 Hz sampler + settlement-window tracker |
| `src/pricing.py` | Up/Down fair value under settlement averaging; EWMA vol |
| `src/fees.py` | Kalshi fee formula (ceil-to-cent), netted out of every edge |
| `src/kalshi/` | RSA-PSS signing, rate-limited async REST, websocket + book maintenance |
| `src/strategy.py` | Event-driven maker/taker logic |
| `src/execution.py` | Live + paper execution, signed-YES average-cost accounting |
| `src/risk.py` | Sizing caps and kill switches |
| `src/bot.py` | Orchestrator (`python -m src.bot`) |
| `src/backtest.py` | Synthetic-market plumbing test (see limitations inside) |

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

### Kalshi credentials (needed even for paper mode)

The Kalshi websocket requires an authenticated connection, and paper mode
consumes live Kalshi market data. Keys are free:

1. Create an account — use **demo.kalshi.co** first (fake money, real
   market structure; this bot defaults to it).
2. Account → API keys → create. Save the downloaded RSA private key PEM.
3. In `.env`: set `KALSHI_API_KEY_ID` and `KALSHI_PRIVATE_KEY_PATH`.
   (`.gitignore` already excludes `.env` and `*.pem`.)

## Running

```bash
# Paper: real BRTI + Kalshi data, simulated fills, nothing ever sent. Default.
python -m src.bot

# Live on the demo exchange (fake money), real order flow end to end:
MODE=live KALSHI_ENV=demo python -m src.bot

# Live with real money -- only after everything in "Verify before live":
MODE=live KALSHI_ENV=prod python -m src.bot
```

Watch the status line: BRTI value, live feeds, per-market fair value vs
market BBO, position, and day PnL. `[PAPER-TAKE]`/`[PAPER-MAKE]` lines mark
simulated fills; `FILL`/`SETTLED` lines mark accounting events.

Tests: `pytest tests/ -v` (77 tests, all offline — pricing math, BRTI
aggregation, book maintenance across both wire formats, fees, risk gates,
paper fills, and strategy decisions).

## Verify before going live — this matters

This bot was written in an environment where `docs.kalshi.com`,
`api.elections.kalshi.com`, and CF Benchmarks' site were **blocked by
network policy**, so several facts were taken from Kalshi's published
examples and third-party documentation rather than confirmed against the
live API. All of them are cheap to check on the demo environment, and the
bot is built to make each one a config change, not a code change:

1. **Order wire format** (`KALSHI_ORDER_API`): default `v2` posts to
   `/portfolio/events/orders` with `side: bid/ask` and decimal dollar
   prices (current docs style); `legacy` posts to `/portfolio/orders` with
   `side: yes/no + action` and integer cents (deprecation announced for
   2026). Place one tiny demo order; if it 4xx's, flip the flag.
2. **Fee rates** (`TAKER_FEE_RATE`, `MAKER_FEE_RATE`): crypto series carry
   a higher multiplier than the general 0.07 and a reduced maker rate.
   Check [kalshi.com/fee-schedule](https://kalshi.com/fee-schedule); the
   defaults here (0.10 / 0.025) deliberately overestimate.
3. **Series ticker** (`SERIES_TICKER`): `KXBTC15M` is the BTC 15-minute
   Up/Down series as of mid-2026; Kalshi renames series occasionally. The
   discovery loop logs what it finds — if it finds nothing, browse
   kalshi.com's crypto section for the current name (`KXETH15M` for ETH).
4. **Strike field**: the "price to beat" is read from the market's
   `floor_strike` (with fallbacks). The tracking log line prints it — sanity
   check it against the Kalshi UI for one window before trusting it.
5. **Settlement mechanics**: contracts settle on the 60-second BRTI mean;
   `SETTLEMENT_WINDOW_S`/`SETTLEMENT_TICKS` encode that and are
   configurable if Kalshi's rulebook changes.

## Where the edge is supposed to come from (and where it leaks)

- **BRTI vs single-exchange watchers.** Settlement is on a consolidated
  index. Bots (and humans) pricing off Coinbase's last trade are pricing
  the wrong underlying by a few dollars — small, but binaries near the
  strike amplify small differences enormously in the final minutes.
- **The averaging window.** Correctly pricing `P(avg > K)` — especially the
  realized-tick collapse inside the last 60 seconds — is this bot's largest
  systematic differentiator. A market quoting 12c of uncertainty when 50 of
  60 ticks are locked in is offering nearly free money *if your index
  replica is accurate*.
- **Where it leaks:** taker fees at mid-probability prices (~2-3c round
  trip), adverse selection on resting quotes (you get filled precisely when
  fair value moved through you faster than you repriced), REST order
  latency against other bots, and any systematic error between this BRTI
  replica and the real print (LMAX Digital's book is not public, so the
  replica is a subset of constituents). `MIN_TAKER_EDGE_CENTS` /
  `MIN_MAKER_EDGE_CENTS` exist to demand enough margin to survive all four.

## Risk controls (all enforced independently of the strategy)

| Control | Default | Behavior |
|---|---|---|
| Data staleness gate | BRTI 2.5s / Kalshi ws 5s | No fair value from stale data: quotes pulled, taking blocked |
| Per-trade cap | 2% of bankroll | Hard cap regardless of Kelly's opinion |
| Kelly fraction | 0.25 | Sizing from edge, quarter-strength |
| Position cap | 50/market | Signed-YES contracts, both directions |
| Notional cap | $500 | Total collateral at risk across markets |
| Daily loss limit | $100 | Halts trading; requires restart (deliberate) |
| Consecutive order errors | 5 | Halts (broken API assumptions ≠ keep firing) |
| Quote TTL | 10s | Server-side expiry: a crashed bot bleeds off the book |
| Close guards | 12s / 1.5s | Quoting stops first, taking stops last |

Live mode also cancels all resting orders on shutdown, and rate-limits
itself to the basic-tier token buckets (configurable if you have a higher
tier).

## Backtesting — the honest version

`python -m src.backtest data.csv` (CSV: `timestamp,price` at ~1s) replays
15-minute windows with real settlement mechanics, but the counterparty is a
**synthetic** market (the model's own fair value, lagged + noised + spread).
That validates window/averaging/fee/sizing plumbing and quantifies the value
of the realized-tick math — it cannot prove edge against Kalshi's real
market makers, because it isn't them. The way to build a real edge dataset
is `MODE=paper`: it trades against genuine Kalshi quotes and logs every
model-vs-market divergence. Paper maker fills ignore queue priority
(optimistic); treat paper PnL as an upper bound.

## Config reference

Every knob lives in `.env` — see `.env.example`, which documents each one.
The high-leverage ones: `MIN_TAKER_EDGE_CENTS` (selectivity), `QUOTE_SIZE` /
`MAX_POS_PER_MARKET` (inventory), `MAKER_VOL_MULT` (quote width vs vol),
`TAKER_FEE_RATE`/`MAKER_FEE_RATE` (must match the live fee schedule), and
`ENABLE_MAKER`/`ENABLE_TAKER` (run one style at a time while validating).

## Known gaps / extension ideas

- **LMAX Digital / itBit books** aren't public; the BRTI replica runs on
  Coinbase + Kraken + Bitstamp + Gemini. Add a feed adapter in
  `src/brti/feeds/` (5-minute job) if you have access to another
  constituent's data.
- **Queue-position modeling** for paper maker fills (currently optimistic).
- **Order amend** instead of cancel/replace once Kalshi's amend endpoint is
  verified — halves write-token spend per reprice.
- **Multi-market**: `KXETH15M` runs on the same machinery; run a second
  instance with a different `.env` rather than one process trading both
  until you've watched inventory behavior for a while.
- **Persistence**: positions/PnL are in-memory; live restarts reconcile
  against `get_positions` but historical fills aren't stored. Pipe
  `logs/trades.csv` somewhere durable if you need an audit trail.
