"""Backtest the Up/Down pricing model on historical per-second prices.

IMPORTANT LIMITATION -- read before trusting any number this produces:

Kalshi does not publish a free historical order-book feed for settled
15-minute markets, so the "market" this backtest trades against is
SYNTHETIC: a lagged, noised, spread-wrapped version of the model's own fair
value. That validates plumbing (window mechanics, settlement averaging,
sizing, fees, risk caps) and gives a rough sense of how often a
latency/staleness edge appears -- it CANNOT prove real edge against
Kalshi's actual market makers. Validate with MODE=paper against live
quotes before believing anything.

Input CSV: columns `timestamp,price` at ~1-second resolution (a recording
of the bot's own BRTI estimate works well -- the estimator logs samples).

Simulation per 15-minute window:
  strike  = mean of prices in [open-60, open)   (the previous settlement)
  settle  = mean of prices in [open+840, open+900)
  each second, the synthetic market quotes fair(lagged spot) +/- spread
  with noise; the taker logic fires when the true-fair vs quote gap clears
  fees + MIN_TAKER_EDGE.
"""
from __future__ import annotations

import argparse
import csv
import random
import statistics
from dataclasses import dataclass

from src.config import CONFIG
from src.fees import fee_per_contract
from src.pricing import EwmaVol, prob_settle_above

WINDOW_S = 15 * 60
AVG_S = 60
SYNTH_LAG_S = 3           # how stale the synthetic market maker's spot is
SYNTH_NOISE = 0.02        # gaussian noise on the synthetic quote (prob units)
SYNTH_HALF_SPREAD = 0.02


@dataclass
class WindowResult:
    open_ts: float
    strike: float
    settle: float
    trades: int
    contracts: int
    pnl: float


def load_series(path: str) -> list[tuple[float, float]]:
    rows: list[tuple[float, float]] = []
    with open(path, newline="") as f:
        for rec in csv.DictReader(f):
            rows.append((float(rec["timestamp"]), float(rec["price"])))
    rows.sort(key=lambda x: x[0])
    return rows


def run_backtest(series: list[tuple[float, float]], seed: int = 7) -> list[WindowResult]:
    rng = random.Random(seed)
    cfg = CONFIG
    if not series:
        return []

    # Second-indexed lookup for O(1) access.
    by_sec = {int(ts): px for ts, px in series}
    t0, t1 = int(series[0][0]), int(series[-1][0])

    vol = EwmaVol(half_life_s=cfg.vol_half_life_s, min_samples=cfg.vol_min_samples,
                  floor_annual=cfg.vol_floor_annual, cap_annual=cfg.vol_cap_annual)
    results: list[WindowResult] = []

    window_open = t0 + AVG_S + 1
    while window_open + WINDOW_S <= t1:
        close_ts = window_open + WINDOW_S
        pre = [by_sec[s] for s in range(window_open - AVG_S, window_open) if s in by_sec]
        post = [by_sec[s] for s in range(close_ts - AVG_S, close_ts) if s in by_sec]
        if len(pre) < AVG_S * 0.8 or len(post) < AVG_S * 0.8:
            window_open += WINDOW_S
            continue
        strike = statistics.fmean(pre)
        settle = statistics.fmean(post)

        pnl = 0.0
        trades = 0
        contracts = 0
        pos = 0
        cash = 0.0
        realized_sum = 0.0
        realized_n = 0

        for sec in range(window_open, close_ts):
            px = by_sec.get(sec)
            if px is None:
                continue
            vol.update(float(sec), px)
            if not vol.ready:
                continue
            tau = close_ts - sec
            if tau <= cfg.taker_stop_before_close_s:
                break
            if tau < AVG_S:  # Kalshi's window is (close-60, close]: start tick excluded
                realized_sum += round(px, 2)
                realized_n += 1

            fair = prob_settle_above(px, strike, tau, vol.sigma_s, AVG_S,
                                     realized_sum if realized_n else None,
                                     realized_n if realized_n else None)
            lag_px = by_sec.get(sec - SYNTH_LAG_S, px)
            mkt_mid = prob_settle_above(lag_px, strike, tau + SYNTH_LAG_S, vol.sigma_s, AVG_S)
            mkt_mid = min(0.99, max(0.01, mkt_mid + rng.gauss(0.0, SYNTH_NOISE)))
            ask = min(0.99, mkt_mid + SYNTH_HALF_SPREAD)
            bid = max(0.01, mkt_mid - SYNTH_HALF_SPREAD)

            edge_buy = fair - ask - fee_per_contract(ask, cfg.taker_fee_rate)
            edge_sell = bid - fair - fee_per_contract(bid, cfg.taker_fee_rate)
            size = cfg.max_taker_size
            if edge_buy >= cfg.min_taker_edge and pos < cfg.max_pos_per_market:
                n = min(size, cfg.max_pos_per_market - pos)
                cash -= n * ask + fee_per_contract(ask, cfg.taker_fee_rate) * n
                pos += n
                trades += 1
                contracts += n
            elif edge_sell >= cfg.min_taker_edge and pos > -cfg.max_pos_per_market:
                n = min(size, cfg.max_pos_per_market + pos)
                cash += n * bid - fee_per_contract(bid, cfg.taker_fee_rate) * n
                pos -= n
                trades += 1
                contracts += n

        result_yes = settle > strike
        pnl = cash + (pos if result_yes else 0)
        results.append(WindowResult(window_open, strike, settle, trades, contracts, pnl))
        window_open += WINDOW_S

    return results


def summarize(results: list[WindowResult]) -> None:
    traded = [r for r in results if r.trades]
    total = sum(r.pnl for r in traded)
    print(f"windows: {len(results)}  traded: {len(traded)}  contracts: {sum(r.contracts for r in traded)}")
    if traded:
        wins = sum(1 for r in traded if r.pnl > 0)
        print(f"win rate (windows): {wins}/{len(traded)} ({wins / len(traded) * 100:.1f}%)")
        print(f"total simulated PnL: ${total:,.2f}")
    print()
    print("NOTE: PnL here is against a SYNTHETIC market derived from this same")
    print("model (lagged + noised). It validates plumbing and the value of the")
    print("settlement-averaging math -- it does NOT demonstrate real edge against")
    print("Kalshi's live market. Use MODE=paper for that.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv_path", help="CSV with columns: timestamp,price (~1s resolution)")
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()
    summarize(run_backtest(load_series(args.csv_path), seed=args.seed))
