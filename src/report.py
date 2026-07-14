"""Summarize a paper/live session's PnL from the trade log.

Usage:  python -m src.report [logs/trades.csv]

Reads the append-only CSV the execution engine writes (one row per fill and
per settlement) and prints net PnL after fees, per market and in total.
"""
from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from dataclasses import dataclass, field


@dataclass
class MarketSummary:
    fills: int = 0
    contracts: int = 0
    taken_or_made: int = 0
    fees: float = 0.0
    gross: float = 0.0  # realized PnL before fees (incl. settlement)
    settles: int = 0
    first_ts: float = field(default=0.0)
    last_ts: float = field(default=0.0)

    @property
    def net(self) -> float:
        return self.gross - self.fees


def summarize(path: str) -> tuple[dict[str, MarketSummary], MarketSummary]:
    per: dict[str, MarketSummary] = defaultdict(MarketSummary)
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            s = per[row["ticker"]]
            ts = float(row["ts"])
            s.first_ts = ts if not s.first_ts else min(s.first_ts, ts)
            s.last_ts = max(s.last_ts, ts)
            s.gross += float(row["realized_delta"])
            s.fees += float(row["fee"])
            if row["kind"] == "fill":
                s.fills += 1
                s.contracts += abs(int(row["signed_count"]))
            elif row["kind"] == "settle":
                s.settles += 1

    total = MarketSummary()
    for s in per.values():
        total.fills += s.fills
        total.contracts += s.contracts
        total.fees += s.fees
        total.gross += s.gross
        total.settles += s.settles
    return dict(per), total


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", default="logs/trades.csv")
    args = parser.parse_args()

    try:
        per, total = summarize(args.path)
    except FileNotFoundError:
        raise SystemExit(
            f"No trade log at {args.path!r}. Run the bot first (python -m src.bot); "
            "every fill and settlement lands there."
        )
    if not per:
        raise SystemExit(f"Trade log {args.path!r} is empty -- no fills yet.")

    print(f"{'market':<28} {'fills':>5} {'ctrts':>6} {'settles':>7} {'gross':>10} {'fees':>8} {'net':>10}")
    for ticker in sorted(per, key=lambda t: per[t].first_ts):
        s = per[ticker]
        print(
            f"{ticker:<28} {s.fills:>5} {s.contracts:>6} {s.settles:>7} "
            f"{s.gross:>10.2f} {s.fees:>8.2f} {s.net:>10.2f}"
        )
    print("-" * 78)
    print(
        f"{'TOTAL':<28} {total.fills:>5} {total.contracts:>6} {total.settles:>7} "
        f"{total.gross:>10.2f} {total.fees:>8.2f} {total.net:>10.2f}"
    )
    open_markets = [t for t, s in per.items() if s.settles == 0]
    if open_markets:
        print(f"\nnote: {len(open_markets)} market(s) have fills but no settlement yet "
              f"({', '.join(sorted(open_markets))}) -- their PnL is not final.")


if __name__ == "__main__":
    main()
