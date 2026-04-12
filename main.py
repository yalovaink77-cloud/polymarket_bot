import asyncio
import json
import signal as sys_signal
import sys
import time
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict

from dotenv import load_dotenv
from loguru import logger

load_dotenv()

# Railway / container ortamında logs/ dizini olmayabilir — önceden oluştur
Path("logs").mkdir(exist_ok=True)

from config import settings
from orderbook_tracker import OrderBookTracker, OrderBookSnapshot
from filters import SignalFilter
from risk_manager import RiskManager
from alerts import TelegramAlert
from executor import TradeExecutor
from weather import WeatherClient, WeatherSnapshot
from performance import PerformanceTracker, DryRunEvaluator
from update_weather_markets import WeatherMarketUpdater

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
        self.telegram = TelegramAlert(on_approved=self._on_approved if settings.require_telegram_approval else None)
        self.executor = TradeExecutor(self.risk_manager)
        self.performance = PerformanceTracker(settings.capital_total_usd)
        self.market_meta: Dict[str, dict] = MARKETS_META
        self.weather_client: WeatherClient | None = WeatherClient(settings.weather_api_key) if settings.weather_api_key else None
        self._weather_state: Dict[str, WeatherSnapshot] = {}
        self.market_updater = WeatherMarketUpdater(
            config_path="markets_config.json",
            on_updated=self._on_markets_updated,
        )
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
        # Aynı market için sinyal spam'ını önlemek: market_id -> son sinyal unix zamanı
        self._last_signal_time: Dict[str, float] = {}
        self._loop = None
        self._stop_event: asyncio.Event | None = None
        self._alive: bool = True
        self._live_mode_enabled = settings.env != "development"
        self._auto_live_warning_sent = False
        self.dry_run_evaluator = (
            DryRunEvaluator(
                horizon_seconds=settings.dry_run_eval_horizon_sec,
                min_trades=settings.dry_run_min_trades,
                min_win_rate=settings.dry_run_min_win_rate,
                min_net_pnl_usd=settings.dry_run_min_net_pnl_usd,
                pnl_floor_price=settings.dry_run_pnl_floor_price,
                state_file=settings.dry_run_state_file,
            )
            if settings.env == "development"
            else None
        )

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

    def _is_live_mode(self) -> bool:
        return self._live_mode_enabled

    def _send_async_message(self, text: str):
        if self._loop and not self._loop.is_closed():
            asyncio.run_coroutine_threadsafe(self.telegram.send_message(text), self._loop)

    def _track_dry_run_trade(self, trade_id: str, signal, risk):
        if self.dry_run_evaluator is None:
            return

        entry_mid_price = float(getattr(signal, "entry_price", 0.0) or 0.0)
        opened_at = None
        if entry_mid_price <= 0:
            snap = self.tracker.latest_snapshot(signal.token_id)
            if snap is None or snap.mid_price is None:
                logger.warning(f"Dry-run trade not tracked (missing mid price) | trade_id={trade_id} market={signal.market_id}")
                return
            entry_mid_price = snap.mid_price
            opened_at = snap.timestamp

        side = (getattr(signal, "side", "") or "").upper()
        if side not in {"YES", "NO"}:
            side = "YES" if signal.imbalance_ratio > 1.0 else "NO"
        direction = f"BUY {side}"
        tracked = self.dry_run_evaluator.record_open_trade(
            trade_id=trade_id,
            token_id=signal.token_id,
            market_id=signal.market_id,
            direction="BUY_YES" if side == "YES" else "BUY_NO",
            size_usd=risk.max_size_usd,
            entry_mid_price=entry_mid_price,
            opened_at=opened_at,
        )

        if tracked:
            summary = self.dry_run_evaluator.summary()
            logger.info(
                f"Dry-run trade tracked | id={trade_id} market={signal.market_id} direction={direction} "
                f"entry_mid={entry_mid_price:.4f} pending={summary['open_trades']}"
            )

    def _process_dry_run_outcomes(self, snap: OrderBookSnapshot):
        if self.dry_run_evaluator is None:
            return

        outcomes = self.dry_run_evaluator.resolve_with_snapshot(
            token_id=snap.token_id,
            market_id=snap.market_id,
            exit_mid_price=snap.mid_price,
            timestamp=snap.timestamp,
            stop_loss_pct=settings.dry_run_stop_loss_pct,
            take_profit_pct=settings.dry_run_take_profit_pct,
        )
        if not outcomes:
            return

        history_path = Path("logs/trade_history.jsonl")
        history_path.parent.mkdir(exist_ok=True)

        for outcome in outcomes:
            status = "WIN" if outcome.won else "LOSS"
            sl_tag = " [STOP-LOSS]" if outcome.stop_loss_hit else ""
            tp_tag = " [TAKE-PROFIT]" if outcome.take_profit_hit else ""
            logger.info(
                f"Dry-run closed | {status}{sl_tag}{tp_tag} | market={outcome.market_id} trade_id={outcome.trade_id} "
                f"entry={outcome.entry_mid_price:.4f} exit={outcome.exit_mid_price:.4f} "
                f"pnl=${outcome.pnl_usd:.2f} ret={100*outcome.return_pct:.2f}%"
            )
            self.risk_manager.remove_position(outcome.trade_id)

            # Trade geçmişine yaz — sinyal parametreleriyle birlikte
            meta = self._decisions.get(outcome.trade_id, {})
            record = {
                "trade_id": outcome.trade_id,
                "market_id": outcome.market_id,
                "token_id": outcome.token_id,
                "direction": outcome.direction,
                "direction_type": meta.get("direction_type", ""),
                "imbalance_ratio": meta.get("imbalance_ratio", 0.0),
                "overreaction_score": meta.get("overreaction_score", 0.0),
                "mid_zscore": meta.get("mid_zscore", 0.0),
                "composite_score": meta.get("composite_score", 0.0),
                "size_usd": outcome.size_usd,
                "entry_mid_price": outcome.entry_mid_price,
                "exit_mid_price": outcome.exit_mid_price,
                "pnl_usd": round(outcome.pnl_usd, 4),
                "return_pct": round(outcome.return_pct, 6),
                "won": outcome.won,
                "stop_loss_hit": outcome.stop_loss_hit,
                "opened_at": outcome.opened_at.isoformat(),
                "closed_at": outcome.closed_at.isoformat(),
            }
            try:
                with history_path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(record) + "\n")
            except Exception as exc:
                logger.warning(f"trade_history yazma hatası: {exc}")

        summary = self.dry_run_evaluator.summary()
        closed = summary["closed_trades"]
        win_rate = 100 * summary["win_rate"]
        net_pnl = summary["net_pnl_usd"]
        open_trades = summary["open_trades"]

        logger.info(
            f"Dry-run stats | closed={closed} win_rate={win_rate:.2f}% net_pnl=${net_pnl:.2f} open={open_trades}"
        )
        self._send_async_message(
            f"🧪 *Dry-run update*\n"
            f"Closed: `{closed}` | Win rate: `{win_rate:.2f}%`\n"
            f"Net PnL: `${net_pnl:.2f}` | Open: `{open_trades}`"
        )

        self._maybe_enable_live_mode()

    def _maybe_enable_live_mode(self):
        if self.dry_run_evaluator is None:
            return
        if self._is_live_mode():
            return
        if not settings.auto_switch_to_live:
            return
        if not self.dry_run_evaluator.ready_for_live():
            return

        has_credentials = all(
            [
                settings.poly_api_key.strip(),
                settings.poly_api_secret.strip(),
                settings.poly_api_passphrase.strip(),
                settings.poly_private_key.strip(),
            ]
        )
        if not has_credentials:
            if not self._auto_live_warning_sent:
                self._auto_live_warning_sent = True
                logger.warning("Dry-run criteria passed but live credentials are incomplete. Staying in DRY RUN mode.")
                self._send_async_message(
                    "⚠️ *Dry-run başarılı* fakat canlı geçiş için API/private key eksik. `DRY RUN` modunda devam ediliyor."
                )
            return

        self._live_mode_enabled = True
        summary = self.dry_run_evaluator.summary()
        logger.warning(
            f"Dry-run criteria passed. LIVE mode enabled automatically | closed={summary['closed_trades']} "
            f"win_rate={100*summary['win_rate']:.2f}% net_pnl=${summary['net_pnl_usd']:.2f}"
        )
        self._send_async_message(
            "✅ *Dry-run başarılı*\n"
            f"Closed: `{summary['closed_trades']}` | Win rate: `{100*summary['win_rate']:.2f}%`\n"
            f"Net PnL: `${summary['net_pnl_usd']:.2f}`\n"
            "🚀 Bot canlı trade moduna geçti."
        )

    def _execute_signal(self, signal, risk):
        live_mode = self._is_live_mode()

        if live_mode:
            order_id = self.executor.execute_trade(signal, risk)
        else:
            order_id = self.executor.dry_run(signal, risk)
            if order_id:
                self._track_dry_run_trade(order_id, signal, risk)

        direction = "BUY YES" if signal.side == "YES" else "BUY NO"
        side = (getattr(signal, "side", "") or "").upper() or ("YES" if signal.imbalance_ratio > 1.0 else "NO")
        entry_price = float(getattr(signal, "entry_price", 0.0) or 0.0)
        if entry_price <= 0:
            latest = self.tracker.latest_snapshot(signal.token_id)
            if latest and latest.mid_price:
                entry_price = latest.mid_price

        if order_id and entry_price > 0 and risk.max_size_usd > 0:
            self.performance.record_trade(
                trade_id=order_id,
                market_id=signal.market_id,
                token_id=signal.token_id,
                side=side,
                entry_price=entry_price,
                size_usd=risk.max_size_usd,
            )
            self._decisions[order_id] = {
                "market_id": signal.market_id,
                "token_id": signal.token_id,
                "side": side,
                "entry_price": entry_price,
                "size_usd": risk.max_size_usd,
                "settled": False,
                # Sinyal metadata — analiz için
                "direction_type": getattr(signal, "direction_type", ""),
                "imbalance_ratio": float(getattr(signal, "imbalance_ratio", 0.0)),
                "overreaction_score": float(getattr(signal, "overreaction_score", 0.0)),
                "mid_zscore": float(getattr(signal, "mid_zscore", 0.0)),
                "composite_score": float(getattr(signal, "composite_score", 0.0)),
            }

        mode = "LIVE" if live_mode else "DRY RUN"
        status = "EXECUTED" if order_id else "FAILED"

        pnl_line = ""
        if not live_mode and self.dry_run_evaluator is not None:
            summary = self.dry_run_evaluator.summary()
            wins = summary["wins"]
            losses = summary["losses"]
            win_rate = 100 * summary["win_rate"]
            net_pnl = summary["net_pnl_usd"]
            pnl_emoji = "📈" if net_pnl >= 0 else "📉"
            pnl_line = (
                f"\n\n{pnl_emoji} *Dry-run P&L*\n"
                f"Kapalı: `{wins}W / {losses}L` (Win: `{win_rate:.1f}%`)\n"
                f"Net PnL: `${net_pnl:.2f}`"
            )
            logger.info(
                f"Dry-run monitor | closed={summary['closed_trades']} open={summary['open_trades']} "
                f"win_rate={win_rate:.2f}% net_pnl=${net_pnl:.2f}"
            )

        market_question = ""
        meta = self.market_meta.get(signal.token_id)
        if not meta:
            meta = next((m for m in self.market_meta.values() if m.get('market_id') == signal.market_id), None)
        if meta:
            q = meta.get('market_question', '')
            if q:
                market_question = f"\nSoru: _{q}_"

        self._send_async_message(
            f"🤖 *Auto Trade {status}* ({mode})\n"
            f"🕐 `{datetime.now(timezone(timedelta(hours=3))).strftime('%d.%m.%Y %H:%M:%S')}` (UTC+3)\n"
            f"Market: `{signal.market_id}`"
            + market_question
            + f"\nDirection: *{direction}*\n"
            f"Size: `${risk.max_size_usd:.2f}`\n"
            f"Order ID: `{order_id or 'N/A'}`"
            + pnl_line
        )

    def _on_snapshot(self, snap: OrderBookSnapshot):
        self._process_dry_run_outcomes(snap)

        filt = self.signal_filters.get(snap.token_id)
        if not filt:
            return
        if not self._weather_allows_trade(snap.token_id):
            return

        # Near-resolution filtresi: market kapanmak üzereyken imbalance sinyal değil gürültüdür
        mid = snap.mid_price
        if mid is not None and not (settings.near_resolution_min < mid < settings.near_resolution_max):
            logger.debug(
                f"Near-resolution skip: market={snap.market_id[:10]} mid={mid:.3f} "
                f"(filter=[{settings.near_resolution_min}, {settings.near_resolution_max}])"
            )
            return

        result = filt.evaluate(snap)
        if result is None or not result.should_alert:
            return

        # Per-market cooldown: aynı market için çok sık sinyal açılmasını önle
        now = time.time()
        last_signal = self._last_signal_time.get(snap.market_id, 0.0)
        if now - last_signal < settings.signal_cooldown_sec:
            return
        # Race condition'ı önlemek için cooldown'ı hemen işaretle
        self._last_signal_time[snap.market_id] = now

        # Performans takibi için giriş fiyatı ve yön bilgisini doldur
        result.entry_price = snap.mid_price or 0.0

        # Yön belirleme: sadece MEAN-REV stratejisi aktif.
        # Analizde MOMENTUM %23 win rate ile yapısal kayıp üretiyor — devre dışı bırakıldı.
        result.direction_type = "MEAN-REV"
        # Mean-reversion: yüksek bid baskısı = YES overbought → BUY NO; tersi BUY YES
        result.side = "NO" if result.imbalance_ratio > 1.0 else "YES"

        logger.debug(
            f"Direction: {result.direction_type} → {result.side} | "
            f"overreact={result.overreaction_score:.2f} imbalance={result.imbalance_ratio:.2f} "
            f"price_dir={result.price_direction}"
        )
            risk = self.risk_manager.check(
                snap.market_id,
                composite_score=float(getattr(result, "composite_score", 0.0)),
                entry_price=float(getattr(result, "entry_price", 0.5) or 0.5),
            )
        if not risk.approved:
            logger.warning(f"Signal detected but risk check failed: {risk.reason}")
            if "No available capital" in risk.reason:
                self._handle_bankruptcy()
            return

        if settings.auto_execute:
            self._execute_signal(result, risk)
            return

        if settings.require_telegram_approval:
            alert_id = str(uuid.uuid4())[:8]
            self._pending_trades[alert_id] = (result, risk)
            if self._loop and not self._loop.is_closed():
                asyncio.run_coroutine_threadsafe(
                    self.telegram.send_signal_alert(alert_id, result, risk),
                    self._loop,
                )
        else:
            self._execute_signal(result, risk)

    def _on_approved(self, alert_id: str):
        entry = self._pending_trades.pop(alert_id, None)
        if not entry:
            return
        signal, risk = entry
        self._execute_signal(signal, risk)

    async def run(self):
        self._loop = asyncio.get_event_loop()
        stop_event = asyncio.Event()
        self._stop_event = stop_event

        logger.info("=" * 50)
        logger.info("Polymarket Bot starting")
        logger.info(f"ENV: {settings.env}")
        logger.info(f"Approval mode: {'manual' if settings.require_telegram_approval else 'auto'}")
        logger.info(f"Execution mode: {'LIVE' if self._is_live_mode() else 'DRY RUN'}")
        logger.info(f"Capital: ${settings.capital_total_usd:.0f}")
        logger.info(f"Markets tracked: {len(MARKETS_TO_TRACK)}")
        if self.dry_run_evaluator is not None:
            criteria = self.dry_run_evaluator.criteria
            summary = self.dry_run_evaluator.summary()
            logger.info(
                f"Dry-run criteria: min_trades={criteria['min_trades']} min_win_rate={100*criteria['min_win_rate']:.2f}% "
                f"min_net_pnl=${criteria['min_net_pnl_usd']:.2f} horizon={settings.dry_run_eval_horizon_sec}s"
            )
            logger.info(
                f"Dry-run progress: closed={summary['closed_trades']} win_rate={100*summary['win_rate']:.2f}% "
                f"net_pnl=${summary['net_pnl_usd']:.2f} open={summary['open_trades']}"
            )
            logger.info(f"Auto switch to live: {'enabled' if settings.auto_switch_to_live else 'disabled'}")
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
        tasks.append(asyncio.create_task(self.market_updater.run_forever(), name="weather-market-updater"))
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
        await self.market_updater.close()

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
                # Dry-run değerlendirici varsa daha detaylı özet Telegram'a gönder
                if self.dry_run_evaluator is not None:
                    dr = self.dry_run_evaluator.summary()
                    wins = dr["wins"]
                    losses = dr["losses"]
                    win_rate = 100 * dr["win_rate"]
                    net_pnl = dr["net_pnl_usd"]
                    open_trades = dr["open_trades"]
                    pnl_emoji = "📈" if net_pnl >= 0 else "📉"
                    self._send_async_message(
                        f"{pnl_emoji} *5 Dakikalık P&L Özeti*\n"
                        f"Kapalı trade: `{wins}W / {losses}L`\n"
                        f"Win rate: `{win_rate:.1f}%`\n"
                        f"Net PnL: `${net_pnl:.2f}`\n"
                        f"Açık pozisyon: `{open_trades}`\n"
                        f"Equity: `${summary['equity_usd']:.2f}`"
                    )
                else:
                    pnl_emoji = "📈" if summary['pnl_usd'] >= 0 else "📉"
                    self._send_async_message(
                        f"{pnl_emoji} *5 Dakikalık P&L Özeti*\n"
                        f"Equity: `${summary['equity_usd']:.2f}`\n"
                        f"PnL: `${summary['pnl_usd']:.2f}`\n"
                        f"Açık pozisyon: `{summary['open_positions']}`"
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
                            params = {"condition_id": market_id}
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

    async def _on_markets_updated(self, updated: dict) -> None:
        """
        WeatherMarketUpdater yeni token_id'leri kaydettiğinde çağrılır.
        markets_config.json'dan güncel metadata'yı yeniden yükler; OrderBookTracker
        için yeni token'ları kayıt dışı bırakma işlemi bir bot yeniden başlatması
        gerektirir (tracker thread-safe hot-reload desteklemiyor).
        """
        global MARKETS_TO_TRACK, MARKETS_META
        MARKETS_TO_TRACK = load_markets_config()
        MARKETS_META = load_markets_metadata()
        self.market_meta = MARKETS_META
        city_list = ", ".join(sorted(updated.keys()))
        logger.info(f"markets_config.json yeniden yüklendi. Güncellenen şehirler: {city_list}")
        try:
            await self.telegram.send_message(
                f"🌤 *Weather Markets Updated*\n"
                f"Güncellenen şehirler: `{city_list}`\n"
                f"_Yeni token'ların aktif takibi için botu yeniden başlatın._"
            )
        except Exception as exc:
            logger.warning(f"Telegram market update bildirimi gönderilemedi: {exc}")

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
