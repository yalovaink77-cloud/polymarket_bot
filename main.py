import asyncio
import signal as sys_signal
import sys
import uuid
from typing import Dict

from dotenv import load_dotenv
from loguru import logger

load_dotenv()

from config import settings
from orderbook_tracker import OrderBookTracker, OrderBookSnapshot
from filters import SignalFilter, SignalResult
from risk_manager import RiskManager
from alerts import TelegramAlert
from executor import TradeExecutor

logger.remove()
logger.add(sys.stdout, level=settings.log_level, format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | {message}")
logger.add("logs/bot.log", level="DEBUG", rotation="10 MB", retention="7 days")

# Buraya gerçek token_id -> market_id eklenecek
MARKETS_TO_TRACK: Dict[str, str] = {}

class PolymarketBot:
    def __init__(self):
        self.risk_manager = RiskManager()
        self.telegram = TelegramAlert(on_approved=self._on_approved)
        self.executor = TradeExecutor(self.risk_manager)
        self.signal_filters: Dict[str, SignalFilter] = {
            token_id: SignalFilter(token_id, market_id)
            for token_id, market_id in MARKETS_TO_TRACK.items()
        }
        self.tracker = OrderBookTracker(
            token_ids=list(MARKETS_TO_TRACK.keys()),
            market_ids=MARKETS_TO_TRACK,
            on_snapshot=self._on_snapshot,
        )
        self._pending_trades: Dict[str, tuple] = {}
        self._loop = None

    def _on_snapshot(self, snap: OrderBookSnapshot):
        filt = self.signal_filters.get(snap.token_id)
        if not filt:
            return
        result = filt.evaluate(snap)
        if result is None or not result.should_alert:
            return
        risk = self.risk_manager.check(snap.market_id)
        if not risk.approved:
            logger.warning(f"Signal detected but risk check failed: {risk.reason}")
            return
        alert_id = str(uuid.uuid4())[:8]
        self._pending_trades[alert_id] = (result, risk)
        if self._loop and not self._loop.is_closed():
            asyncio.run_coroutine_threadsafe(
                self.telegram.send_signal_alert(alert_id, result, risk),
                self._loop,
            )

    def _on_approved(self, alert_id: str):
        entry = self._pending_trades.pop(alert_id, None)
        if not entry:
            return
        signal, risk = entry
        if settings.env == "development":
            self.executor.dry_run(signal, risk)
        else:
            self.executor.execute_trade(signal, risk)

    async def run(self):
        self._loop = asyncio.get_event_loop()
        logger.info("=" * 50)
        logger.info("Polymarket Bot starting")
        logger.info(f"ENV: {settings.env}")
        logger.info(f"Capital: ${settings.capital_total_usd:.0f}")
        logger.info(f"Markets tracked: {len(MARKETS_TO_TRACK)}")
        logger.info("=" * 50)

        if not MARKETS_TO_TRACK:
            logger.warning("No markets configured in MARKETS_TO_TRACK")

        await self.telegram.start()
        await self.telegram.send_message("🤖 *Polymarket Bot started*\n" + self.risk_manager.status_summary())

        self.tracker.start()

        stop_event = asyncio.Event()

        def _shutdown(*_):
            logger.info("Shutdown signal received")
            stop_event.set()

        for sig in (sys_signal.SIGINT, sys_signal.SIGTERM):
            sys_signal.signal(sig, _shutdown)

        logger.info("Bot running. Press Ctrl+C to stop.")
        await stop_event.wait()

        self.tracker.stop()
        await self.telegram.send_message("🛑 *Polymarket Bot stopped*")
        await self.telegram.stop()
        logger.info("Bot stopped cleanly.")

if __name__ == "__main__":
    import os
    os.makedirs("logs", exist_ok=True)
    bot = PolymarketBot()
    asyncio.run(bot.run())
