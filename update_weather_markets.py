"""
update_weather_markets.py

Polymarket Gamma API'den hava durumu market token_id'lerini günlük olarak çeker
ve markets_config.json dosyasını günceller.

Zamanlama : Her gün 10:05 UTC (13:05 Türkiye saati)
Hedef tarih: çalışma anından 4 gün sonrası
Scheduler : asyncio tabanlı, dışarıdan cron gerekmez

Entegrasyon:
    from update_weather_markets import WeatherMarketUpdater
    updater = WeatherMarketUpdater()
    asyncio.create_task(updater.run_forever())
"""

import asyncio
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Callable, Optional

import aiohttp
from loguru import logger

# ---------------------------------------------------------------------------
# Şehir konfigürasyonu
# slug_name  : Polymarket slug'ında kullanılan şehir adı
# city       : markets_config.json'daki "city" alanıyla eşleşmeli
# ---------------------------------------------------------------------------
_CITY_MAP: list[dict] = [
    {
        "slug_name": "nyc",
        "city": "New York",
        "country": "US",
        "lat": 40.7128,
        "lon": -74.0060,
        "noaa_station": "KNYC",
        "settlement_station": "Central Park",
    },
    {
        "slug_name": "london",
        "city": "London",
        "country": "GB",
        "lat": 51.5074,
        "lon": -0.1278,
        "noaa_station": None,
        "settlement_station": "Heathrow Airport",
    },
    {
        "slug_name": "seoul",
        "city": "Seoul",
        "country": "KR",
        "lat": 37.5665,
        "lon": 126.9780,
        "noaa_station": None,
        "settlement_station": "Seoul Weather Station",
    },
    {
        "slug_name": "shanghai",
        "city": "Shanghai",
        "country": "CN",
        "lat": 31.2304,
        "lon": 121.4737,
        "noaa_station": None,
        "settlement_station": "Pudong Airport ZSPD",
    },
    {
        "slug_name": "miami",
        "city": "Miami",
        "country": "US",
        "lat": 25.7617,
        "lon": -80.1918,
        "noaa_station": "KMIA",
        "settlement_station": "Miami International Airport",
    },
    {
        "slug_name": "atlanta",
        "city": "Atlanta",
        "country": "US",
        "lat": 33.7490,
        "lon": -84.3880,
        "noaa_station": "KATL",
        "settlement_station": "Hartsfield-Jackson Atlanta International Airport",
    },
    {
        "slug_name": "tokyo",
        "city": "Tokyo",
        "country": "JP",
        "lat": 35.6762,
        "lon": 139.6503,
        "noaa_station": None,
        "settlement_station": "Tokyo / JMA",
    },
    {
        "slug_name": "paris",
        "city": "Paris",
        "country": "FR",
        "lat": 48.8566,
        "lon": 2.3522,
        "noaa_station": None,
        "settlement_station": "Paris-Orly / Météo-France",
    },
    {
        "slug_name": "chicago",
        "city": "Chicago",
        "country": "US",
        "lat": 41.8781,
        "lon": -87.6298,
        "noaa_station": "KORD",
        "settlement_station": "O'Hare Airport",
    },
    {
        "slug_name": "toronto",
        "city": "Toronto",
        "country": "CA",
        "lat": 43.6532,
        "lon": -79.3832,
        "noaa_station": None,
        "settlement_station": "Toronto Pearson Airport",
    },
]

# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------

class WeatherMarketUpdater:
    """
    Polymarket hava durumu market token_id'lerini günlük günceller.

    Params:
        config_path   : markets_config.json dosyasının yolu (varsayılan: proje kökü)
        lookahead_days: Kaç gün ileriyi hedefleyeceğiz (varsayılan: 4)
        schedule_utc  : (saat, dakika) tuple'ı — varsayılan (10, 5)
        on_updated    : Güncelleme tamamlandığında çağrılacak async callback
                        Parametre: Dict[city_name, new_token_id]
    """

    GAMMA_BASE = "https://gamma-api.polymarket.com/events"
    REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=15)

    def __init__(
        self,
        config_path: str | Path = "markets_config.json",
        lookahead_days: int = 4,
        schedule_utc: tuple[int, int] = (10, 5),
        on_updated: Optional[Callable] = None,
    ) -> None:
        self._config_path = Path(config_path)
        self._lookahead_days = lookahead_days
        self._schedule_hour, self._schedule_minute = schedule_utc
        self._on_updated = on_updated
        self._session: Optional[aiohttp.ClientSession] = None

    # ------------------------------------------------------------------
    # Public scheduler entrypoint
    # ------------------------------------------------------------------

    async def run_forever(self) -> None:
        """
        asyncio.create_task() ile çalıştırılır.
        Başlangıçta bir kez çalışır, ardından her gün 10:05 UTC'de tekrarlar.
        """
        logger.info("WeatherMarketUpdater scheduler başladı.")
        await self._run_safe()

        while True:
            delay = self._seconds_until_next_schedule()
            next_run = datetime.now(timezone.utc) + timedelta(seconds=delay)
            logger.info(
                f"WeatherMarketUpdater: sonraki çalışma "
                f"{next_run.strftime('%Y-%m-%d %H:%M:%S')} UTC "
                f"({delay:.0f}s sonra)"
            )
            await asyncio.sleep(delay)
            await self._run_safe()

    # ------------------------------------------------------------------
    # Core update logic
    # ------------------------------------------------------------------

    async def update_once(self) -> dict[str, str]:
        """
        Tüm şehirler için Polymarket API'yi sorgular ve markets_config.json'u
        günceller. Döner: {city_name: new_token_id} (güncellenen şehirler)
        """
        target_date = datetime.now(timezone.utc).date() + timedelta(days=self._lookahead_days)
        logger.info(
            f"WeatherMarketUpdater: hedef tarih {target_date} "
            f"({len(_CITY_MAP)} şehir sorgulanacak)"
        )

        config = self._load_config()
        updated: dict[str, str] = {}
        session = await self._get_session()

        for city_meta in _CITY_MAP:
            markets = await self._fetch_markets_with_fallback(
                session, city_meta["slug_name"], target_date
            )

            if not markets:
                logger.warning(
                    f"[{city_meta['city']}] Hiçbir slug varyantı için market bulunamadı."
                )
                continue

            best = _pick_best_market(markets)
            if best is None:
                logger.warning(
                    f"[{city_meta['city']}] Geçerli market bulunamadı "
                    f"(volume bilgisi yok)."
                )
                continue

            token_id = _yes_token_id(best)
            market_id = str(best.get("id") or "")

            if not token_id or not market_id:
                logger.warning(
                    f"[{city_meta['city']}] token_id veya market_id boş — atlanıyor. "
                    f"clobTokenIds={best.get('clobTokenIds')!r} "
                    f"outcomes={best.get('outcomes')!r}"
                )
                continue

            vol = _market_volume(best)
            question = str(best.get("question") or "")
            old_token = self._find_existing_token(config, city_meta["city"])

            if old_token == token_id:
                logger.debug(
                    f"[{city_meta['city']}] token_id zaten güncel — değişiklik yok."
                )
                continue

            self._upsert_entry(config, city_meta, token_id, market_id, question)
            updated[city_meta["city"]] = token_id
            logger.info(
                f"[{city_meta['city']}] Güncellendi ✓ "
                f"market_id={market_id[:18]}… "
                f"token={token_id[:16]}… "
                f"volume={vol:.0f} "
                f"soru='{question[:60]}'"
            )

        if updated:
            self._save_config(config)
            logger.info(
                f"WeatherMarketUpdater: {len(updated)} market güncellendi → "
                f"{list(updated.keys())}"
            )
        else:
            logger.info("WeatherMarketUpdater: Hiçbir market güncellenmedi (zaten güncel).")

        return updated

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            headers = {"User-Agent": "polymarket-weather-bot/2.0"}
            self._session = aiohttp.ClientSession(
                timeout=self.REQUEST_TIMEOUT, headers=headers
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _fetch_markets(
        self, session: aiohttp.ClientSession, slug: str
    ) -> list[dict]:
        """
        Gamma API'den slug'a karşılık gelen event'i çeker.
        Döner: markets listesi. Event bulunamazsa veya hata olursa [] döner.
        """
        params = {"slug": slug}
        try:
            async with session.get(self.GAMMA_BASE, params=params) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning(
                        f"Gamma API hatası slug='{slug}': "
                        f"HTTP {resp.status} {body[:120]}"
                    )
                    return []
                events: list = await resp.json(content_type=None)
        except Exception as exc:
            logger.warning(f"Gamma API isteği başarısız slug='{slug}': {exc}")
            return []

        if not events:
            return []

        event = events[0]
        return event.get("markets") or []

    async def _fetch_markets_with_fallback(
        self,
        session: aiohttp.ClientSession,
        slug_name: str,
        target_date,
    ) -> list[dict]:
        """
        Ana slug ile dener; boş dönerse alternatif slug varyantlarını dener.

        Varyant mantığı:
        - Gün rakamı tek hane: "april-7" → önce dene, sonra "april-07" dene
        - Gün rakamı çift hane: "april-14" → önce dene, sonra leading-zero olmadan aynı
        Her iki varyant da boş dönerse [] döner ve çağıran log yazar.
        """
        primary_slug = _build_slug(slug_name, target_date)
        markets = await self._fetch_markets(session, primary_slug)
        if markets:
            logger.debug(f"[{slug_name}] Slug bulundu: '{primary_slug}'")
            return markets

        # Alternatif: gün basamaklı / basamaksız
        day_no_zero = str(target_date.day)
        day_zero = f"{target_date.day:02d}"
        alt_day = day_zero if len(day_no_zero) == 1 else day_no_zero
        month_name = target_date.strftime("%B").lower()
        year = str(target_date.year)
        alt_slug = (
            f"highest-temperature-in-{slug_name}-on-"
            f"{month_name}-{alt_day}-{year}"
        )

        if alt_slug == primary_slug:
            return []

        logger.debug(
            f"[{slug_name}] '{primary_slug}' bulunamadı, "
            f"alternatif deneniyor: '{alt_slug}'"
        )
        markets = await self._fetch_markets(session, alt_slug)
        if markets:
            logger.debug(f"[{slug_name}] Alternatif slug bulundu: '{alt_slug}'")
        return markets

    # ------------------------------------------------------------------
    # Config helpers
    # ------------------------------------------------------------------

    def _load_config(self) -> list[dict]:
        if not self._config_path.exists():
            logger.error(f"markets_config.json bulunamadı: {self._config_path}")
            return []
        try:
            return json.loads(self._config_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.error(f"markets_config.json okunamadı: {exc}")
            return []

    def _save_config(self, data: list[dict]) -> None:
        try:
            self._config_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as exc:
            logger.error(f"markets_config.json yazılamadı: {exc}")

    @staticmethod
    def _find_existing_token(config: list[dict], city: str) -> str:
        """weather_enabled=true olan şehir girişinin mevcut token_id'sini döner."""
        for entry in config:
            if entry.get("weather_enabled") and entry.get("city") == city:
                return str(entry.get("token_id") or "")
        return ""

    @staticmethod
    def _upsert_entry(
        config: list[dict],
        city_meta: dict,
        token_id: str,
        market_id: str,
        question: str,
    ) -> None:
        """
        Şehire ait weather_enabled girişi varsa günceller, yoksa yeni ekler.
        weather_enabled olmayan kayıtlara (NHL, GTA VI vb.) hiç dokunmaz.
        """
        city = city_meta["city"]
        for entry in config:
            if entry.get("weather_enabled") and entry.get("city") == city:
                entry["token_id"] = token_id
                entry["market_id"] = market_id
                if question:
                    entry["market_question"] = question
                return

        new_entry: dict = {
            "token_id": token_id,
            "market_id": market_id,
            "market_question": question,
            "city": city,
            "country": city_meta["country"],
            "lat": city_meta["lat"],
            "lon": city_meta["lon"],
            "noaa_station": city_meta["noaa_station"],
            "settlement_station": city_meta["settlement_station"],
            "required_condition": "",
            "weather_enabled": True,
        }
        config.append(new_entry)
        logger.info(f"[{city}] Yeni weather market girişi eklendi.")

    # ------------------------------------------------------------------
    # Scheduler util
    # ------------------------------------------------------------------

    def _seconds_until_next_schedule(self) -> float:
        """
        Bir sonraki schedule_hour:schedule_minute UTC anına kadar kaç saniye
        kaldığını döner. Zaman zaten geçtiyse yarına hesaplar.
        """
        now = datetime.now(timezone.utc)
        target = now.replace(
            hour=self._schedule_hour,
            minute=self._schedule_minute,
            second=0,
            microsecond=0,
        )
        if target <= now:
            target += timedelta(days=1)
        return (target - now).total_seconds()

    async def _run_safe(self) -> None:
        """update_once()'ı exception yakalyarak çalıştırır."""
        try:
            updated = await self.update_once()
            if updated and self._on_updated:
                try:
                    await self._on_updated(updated)
                except Exception as cb_exc:
                    logger.warning(f"WeatherMarketUpdater callback hatası: {cb_exc}")
        except Exception as exc:
            logger.error(f"WeatherMarketUpdater güncelleme hatası: {exc}")


# ---------------------------------------------------------------------------
# Modül düzeyinde saf yardımcı fonksiyonlar
# ---------------------------------------------------------------------------

def _build_slug(slug_name: str, target_date) -> str:
    """
    "highest-temperature-in-{slug_name}-on-{month}-{day}-{year}" formatında slug üretir.
    Gün: leading-zero yok (5, 04 değil).
    """
    month_name = target_date.strftime("%B").lower()
    day = str(target_date.day)          # "5" — leading zero yok
    year = str(target_date.year)
    return f"highest-temperature-in-{slug_name}-on-{month_name}-{day}-{year}"


def _market_volume(market: dict) -> float:
    """
    Market hacmini döner.
    Önce volumeNum'u dener; yoksa veya geçersizse volume'u dener; ikisi de yoksa 0.0.
    """
    for field in ("volumeNum", "volume"):
        raw = market.get(field)
        if raw is None:
            continue
        try:
            return float(raw)
        except (TypeError, ValueError):
            continue
    return 0.0


def _pick_best_market(markets: list[dict]) -> Optional[dict]:
    """
    Verilen market (bucket) listesinden en yüksek volume'e sahip olanı döner.
    volumeNum yoksa volume alanına düşer; ikisi de yoksa 0.0 kabul edilir.
    Tüm marketler geçersizse None döner.
    """
    best: Optional[dict] = None
    best_vol = -1.0
    for m in markets:
        vol = _market_volume(m)
        if vol > best_vol:
            best_vol = vol
            best = m
    return best


def _parse_json_field(value) -> list:
    """
    Gamma API bazı alanları string-encoded JSON olarak döner: '["Yes","No"]'
    Bu fonksiyon string veya liste girişini güvenli şekilde listeye çevirir.
    """
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return parsed
        except (json.JSONDecodeError, ValueError):
            pass
    return []


def _yes_token_id(market: dict) -> str:
    """
    Market'in YES outcome token_id'sini döner.

    Gamma API'de iki temsil biçimi vardır:

    1. clobTokenIds + outcomes (string-encoded JSON — negRisk ve standart marketlerde):
       outcomes     = '["Yes", "No"]'        → json.loads → ["Yes", "No"]
       clobTokenIds = '["token0", "token1"]'  → json.loads → ["token0", "token1"]
       "Yes" index'ini bul → o index'teki token'ı al

    2. tokens listesi (eski format):
       [{"outcome": "Yes", "token_id": "..."}, ...]

    Önce 1. yöntemi dener; başarısız olursa 2. yönteme geçer.
    İkisi de çalışmazsa boş string döner.
    """
    # — Yöntem 1: clobTokenIds + outcomes —
    outcomes = _parse_json_field(market.get("outcomes"))
    clob_ids = _parse_json_field(market.get("clobTokenIds"))

    if outcomes and clob_ids and len(outcomes) == len(clob_ids):
        for idx, outcome in enumerate(outcomes):
            if str(outcome).strip().lower() == "yes":
                token = str(clob_ids[idx]).strip()
                if token:
                    return token

    # — Yöntem 2: tokens listesi (eski / fallback) —
    tokens: list = market.get("tokens") or []
    for tok in tokens:
        if str(tok.get("outcome") or "").strip().lower() == "yes":
            tid = str(tok.get("token_id") or "").strip()
            if tid:
                return tid

    # — Son çare: ilk clobTokenId veya ilk token —
    if clob_ids:
        first = str(clob_ids[0]).strip()
        if first:
            logger.debug(
                f"_yes_token_id: 'Yes' bulunamadı, ilk clobTokenId kullanılıyor: {first[:16]}…"
            )
            return first

    if tokens:
        tid = str(tokens[0].get("token_id") or "").strip()
        if tid:
            logger.debug(
                f"_yes_token_id: 'Yes' bulunamadı, ilk token kullanılıyor: {tid[:16]}…"
            )
            return tid

    return ""


# ---------------------------------------------------------------------------
# Standalone çalıştırma (opsiyonel test)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    from loguru import logger as _log

    _log.remove()
    _log.add(
        sys.stdout,
        level="DEBUG",
        format="<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | {message}",
    )

    async def _main():
        updater = WeatherMarketUpdater()
        await updater.update_once()
        await updater.close()

    asyncio.run(_main())

