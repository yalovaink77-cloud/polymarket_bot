import asyncio
import json
import signal as sys_signal
import sys
import uuid
from pathlib import Path
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
from weather import WeatherClient, WeatherSnapshot
from performance import PerformanceTracker

logger.remove()
logger.add(sys.stdout, level=settings.log_level, format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | {message}")
logger.add("logs/bot.log", level="DEBUG", rotation="10 MB", retention="7 days")

def load_markets_config() -> Dict[str, str]:
    """
    markets_config.json dosyasından token_id -> market_id eşleştirmelerini yükler.
    Dosya yoksa veya bozuksa boş sözlük döner ve loga uyarı yazar.
    """
    config_path = Path("markets_config.json")
    if not config_path.exists():
        logger.warning("markets_config.json bulunamadı — şu anda hiçbir market takip edilmeyecek.")
        return {}

    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.error(f"markets_config.json okunamadı: {exc}")
        return {}

    markets: Dict[str, str] = {}
    if isinstance(data, list):
        for entry in data:
            token_id = entry.get("token_id")
            market_id = entry.get("market_id")
            if not token_id or not market_id:
                logger.warning(f"Geçersiz market girdisi atlandı: {entry}")
                continue
            markets[token_id] = market_id
    elif isinstance(data, dict):
        # Eğer kullanıcı doğrudan {token_id: market_id} şeklinde sözlük yazmışsa onu da kabul et
        markets = {str(k): str(v) for k, v in data.items()}
    else:
        logger.error("markets_config.json beklenen formatta değil (liste veya sözlük olmalı).")
        return {}

    logger.info(f"markets_config.json içinden {len(markets)} market yüklendi.")
    return markets


def load_markets_metadata() -> Dict[str, dict]:
    """
    markets_config.json içindeki ham girdileri token_id -> metadata şeklinde döndürür.
    Şehir / ülke / hava durumu kuralı gibi ek alanlar bu sözlükte saklanır.
    """
    config_path = Path("markets_config.json")
    if not config_path.exists():
        return {}

    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.error(f"markets_config.json okunamadı (metadata): {exc}")
        return {}

    meta: Dict[str, dict] = {}
    if isinstance(data, list):
        for entry in data:
            token_id = entry.get("token_id")
            if not token_id:
                continue
            meta[str(token_id)] = entry
    elif isinstance(data, dict):
        for token_id, entry in data.items():
            if isinstance(entry, dict):
                merged = {"token_id": token_id}
                merged.update(entry)
                meta[str(token_id)] = merged
            else:
                meta[str(token_id)] = {"token_id": token_id, "market_id": str(entry)}
    return meta


# markets_config.json içinden gerçek token_id -> market_id eşleştirmeleri okunur
MARKETS_TO_TRACK: Dict[str, str] = load_markets_config()
MARKETS_META: Dict[str, dict] = load_markets_metadata()

class PolymarketBot:
    def __init__(self):
        self.risk_manager = RiskManager()
        self.telegram = TelegramAlert(on_approved=self._on_approved)
        self.executor = TradeExecutor(self.risk_manager)
        self.performance = PerformanceTracker(settings.capital_total_usd)
        self.market_meta: Dict[str, dict] = MARKETS_META
        self.weather_client: WeatherClient | None = WeatherClient(settings.weather_api_key) if settings.weather_api_key else None
        self._weather_state: Dict[str, WeatherSnapshot] = {}
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
        # Otomatik verilen kararların çözüm (win/lose) takibi için
        self._decisions: Dict[str, dict] = {}
        self._loop = None
        self._stop_event: asyncio.Event | None = None
        self._alive: bool = True

    def _weather_allows_trade(self, token_id: str) -> bool:
        """
        Hava durumu temelli basit bir filtre.
        markets_config.json içinde ilgili token için örneğin:
        {
          "token_id": "...",
          "market_id": "...",
          "city": "Istanbul",
          "country": "TR",
          "required_condition": "RAIN"
        }
        gibi bir kayıt varsa, anlık hava durumu bu şartı sağlamıyorsa sinyali reddeder.
        """
        meta = self.market_meta.get(token_id) if self.market_meta else None
        if not meta:
            # Hiç metadata yoksa hava durumu filtresi uygulama
            return True

        required_condition = str(meta.get("required_condition") or "").upper().strip()
        if not required_condition:
            return True

        weather = self._weather_state.get(token_id)
        if not weather:
            # Şu an için hava durumu bilgisi yoksa aşırı agresif olmamak adına trade açma
            logger.debug(f"No weather data yet for token {token_id}; skipping weather-gated signal.")
            return False

        if weather.condition != required_condition:
            logger.info(
                f"Weather condition mismatch for {token_id}: "
                f"required={required_condition}, current={weather.condition}"
            )
            return False

        return True

    def _handle_bankruptcy(self):
        """
        Tüm sermaye fiilen tükendiğinde botu 'öldürür' ve nazikçe durdurur.
        """
        if not self._alive:
            return
        self._alive = False
        logger.critical("💀 Bot tüm sermayeyi kaybetti — simülasyon sona erdi.")
        if self._loop and self._stop_event and not self._stop_event.is_set():
            async def _notify_and_stop():
                try:
                    await self.telegram.send_message(
                        "💀 *Bot öldü*\n"
                        "Tüm sermaye tükendi veya kullanılabilir sermaye kalmadı. "
                        "Süreç güvenli şekilde durduruluyor."
                    )
                finally:
                    self._stop_event.set()

            asyncio.run_coroutine_threadsafe(_notify_and_stop(), self._loop)

    def _on_snapshot(self, snap: OrderBookSnapshot):
        filt = self.signal_filters.get(snap.token_id)
        if not filt:
            return
        if not self._weather_allows_trade(snap.token_id):
            return
        result = filt.evaluate(snap)
        if result is None or not result.should_alert:
            return
        # Performans takibi için giriş fiyatı ve yön bilgisini doldur
        result.entry_price = snap.mid_price or 0.0
        result.side = "YES" if result.imbalance_ratio > 1.0 else "NO"
        risk = self.risk_manager.check(snap.market_id)
        if not risk.approved:
            logger.warning(f"Signal detected but risk check failed: {risk.reason}")
            if "No available capital" in risk.reason:
                self._handle_bankruptcy()
            return
        # Eğer auto_execute açıksa, onay beklemeden hemen trade et
        if settings.auto_execute:
            trade_id = None
            if settings.env == "development":
                trade_id = self.executor.dry_run(result, risk)
            else:
                trade_id = self.executor.execute_trade(result, risk)

            if trade_id:
                # Performans kaydı
                self.performance.record_trade(
                    trade_id=trade_id,
                    market_id=result.market_id,
                    token_id=result.token_id,
                    side=result.side,
                    entry_price=result.entry_price,
                    size_usd=risk.max_size_usd,
                )
                # Çözüm (win/lose) takibi için karar kaydı
                self._decisions[trade_id] = {
                    "market_id": result.market_id,
                    "token_id": result.token_id,
                    "side": result.side,
                    "entry_price": result.entry_price,
                    "size_usd": risk.max_size_usd,
                    "settled": False,
                }
                logger.info(
                    f"AUTO-EXECUTED trade | market={result.market_id} "
                    f"side={result.side} size=${risk.max_size_usd:.2f} id={trade_id}"
                )
                if self._loop and not self._loop.is_closed():
                    async def _notify():
                        await self.telegram.send_message(
                            f"⚡ *Auto trade executed*\n\n"
                            f"Market: `{result.market_id}`\n"
                            f"Side: *{result.side}*\n"
                            f"Size: `${risk.max_size_usd:.2f}`\n"
                            f"Composite: `{result.composite_score:.3f}`\n"
                            f"Trade ID: `{trade_id}`"
                        )

                    asyncio.run_coroutine_threadsafe(_notify(), self._loop)
            return

        # Aksi halde, eski davranış: Telegram üzerinden onay iste
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
        trade_id = None
        if settings.env == "development":
            trade_id = self.executor.dry_run(signal, risk)
        else:
            trade_id = self.executor.execute_trade(signal, risk)

        if trade_id:
            size_usd = risk.max_size_usd
            self.performance.record_trade(
                trade_id=trade_id,
                market_id=signal.market_id,
                token_id=signal.token_id,
                side=signal.side or ("YES" if signal.imbalance_ratio > 1.0 else "NO"),
                entry_price=signal.entry_price or 0.0,
                size_usd=size_usd,
            )

    async def run(self):
        self._loop = asyncio.get_event_loop()
        stop_event = asyncio.Event()
        self._stop_event = stop_event

        logger.info("=" * 50)
        logger.info("Polymarket Bot starting")
        logger.info(f"ENV: {settings.env}")
        logger.info(f"Capital: ${settings.capital_total_usd:.0f}")
        logger.info(f"Markets tracked: {len(MARKETS_TO_TRACK)}")
        logger.info("=" * 50)

        if not MARKETS_TO_TRACK:
            logger.error("No markets configured in MARKETS_TO_TRACK. Lütfen markets_config.json dosyasını doldurun.")
            return

        await self.telegram.start()
        await self.telegram.send_message(
            "🤖 *Polymarket Bot started*\n"
            + self.risk_manager.status_summary()
            + "\n\n"
            "Bu bir canlılık simülasyonudur: sermaye sıfırlanırsa bot *ölecek* ve kendini durduracaktır."
        )

        self.tracker.start()

        tasks = []
        if self.weather_client and self.market_meta:
            tasks.append(asyncio.create_task(self._weather_loop(), name="weather-poller"))
        # Periyodik olarak sanal PnL durumunu logla
        tasks.append(asyncio.create_task(self._performance_loop(), name="performance-reporter"))
        # Market çözüldüğünde trade'in doğru/yanlış olduğunu tespit et
        tasks.append(asyncio.create_task(self._resolution_loop(), name="resolution-watcher"))

        def _shutdown(*_):
            logger.info("Shutdown signal received")
            stop_event.set()

        for sig in (sys_signal.SIGINT, sys_signal.SIGTERM):
            sys_signal.signal(sig, _shutdown)

        logger.info("Bot running. Press Ctrl+C to stop.")
        await stop_event.wait()

        for task in tasks:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        self.tracker.stop()
        await self.telegram.send_message("🛑 *Polymarket Bot stopped*")
        await self.telegram.stop()

        if self.weather_client:
            await self.weather_client.close()

        logger.info("Bot stopped cleanly.")

    async def _performance_loop(self):
        """
        Belirli aralıklarla portföyün yaklaşık PnL özetini loglar.
        """
        interval = 300  # 5 dakika
        logger.info("Performance reporter started (interval=300s)")
        while self._stop_event and not self._stop_event.is_set():
            try:
                summary = self.performance.mark_to_market(
                    lambda token_id: (self.tracker.latest_snapshot(token_id).mid_price
                                      if self.tracker.latest_snapshot(token_id) else None)
                )
                logger.info(
                    f"PnL Summary — equity=${summary['equity_usd']:.2f} "
                    f"pnl=${summary['pnl_usd']:.2f} "
                    f"open_positions={summary['open_positions']}"
                )
            except Exception as exc:
                logger.error(f"Performance reporter error: {exc}")
            await asyncio.sleep(interval)

    async def _resolution_loop(self):
        """
        Açık trade'ler için Polymarket API üzerinden market çözümünü kontrol eder
        ve her trade'in doğru/yanlış olduğunu Telegram'dan bildirir.
        """
        import aiohttp

        interval = 900  # 15 dakika
        logger.info("Resolution watcher started (interval=900s)")
        while self._stop_event and not self._stop_event.is_set():
            try:
                open_decisions = {
                    tid: rec
                    for tid, rec in self._decisions.items()
                    if not rec.get("settled")
                }
                market_ids = sorted({rec["market_id"] for rec in open_decisions.values()})
                if market_ids:
                    timeout = aiohttp.ClientTimeout(total=5)
                    async with aiohttp.ClientSession(timeout=timeout) as session:
                        for market_id in market_ids:
                            url = "https://gamma-api.polymarket.com/markets"
                            params = {"id": market_id}
                            try:
                                async with session.get(url, params=params) as resp:
                                    if resp.status != 200:
                                        continue
                                    data = await resp.json()
                            except Exception as exc:
                                logger.error(f"Resolution fetch failed for market {market_id}: {exc}")
                                continue

                            if not data:
                                continue
                            market = data[0]
                            if not market.get("closed"):
                                # Henüz çözülmemiş / kapanmamış
                                continue

                            # outcomes ve outcomePrices alanları string JSON olarak geliyor
                            import json as _json

                            outcomes_raw = market.get("outcomes") or "[]"
                            prices_raw = market.get("outcomePrices") or "[]"
                            try:
                                outcomes = (
                                    _json.loads(outcomes_raw)
                                    if isinstance(outcomes_raw, str)
                                    else outcomes_raw
                                )
                                prices = (
                                    [_json.loads(p) for p in prices_raw]
                                    if isinstance(prices_raw, str)
                                    else prices_raw
                                )
                            except Exception:
                                # Fallback: çözüm bilgisi alınamazsa geç
                                continue
                            if not outcomes or not prices:
                                continue

                            try:
                                prices_f = [float(p) for p in prices]
                            except Exception:
                                continue
                            winner_idx = max(range(len(prices_f)), key=lambda i: prices_f[i])
                            winner_outcome = str(outcomes[winner_idx]).upper()

                            # Bu marketteki tüm açık trade'leri sonuçlandır
                            for trade_id, rec in list(open_decisions.items()):
                                if rec["market_id"] != market_id or rec.get("settled"):
                                    continue
                                side = str(rec["side"]).upper() or "YES"
                                entry_price = float(rec.get("entry_price") or 0.0)
                                size_usd = float(rec.get("size_usd") or 0.0)
                                if entry_price <= 0 or size_usd <= 0:
                                    continue

                                shares = size_usd / entry_price
                                if side == "YES":
                                    final_price = 1.0 if winner_outcome == "YES" else 0.0
                                    pnl = shares * (final_price - entry_price)
                                else:
                                    entry_no = 1.0 - entry_price
                                    final_no = 1.0 if winner_outcome == "NO" else 0.0
                                    pnl = shares * (final_no - entry_no)

                                won = pnl >= 0
                                rec["settled"] = True
                                rec["won"] = won
                                rec["pnl_usd"] = pnl
                                self._decisions[trade_id] = rec

                                logger.info(
                                    f"Trade resolved | id={trade_id} market={market_id} "
                                    f"side={side} outcome={winner_outcome} pnl=${pnl:.2f}"
                                )
                                if self._loop and not self._loop.is_closed():
                                    async def _notify(trade_id=trade_id, side=side, pnl=pnl,
                                                      winner_outcome=winner_outcome, market_id=market_id):
                                        icon = "✅" if pnl >= 0 else "❌"
                                        verdict = "WON" if pnl >= 0 else "LOST"
                                        await self.telegram.send_message(
                                            f"{icon} *Trade {verdict}* \n\n"
                                            f"Market: `{market_id}`\n"
                                            f"Side: *{side}*\n"
                                            f"Outcome: *{winner_outcome}*\n"
                                            f"PnL (sim): `${pnl:+.2f}`\n"
                                            f"Trade ID: `{trade_id}`"
                                        )

                                    asyncio.run_coroutine_threadsafe(_notify(), self._loop)

            except Exception as exc:
                logger.error(f"Resolution watcher error: {exc}")

            await asyncio.sleep(interval)

    async def _weather_loop(self):
        """
        Takip edilen tüm tokenler için periyodik hava durumu güncellemesi.
        markets_config.json içinde ilgili kayıtta 'city' (ve opsiyonel 'country') alanı bekler.
        """
        poll_interval = max(15, int(getattr(settings, "weather_poll_interval", 60)))
        logger.info(f"Weather poller started (interval={poll_interval}s)")
        while self._stop_event and not self._stop_event.is_set():
            try:
                for token_id, meta in (self.market_meta or {}).items():
                    city = meta.get("city")
                    if not city:
                        continue
                    country = meta.get("country")
                    snapshot = await self.weather_client.fetch_city_weather(city, country)  # type: ignore[union-attr]
                    if snapshot:
                        self._weather_state[token_id] = snapshot
            except Exception as exc:
                logger.error(f"Weather poller error: {exc}")
            await asyncio.sleep(poll_interval)

if __name__ == "__main__":
    import os
    os.makedirs("logs", exist_ok=True)
    bot = PolymarketBot()
    asyncio.run(bot.run())
