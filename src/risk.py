"""Risk management: sizing, exposure caps, and kill switches.

Philosophy: every limit here is enforced independently of the strategy's
opinion. Kelly sizing is only as trustworthy as the model probability
feeding it, so it is hard-capped per trade; data staleness halts trading
outright because a fair value computed from a stale index is not a fair
value; and the daily loss limit is a stop-and-think switch, not a tunable.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from src.config import Config

logger = logging.getLogger("risk")


@dataclass
class RiskManager:
    cfg: Config

    halted: bool = False
    halt_reason: str = ""
    realized_pnl_today: float = 0.0
    _day_key: str = ""
    _consecutive_order_errors: int = 0
    _last_data_ok: bool = field(default=False)

    # ---- kill switches ----

    def halt(self, reason: str) -> None:
        if not self.halted:
            logger.error("TRADING HALTED: %s", reason)
        self.halted = True
        self.halt_reason = reason

    def roll_day(self, now: float | None = None) -> None:
        day = time.strftime("%Y-%m-%d", time.gmtime(now if now is not None else time.time()))
        if day != self._day_key:
            self._day_key = day
            self.realized_pnl_today = 0.0
            if self.halted and self.halt_reason == "daily loss limit":
                # A new day does NOT auto-resume after a loss-limit halt;
                # restarting the bot is a deliberate human action.
                logger.warning("new day, but staying halted (%s) until restart", self.halt_reason)

    def on_realized_pnl(self, pnl: float) -> None:
        self.realized_pnl_today += pnl
        if self.realized_pnl_today <= -abs(self.cfg.daily_loss_limit_usd):
            self.halt("daily loss limit")

    def record_order_error(self) -> None:
        self._consecutive_order_errors += 1
        if self._consecutive_order_errors >= self.cfg.max_consecutive_order_errors:
            self.halt(f"{self._consecutive_order_errors} consecutive order errors")

    def record_order_ok(self) -> None:
        self._consecutive_order_errors = 0

    # ---- gates ----

    def data_ok(self, brti_age_ms: float, kalshi_age_ms: float, vol_ready: bool) -> bool:
        ok = (
            brti_age_ms <= self.cfg.brti_stale_ms
            and kalshi_age_ms <= self.cfg.kalshi_ws_stale_ms
            and vol_ready
        )
        if ok != self._last_data_ok:
            logger.info(
                "data gate %s (brti_age=%.0fms kalshi_age=%.0fms vol_ready=%s)",
                "OPEN" if ok else "CLOSED", brti_age_ms, kalshi_age_ms, vol_ready,
            )
            self._last_data_ok = ok
        return ok

    def can_trade(self, brti_age_ms: float, kalshi_age_ms: float, vol_ready: bool) -> bool:
        return not self.halted and self.data_ok(brti_age_ms, kalshi_age_ms, vol_ready)

    # ---- sizing ----

    def taker_size(
        self,
        model_p: float,
        price: float,
        direction: int,          # +1 buy YES exposure, -1 sell/short YES
        current_pos: int,
        collateral_in_use: float,
    ) -> int:
        """Contracts for an aggressive order, respecting fractional Kelly,
        the per-trade risk cap, per-market position cap, and the global
        collateral cap. Returns 0 when any limit says no."""
        if self.halted:
            return 0
        if direction > 0:
            cost = price                       # dollars at risk per contract
            edge = model_p - price
            kelly = edge / max(1.0 - price, 1e-6)
        else:
            cost = 1.0 - price                 # short collateral per contract
            edge = price - model_p
            kelly = edge / max(price, 1e-6)
        if edge <= 0.0 or cost <= 0.0:
            return 0

        dollars = kelly * self.cfg.kelly_fraction * self.cfg.bankroll_usd
        dollars = min(dollars, self.cfg.max_risk_per_trade * self.cfg.bankroll_usd)
        dollars = min(dollars, max(0.0, self.cfg.max_total_notional_usd - collateral_in_use))
        n = int(dollars / cost)
        n = min(n, self.cfg.max_taker_size)
        n = min(n, self._position_room(direction, current_pos))
        return max(0, n)

    def quote_size(self, direction: int, current_pos: int, collateral_in_use: float) -> int:
        """Contracts for a resting quote on one side, shrinking to zero as
        the inventory cap on that side is approached."""
        if self.halted:
            return 0
        room = self._position_room(direction, current_pos)
        if collateral_in_use >= self.cfg.max_total_notional_usd:
            return 0
        return max(0, min(self.cfg.quote_size, room))

    def _position_room(self, direction: int, current_pos: int) -> int:
        return self.cfg.max_pos_per_market - direction * current_pos
