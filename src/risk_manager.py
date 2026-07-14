"""Position sizing and exposure limits.

Sizing uses fractional Kelly, capped hard by a per-trade risk ceiling. Kelly
sizing is only as good as the probability estimate feeding it -- an
overconfident model will produce an overconfident (and eventually
account-destroying) bet size, which is why this is capped independently of
whatever Kelly recommends.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from src.config import CONFIG


@dataclass
class RiskManager:
    bankroll_usd: float = CONFIG.bankroll_usd
    max_risk_per_trade: float = CONFIG.max_risk_per_trade
    max_concurrent_positions: int = CONFIG.max_concurrent_positions
    daily_loss_limit_usd: float = CONFIG.daily_loss_limit_usd
    kelly_fraction: float = CONFIG.kelly_fraction

    open_positions: int = 0
    realized_pnl_today_usd: float = field(default=0.0)

    def kelly_stake_fraction(self, model_prob: float, price_cents: float) -> float:
        """Fractional Kelly stake as a fraction of bankroll for a binary bet
        that costs `price_cents` (0-100) and pays out 100 cents if correct.

        Kelly f* = p - (1-p) / b, where b = payout_odds = (100 - price) / price
        """
        price_cents = max(1e-6, min(99.999999, price_cents))
        p = model_prob
        b = (100.0 - price_cents) / price_cents
        edge = p * (1 + b) - 1  # = p*(100/price) - 1
        if edge <= 0:
            return 0.0
        f_star = edge / b
        return max(0.0, f_star * self.kelly_fraction)

    def can_open_new_position(self) -> tuple[bool, str]:
        if self.open_positions >= self.max_concurrent_positions:
            return False, "max concurrent positions reached"
        if self.realized_pnl_today_usd <= -abs(self.daily_loss_limit_usd):
            return False, "daily loss limit hit"
        return True, ""

    def size_position(self, model_prob: float, price_cents: float) -> dict:
        """Returns contract count and dollar risk for a proposed trade,
        respecting both Kelly sizing and the hard per-trade risk cap."""
        ok, reason = self.can_open_new_position()
        if not ok:
            return {"contracts": 0, "risk_usd": 0.0, "reason": reason}

        kelly_frac = self.kelly_stake_fraction(model_prob, price_cents)
        if kelly_frac <= 0:
            return {"contracts": 0, "risk_usd": 0.0, "reason": "no positive edge"}

        kelly_risk_usd = kelly_frac * self.bankroll_usd
        hard_cap_usd = self.max_risk_per_trade * self.bankroll_usd
        risk_usd = min(kelly_risk_usd, hard_cap_usd)

        cost_per_contract = price_cents / 100.0
        contracts = int(risk_usd // cost_per_contract) if cost_per_contract > 0 else 0
        actual_risk_usd = contracts * cost_per_contract
        return {"contracts": contracts, "risk_usd": actual_risk_usd, "reason": "" if contracts > 0 else "size rounds to 0"}

    def record_position_opened(self):
        self.open_positions += 1

    def record_position_closed(self, pnl_usd: float):
        self.open_positions = max(0, self.open_positions - 1)
        self.realized_pnl_today_usd += pnl_usd

    def reset_daily_counters(self):
        self.realized_pnl_today_usd = 0.0
