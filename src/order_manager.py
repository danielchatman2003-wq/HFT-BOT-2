"""Executes trade signals in either paper (simulated) or live mode, and
tracks open positions for the risk manager and PnL reporting.
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field

from src.kalshi_client import KalshiClient
from src.risk_manager import RiskManager
from src.strategy import Signal

logger = logging.getLogger("order_manager")


@dataclass
class Position:
    ticker: str
    side: str
    contracts: int
    entry_price_cents: float
    opened_at: float = field(default_factory=time.time)


class OrderManager:
    def __init__(self, risk_manager: RiskManager, kalshi_client: KalshiClient | None = None, paper: bool = True):
        self.risk_manager = risk_manager
        self.kalshi_client = kalshi_client
        self.paper = paper
        self.positions: dict[str, Position] = {}

    def execute_signal(self, signal: Signal) -> Position | None:
        if signal.ticker in self.positions:
            return None  # already have a position in this market

        sizing = self.risk_manager.size_position(signal.model_prob, signal.market_price_cents)
        contracts = sizing["contracts"]
        if contracts <= 0:
            logger.info("Skipping %s: %s", signal.ticker, sizing["reason"])
            return None

        if self.paper:
            logger.info(
                "[PAPER] BUY %s %s x%d @ %.1fc (model_prob=%.3f edge=%.1fc)",
                signal.side.upper(), signal.ticker, contracts, signal.market_price_cents,
                signal.model_prob, signal.edge_cents,
            )
        else:
            if self.kalshi_client is None:
                raise RuntimeError("Live mode requires a KalshiClient")
            self.kalshi_client.place_order(
                ticker=signal.ticker,
                side=signal.side,
                action="buy",
                count=contracts,
                price_cents=int(round(signal.market_price_cents)),
                client_order_id=str(uuid.uuid4()),
            )
            logger.info(
                "[LIVE] Order placed: BUY %s %s x%d @ %.1fc",
                signal.side.upper(), signal.ticker, contracts, signal.market_price_cents,
            )

        position = Position(
            ticker=signal.ticker,
            side=signal.side,
            contracts=contracts,
            entry_price_cents=signal.market_price_cents,
        )
        self.positions[signal.ticker] = position
        self.risk_manager.record_position_opened()
        return position

    def settle_position(self, ticker: str, resolved_yes: bool):
        """Call once a market's 15-minute window has expired and Kalshi has
        settled it, to realize PnL and free up the position slot."""
        position = self.positions.pop(ticker, None)
        if position is None:
            return

        won = (position.side == "yes") == resolved_yes
        payout_cents = 100.0 if won else 0.0
        pnl_usd = (payout_cents - position.entry_price_cents) / 100.0 * position.contracts
        self.risk_manager.record_position_closed(pnl_usd)
        logger.info(
            "Settled %s %s x%d: %s, pnl=$%.2f",
            position.side.upper(), ticker, position.contracts,
            "WIN" if won else "LOSS", pnl_usd,
        )
        return pnl_usd
