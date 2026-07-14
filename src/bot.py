"""Bot orchestrator: wires feeds, estimator, Kalshi client, strategy, risk.

Run modes (MODE in .env):
  paper -- real market data (BRTI constituents + Kalshi websocket), fills
           simulated locally, no orders ever sent. SAFE DEFAULT.
  live  -- real orders on the configured Kalshi environment (demo or prod).

Task layout (all asyncio, single process):
  - one task per BRTI constituent exchange feed
  - 1 Hz estimator sampler (aligned to wall-clock seconds, like the BRTI)
  - Kalshi websocket reader (books, trades, fills)
  - market discovery/settlement poller (REST)
  - event consumer driving the strategy
  - 250 ms timer + housekeeping, status logger

Note: Kalshi's websocket requires an authenticated connection even for
market data, so an API key is needed for paper mode too (keys are free;
use the demo environment while testing).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import signal
import time
from datetime import datetime, timezone

from src.brti.feeds import FEEDS
from src.brti.index import BrtiEstimator
from src.config import CONFIG, Config
from src.execution import LiveExecution, PaperExecution
from src.kalshi.auth import KalshiSigner
from src.kalshi.rest import KalshiAPIError, KalshiRest
from src.kalshi.ws import KalshiWs
from src.pricing import EwmaVol
from src.risk import RiskManager
from src.strategy import UpDownStrategy

logger = logging.getLogger("bot")


def _parse_close_ts(market: dict) -> float | None:
    raw = market.get("close_time") or market.get("expiration_time")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _parse_strike(market: dict) -> float | None:
    """The Up/Down 'price to beat'. Kalshi records it on floor_strike;
    fall back to other strike-ish fields if the schema shifts."""
    for key in ("floor_strike", "functional_strike", "cap_strike"):
        val = market.get(key)
        if val is not None:
            try:
                return float(val)
            except (TypeError, ValueError):
                continue
    return None


class Bot:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.events: asyncio.Queue = asyncio.Queue(maxsize=4096)

        self.estimator = BrtiEstimator(
            lambda_=cfg.brti_lambda,
            deviation_cap=cfg.brti_deviation_cap,
            min_depth=cfg.brti_min_depth_btc,
            feed_stale_ms=cfg.brti_feed_stale_ms,
        )
        self.vol = EwmaVol(
            half_life_s=cfg.vol_half_life_s,
            min_samples=cfg.vol_min_samples,
            floor_annual=cfg.vol_floor_annual,
            cap_annual=cfg.vol_cap_annual,
            winsor_k=cfg.vol_winsor_k,
        )
        self.feeds = []
        for name in cfg.brti_feeds:
            feed_cls = FEEDS.get(name)
            if feed_cls is None:
                logger.warning("unknown BRTI feed %r; skipping", name)
                continue
            feed = feed_cls()
            self.feeds.append(feed)
            self.estimator.register(feed.name, feed.book)

        try:
            self.signer: KalshiSigner | None = KalshiSigner(cfg.kalshi_api_key_id, cfg.kalshi_private_key_path)
        except (FileNotFoundError, ValueError):
            self.signer = None
        if self.signer is None or not cfg.kalshi_api_key_id:
            raise SystemExit(
                "Kalshi API credentials required (even paper mode consumes the "
                "authenticated websocket). Set KALSHI_API_KEY_ID and "
                "KALSHI_PRIVATE_KEY_PATH in .env -- keys are free, and "
                "KALSHI_ENV=demo works against the sandbox."
            )

        self.rest = KalshiRest(cfg, self.signer)
        self.ws = KalshiWs(cfg, self.signer, self.events)
        self.risk = RiskManager(cfg)
        if cfg.mode == "live":
            self.execution: LiveExecution | PaperExecution = LiveExecution(cfg, self.risk, self.rest)
        else:
            self.execution = PaperExecution(cfg, self.risk, self.ws.books)
        self.strategy = UpDownStrategy(cfg, self.estimator, self.vol, self.ws.books, self.execution, self.risk)

        self._pending_settlement: set[str] = set()
        self._stop = asyncio.Event()

    # ---- tasks ----

    async def sampler(self) -> None:
        """1 Hz BRTI estimate, aligned to wall-clock seconds like the real index."""
        while not self._stop.is_set():
            now = time.time()
            await asyncio.sleep(max(0.01, math.floor(now) + 1.0 - now))
            ts = time.time()
            value = self.estimator.sample(ts)
            if value is not None:
                self.vol.update(ts, value)
                self._push(("brti_tick", value))

    async def timer(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(0.25)
            self.risk.roll_day()
            if isinstance(self.execution, PaperExecution):
                self.execution.expire_stale_quotes()
            self._push(("timer", None))

    async def consumer(self) -> None:
        while not self._stop.is_set():
            kind, payload = await self.events.get()
            try:
                if kind == "fill":
                    # Live fills mutate positions first, then wake the strategy.
                    self.execution.on_fill(payload)
                    await self.strategy.on_event("fill", payload)
                elif kind == "trade":
                    if isinstance(self.execution, PaperExecution):
                        self.execution.on_market_trade(payload)
                    await self.strategy.on_event("kalshi_book", payload["ticker"])
                elif kind == "kalshi_book":
                    if isinstance(self.execution, PaperExecution):
                        self.execution.on_book_update(payload)
                    await self.strategy.on_event(kind, payload)
                else:
                    await self.strategy.on_event(kind, payload)
            except Exception:
                logger.exception("event handling failed for %s", kind)

    async def discovery(self) -> None:
        """Track the open Up/Down markets (current window + the next), feed
        strikes/close times to the strategy, and settle finished windows."""
        while not self._stop.is_set():
            try:
                markets = await self.rest.get_markets(series_ticker=self.cfg.series_ticker, status="open")
                now = time.time()
                active: list[tuple[float, dict]] = []
                for mkt in markets:
                    close_ts = _parse_close_ts(mkt)
                    if close_ts is None or close_ts <= now:
                        continue
                    active.append((close_ts, mkt))
                active.sort(key=lambda x: x[0])
                active = active[:2]  # current window + next

                for close_ts, mkt in active:
                    self.strategy.upsert_market(mkt["ticker"], close_ts, _parse_strike(mkt))
                self.ws.set_markets({mkt["ticker"] for _, mkt in active})

                active_tickers = {mkt["ticker"] for _, mkt in active}
                for ticker, m in list(self.strategy.markets.items()):
                    if ticker not in active_tickers and not m.settled and m.close_ts <= now:
                        self._pending_settlement.add(ticker)
                await self._settle_pending()
            except KalshiAPIError as exc:
                logger.warning("market discovery failed: %s", exc)
            except Exception:
                logger.exception("market discovery error")
            await asyncio.sleep(self.cfg.market_poll_seconds)

    async def _settle_pending(self) -> None:
        for ticker in list(self._pending_settlement):
            try:
                mkt = await self.rest.get_market(ticker)
            except KalshiAPIError as exc:
                logger.warning("settlement poll %s failed: %s", ticker, exc)
                continue
            result = (mkt.get("result") or "").lower()
            if result in ("yes", "no"):
                await self.strategy.settle_market(ticker, result == "yes")
                self.strategy.drop_market(ticker)
                self._pending_settlement.discard(ticker)
            elif mkt.get("status") in ("settled", "finalized") :
                # Settled but result field unrecognized -- don't guess.
                logger.warning("%s settled with unparseable result %r; positions left unrealized", ticker, mkt.get("result"))
                self._pending_settlement.discard(ticker)

    async def status(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(self.cfg.status_interval_s)
            feeds_up = ",".join(self.estimator.live_feeds()) or "none"
            brti = self.estimator.last_value
            parts = [
                f"BRTI={brti:,.2f}" if brti else "BRTI=warming",
                f"vol={self.vol.sigma_annual * 100:.0f}%ann" if self.vol.ready else "vol=warming",
                f"feeds=[{feeds_up}]",
                f"pnl_day=${self.risk.realized_pnl_today:,.2f}",
            ]
            if self.risk.halted:
                parts.append(f"HALTED({self.risk.halt_reason})")
            for m in self.strategy.markets.values():
                book = self.ws.books.get(m.ticker)
                bid, ask = book.bbo() if book else (None, None)
                pos = self.execution.position(m.ticker).pos
                tau = m.seconds_to_close(time.time())
                parts.append(
                    f"{m.ticker}[t-{max(tau, 0):.0f}s K={m.strike or 0:,.0f} "
                    f"fair={m.last_fair if m.last_fair is not None else float('nan'):.2f} "
                    f"mkt={_fmt_px(bid)}/{_fmt_px(ask)} pos={pos:+d}]"
                )
            logger.info(" | ".join(parts))

    # ---- lifecycle ----

    def _push(self, event: tuple) -> None:
        try:
            self.events.put_nowait(event)
        except asyncio.QueueFull:
            pass  # timer/brti ticks recur; dropping one is harmless

    async def run(self) -> None:
        logger.info(
            "starting: mode=%s env=%s series=%s order_api=%s feeds=%s",
            self.cfg.mode, self.cfg.kalshi_env, self.cfg.series_ticker,
            self.cfg.kalshi_order_api, ",".join(f.name for f in self.feeds),
        )
        if self.cfg.mode == "live":
            try:
                bal = await self.rest.get_balance()
                logger.info("Kalshi balance: %s", bal)
            except KalshiAPIError as exc:
                raise SystemExit(f"balance check failed -- fix credentials before going live: {exc}")

        tasks = [asyncio.create_task(f.run(), name=f"feed-{f.name}") for f in self.feeds]
        tasks += [
            asyncio.create_task(self.sampler(), name="sampler"),
            asyncio.create_task(self.ws.run(), name="kalshi-ws"),
            asyncio.create_task(self.discovery(), name="discovery"),
            asyncio.create_task(self.consumer(), name="consumer"),
            asyncio.create_task(self.timer(), name="timer"),
            asyncio.create_task(self.status(), name="status"),
        ]
        try:
            await self._stop.wait()
        finally:
            logger.info("shutting down...")
            for f in self.feeds:
                f.stop()
            self.ws.stop()
            if isinstance(self.execution, LiveExecution):
                with contextlib.suppress(Exception):
                    await self.execution.cancel_all()
                    logger.info("all live orders canceled")
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.rest.close()

    def request_stop(self) -> None:
        self._stop.set()


def _fmt_px(p: float | None) -> str:
    return f"{p * 100:.0f}c" if p is not None else "--"


def main() -> None:
    logging.basicConfig(
        level=getattr(logging, CONFIG.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if CONFIG.mode not in ("paper", "live"):
        raise SystemExit(f"MODE={CONFIG.mode!r} must be 'paper' or 'live' (backtests: python -m src.backtest)")
    if CONFIG.mode == "live" and CONFIG.kalshi_env == "prod":
        logger.warning("LIVE mode on PROD: real money. Ctrl-C now if that wasn't intentional.")

    bot = Bot(CONFIG)
    loop = asyncio.new_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, bot.request_stop)
    try:
        loop.run_until_complete(bot.run())
    finally:
        loop.close()


if __name__ == "__main__":
    main()
