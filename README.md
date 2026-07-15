# Kalshi 15-Minute Crypto Up/Down HFT Bot

An event-driven trading bot for Kalshi's 15-minute crypto **Up/Down** markets
(default series: `KXBTC15M`, "Bitcoin Up or Down"). Each window, Kalshi lists
a binary contract that pays $1 if the settlement value ends **above the
"price to beat"** set at window open, $0 otherwise — and settles it on the
**CME CF Bitcoin Real-Time Index (BRTI)**: specifically, the *mean of the
final 60 one-second BRTI prints* before close.

This bot's entire premise is to price that contract slightly better and
slightly faster than the market by:

1. **Trading off the settlement index itself.** Kalshi streams the official
   CF Benchmarks BRTI over its websocket (`cfbenchmarks_value` channel),
   including — during the final minute of each quarter-hour — the exact
   running settlement average. The bot consumes that as its primary index,
   and *also* runs a local BRTI replica built from the constituent exchange
   order books (CF's published methodology: consolidated book → mid
   price-volume curve → utilized depth capped at 0.5% deviation, min 1 BTC →
   exponentially weighted mid, λ = 10.3) as a fallback and a continuous
   cross-check (`BRTI_SOURCE=auto`).
2. **Pricing the settlement average correctly.** A 60-second average is less
   volatile than a point close (variance `σ²(a + w/3)`, not `σ²(a + w)`),
   and inside the final minute part of the average is *already realized* —
   the bot tracks the locked-in ticks (officially streamed, exact) and
   reprices as certainty accrues, which is exactly when markets are most
   often mispriced.
3. **Reacting on events, not polls**: Kalshi order book deltas, official
   index ticks, lifecycle events (strike, close changes, instant settlement
   results), and fills — all over one websocket, driving maker quotes and
   taker sweeps from a single async event loop. Market rolls every 15
   minutes are handled with `update_subscription`, not reconnects.

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
 Kalshi ws: official BRTI ────────┐ (primary index + exact
 (cfbenchmarks_value, 1/sec)      │  settlement-window average)
                                  ▼
 Coinbase ─┐             ┌─────────────────┐        ┌──────────────┐
 Kraken   ─┤  L2 books   │ IndexSource     │───────▶│ EWMA vol     │
 Bitstamp ─┼────────────▶│ official-first, │        │ (per-√sec)   │
 Gemini   ─┘  (replica)  │ replica fallback│        └──────┬───────┘
                         └────────┬────────┘               │
                                  │ index ticks +          │ σ
                                  │ settlement-window sum  │
                                  ▼                        ▼
 Kalshi ws ──────────────▶ ┌──────────────────────────────────┐
 (orderbook_delta, trade,  │  UpDownStrategy                  │
  lifecycle, fill)         │  fair = P(60s avg > strike)      │
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

## Verified vs. verify-before-live

The **websocket layer is built against Kalshi's official AsyncAPI spec**:
host (`wss://external-api-ws.kalshi.com/trade-api/ws/v2`), the
`orderbook_delta`/`trade`/`fill` message shapes (dollar fixed-point fields,
canonical `outcome_side`/`book_side` direction, actual `fee_cost` on fills,
`post_position_fp` drift checks), the `cfbenchmarks_value` official index
stream and its settlement-window semantics (`(close−60s, close]`, start
tick excluded, close tick included, 60 ticks), `market_lifecycle_v2`
metadata (strike / close changes / `price_ranges` tick bands / instant
`determined` results), `update_subscription` market rolls, and terminal
error codes (10/17/25 → resubscribe).

The **REST side could not be verified** from the build environment
(network policy blocked the API hosts), so check these cheaply on demo
before live — each is a config change, not a code change:

1. **Order wire format** (`KALSHI_ORDER_API`): default `v2` posts to
   `/portfolio/events/orders` with `side: bid/ask` and decimal dollar
   prices (current docs style); `legacy` posts to `/portfolio/orders` with
   `side: yes/no + action` and integer cents (deprecation announced for
   2026). Place one tiny demo order; if it 4xx's, flip the flag.
2. **Fee rates** (`TAKER_FEE_RATE`, `MAKER_FEE_RATE`): crypto series carry
   a higher multiplier than the general 0.07 and a reduced maker rate.
   Check [kalshi.com/fee-schedule](https://kalshi.com/fee-schedule); the
   defaults here (0.10 / 0.025) deliberately overestimate. (Live fills
   report the actual fee via `fee_cost`, which the accounting uses
   directly — the configured rates then only gate *pre-trade* edge.)
3. **Series ticker** (`SERIES_TICKER`): `KXBTC15M` is the BTC 15-minute
   Up/Down series as of mid-2026; Kalshi renames series occasionally. The
   discovery loop logs what it finds — if it finds nothing, browse
   kalshi.com's crypto section for the current name (`KXETH15M` for ETH,
   with `BRTI_INDEX_ID=ETHUSD_RTI`).
4. **Strike field**: the "price to beat" arrives via `floor_strike` on the
   market object and `market_lifecycle_v2` metadata updates. The tracking
   log prints it — sanity check against the Kalshi UI for one window.
5. **Demo websocket host**: the spec documents production only; the demo
   default here is `wss://demo-api.kalshi.co` (override with
   `KALSHI_WS_URL` if demo lives elsewhere).

## Where the edge is supposed to come from (and where it leaks)

- **The settlement index itself, live.** The bot prices off the official
  BRTI stream (and the exact running settlement average in the final
  minute) while slower participants watch a single exchange's last trade —
  pricing the wrong underlying by a few dollars, which binaries near the
  strike amplify enormously in the final minutes.
- **The averaging window.** Correctly pricing `P(avg > K)` — especially the
  realized-tick collapse inside the last 60 seconds, now fed by the exact
  official window — is this bot's largest systematic differentiator. A
  market quoting 12c of uncertainty when 50 of 60 ticks are locked in is
  offering nearly free money.
- **Where it leaks:** taker fees at mid-probability prices (~2-3c round
  trip), adverse selection on resting quotes (you get filled precisely when
  fair value moved through you faster than you repriced), and REST order
  latency against other bots consuming the same official stream — that last
  one is the real competition. `MIN_TAKER_EDGE_CENTS` /
  `MIN_MAKER_EDGE_CENTS` exist to demand enough margin to survive all
  three.

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
