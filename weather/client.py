import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Tuple

import aiohttp
from loguru import logger

from config import settings

# ---------------------------------------------------------------------------
# City coordinate registry — ABD şehirleri için NOAA desteği
# ---------------------------------------------------------------------------
_CITY_COORDS: dict[str, tuple[float, float]] = {
    "new york":      (40.7128, -74.0060),
    "new york city": (40.7128, -74.0060),
    "nyc":           (40.7128, -74.0060),
    "chicago":       (41.8781, -87.6298),
    "los angeles":   (34.0522, -118.2437),
    "la":            (34.0522, -118.2437),
    "miami":         (25.7617, -80.1918),
    "seattle":       (47.6062, -122.3321),
    "atlanta":       (33.7490, -84.3880),
}


@dataclass
class WeatherSnapshot:
    city: str
    condition: str
    temperature_c: float
    humidity: float
    wind_speed: float
    fetched_at: datetime
    # --- Çok-model tahmin alanları (koordinat bulunamazsa None) ---
    noaa_forecast_temp: Optional[float] = field(default=None)       # °F — NOAA NWS
    openmeteo_forecast_temp: Optional[float] = field(default=None)  # °C — GFS + ECMWF ort.
    model_agreement: Optional[bool] = field(default=None)           # ±3 °C aralığında mı
    edge_score: float = field(default=0.0)                          # 0.0–1.0


class WeatherClient:
    """
    Hava durumu istemcisi — üç kaynak:

    1. OpenWeatherMap → anlık gözlem (temperature_c, humidity, wind_speed, condition)
       Gereksinim: settings.weather_api_key
    2. NOAA NWS (api.weather.gov) → gündüz dönemi sıcaklık tahmini (°F)
       Ücretsiz, API anahtarı gerekmez. Yalnızca ABD şehirleri.
    3. Open-Meteo (open-meteo.com) → GFS + ECMWF saatlik tahmin ortalaması (°C)
       Ücretsiz, API anahtarı gerekmez, küresel kapsama.

    NOAA ve Open-Meteo tahminleri Kelvin bazında %15'ten fazla ayrışırsa
    doğrudan Telegram Bot API üzerinden uyarı gönderir.

    fetch_city_weather() imzası değişmez — main.py güncellenmez.
    """

    _NOAA_HEADERS: dict[str, str] = {
        "Accept": "application/geo+json",
        # NOAA, User-Agent başlığını zorunlu kılar
        "User-Agent": "polymarket-weather-bot/2.0 (polymarket_bot_v2)",
    }

    def __init__(self, api_key: str):
        self._api_key = api_key
        self._session: Optional[aiohttp.ClientSession] = None
        # NOAA grid metadata cache: "lat,lon" → forecast URL
        self._noaa_grid_cache: dict[str, str] = {}

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=10)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    # ------------------------------------------------------------------
    # Public interface — imza değişmedi
    # ------------------------------------------------------------------

    async def fetch_city_weather(
        self, city: str, country: Optional[str] = None
    ) -> Optional[WeatherSnapshot]:
        """
        Belirtilen şehir için anlık hava durumu + NOAA/Open-Meteo tahminlerini döner.
        Şehrin koordinatları biliniyorsa model karşılaştırması ve edge_score hesaplanır.
        """
        # 1. OpenWeatherMap anlık gözlem
        snapshot = await self._fetch_owm(city, country)
        if snapshot is None:
            snapshot = WeatherSnapshot(
                city=city,
                condition="UNKNOWN",
                temperature_c=0.0,
                humidity=0.0,
                wind_speed=0.0,
                fetched_at=datetime.utcnow(),
            )

        # 2. Koordinat varsa NOAA + Open-Meteo tahminleri
        coords = _CITY_COORDS.get(city.lower())
        if coords is not None:
            lat, lon = coords
            noaa_f, om_c = await self._fetch_forecasts_parallel(lat, lon)
            snapshot.noaa_forecast_temp = noaa_f
            snapshot.openmeteo_forecast_temp = om_c

            if noaa_f is not None and om_c is not None:
                noaa_c = _f_to_c(noaa_f)
                agreement, pct_diff = _model_agreement(noaa_c, om_c)
                snapshot.model_agreement = agreement
                snapshot.edge_score = _calc_edge_score(noaa_c, om_c, agreement)
                if pct_diff > 0.15:
                    await self._send_disagreement_alert(
                        city, noaa_f, noaa_c, om_c, pct_diff
                    )
            else:
                snapshot.model_agreement = None
                snapshot.edge_score = 0.3  # Eksik veri — düşük güven

        logger.debug(
            f"Weather [{city}] cond={snapshot.condition} temp={snapshot.temperature_c:.1f}°C | "
            f"NOAA={snapshot.noaa_forecast_temp}°F "
            f"OM={snapshot.openmeteo_forecast_temp}°C "
            f"agree={snapshot.model_agreement} edge={snapshot.edge_score:.3f}"
        )
        return snapshot

    # ------------------------------------------------------------------
    # 1. OpenWeatherMap — anlık gözlem
    # ------------------------------------------------------------------

    async def _fetch_owm(
        self, city: str, country: Optional[str]
    ) -> Optional[WeatherSnapshot]:
        if not self._api_key:
            logger.warning("WEATHER_API_KEY tanımlı değil; OWM sorgusu atlanıyor.")
            return None

        query = f"{city},{country}" if country else city
        params = {"q": query, "appid": self._api_key, "units": "metric"}
        url = "https://api.openweathermap.org/data/2.5/weather"

        try:
            session = await self._get_session()
            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.warning(f"OWM error [{query}]: {resp.status} {text[:120]}")
                    return None
                data = await resp.json()
        except Exception as exc:
            logger.error(f"OWM request failed [{query}]: {exc}")
            return None

        weather_list = data.get("weather") or []
        main = data.get("main") or {}
        wind = data.get("wind") or {}

        condition = "UNKNOWN"
        if weather_list:
            condition = str(weather_list[0].get("main") or "UNKNOWN").upper()

        def _safe_float(d: dict, k: str) -> float:
            try:
                return float(d.get(k, 0.0))
            except (TypeError, ValueError):
                return 0.0

        return WeatherSnapshot(
            city=city,
            condition=condition,
            temperature_c=_safe_float(main, "temp"),
            humidity=_safe_float(main, "humidity"),
            wind_speed=_safe_float(wind, "speed"),
            fetched_at=datetime.utcnow(),
        )

    # ------------------------------------------------------------------
    # 2. NOAA NWS — api.weather.gov (ABD şehirleri, ücretsiz)
    # ------------------------------------------------------------------

    async def _noaa_forecast_url(self, lat: float, lon: float) -> Optional[str]:
        """NOAA /points endpointinden tahmin URL'sini alır; bellekte cache'lenir."""
        cache_key = f"{lat:.4f},{lon:.4f}"
        if cache_key in self._noaa_grid_cache:
            return self._noaa_grid_cache[cache_key]

        url = f"https://api.weather.gov/points/{lat:.4f},{lon:.4f}"
        try:
            session = await self._get_session()
            async with session.get(url, headers=self._NOAA_HEADERS) as resp:
                if resp.status != 200:
                    logger.warning(f"NOAA /points error: {resp.status}")
                    return None
                data = await resp.json(content_type=None)
        except Exception as exc:
            logger.warning(f"NOAA /points request failed: {exc}")
            return None

        forecast_url: Optional[str] = (data.get("properties") or {}).get("forecast")
        if forecast_url:
            self._noaa_grid_cache[cache_key] = forecast_url
        return forecast_url

    async def _fetch_noaa_temp_f(self, lat: float, lon: float) -> Optional[float]:
        """
        NOAA NWS üzerinden en yakın gündüz tahmininin sıcaklığını (°F) döner.
        İlk 4 periyotta gündüz dönemi (isDaytime=True) yoksa ilk kayıt kullanılır.
        """
        forecast_url = await self._noaa_forecast_url(lat, lon)
        if forecast_url is None:
            return None

        try:
            session = await self._get_session()
            async with session.get(forecast_url, headers=self._NOAA_HEADERS) as resp:
                if resp.status != 200:
                    logger.warning(f"NOAA forecast error: {resp.status}")
                    return None
                data = await resp.json(content_type=None)
        except Exception as exc:
            logger.warning(f"NOAA forecast request failed: {exc}")
            return None

        periods: list = ((data.get("properties") or {}).get("periods") or [])
        if not periods:
            return None

        target = periods[0]
        for p in periods[:4]:
            if p.get("isDaytime"):
                target = p
                break

        try:
            return float(target["temperature"])
        except (KeyError, TypeError, ValueError):
            return None

    # ------------------------------------------------------------------
    # 3. Open-Meteo — open-meteo.com (GFS + ECMWF, ücretsiz, küresel)
    # ------------------------------------------------------------------

    async def _fetch_openmeteo_model(
        self, lat: float, lon: float, model: str
    ) -> Optional[float]:
        """Tek bir Open-Meteo modeli için sonraki saatin sıcaklığını (°C) döner."""
        url = "https://api.open-meteo.com/v1/forecast"
        params = {
            "latitude": lat,
            "longitude": lon,
            "hourly": "temperature_2m",
            "models": model,
            "forecast_days": 1,
            "timezone": "UTC",
        }
        try:
            session = await self._get_session()
            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.warning(
                        f"Open-Meteo [{model}] error: {resp.status} {text[:120]}"
                    )
                    return None
                data = await resp.json(content_type=None)
        except Exception as exc:
            logger.warning(f"Open-Meteo [{model}] request failed: {exc}")
            return None

        temps: list = ((data.get("hourly") or {}).get("temperature_2m") or [])
        return _first_valid(temps)

    async def _fetch_openmeteo_temp_c(self, lat: float, lon: float) -> Optional[float]:
        """
        GFS (gfs_seamless) ve ECMWF (ecmwf_ifs025) modellerini paralel çeker.
        Her ikisi mevcutsa ortalamasını, sadece biri mevcutsa onu döner.
        """
        gfs_result, ecmwf_result = await asyncio.gather(
            self._fetch_openmeteo_model(lat, lon, "gfs_seamless"),
            self._fetch_openmeteo_model(lat, lon, "ecmwf_ifs025"),
            return_exceptions=True,
        )

        gfs_t: Optional[float] = None
        ecmwf_t: Optional[float] = None

        if isinstance(gfs_result, Exception):
            logger.warning(f"Open-Meteo GFS exception: {gfs_result}")
        elif gfs_result is not None:
            gfs_t = float(gfs_result)

        if isinstance(ecmwf_result, Exception):
            logger.warning(f"Open-Meteo ECMWF exception: {ecmwf_result}")
        elif ecmwf_result is not None:
            ecmwf_t = float(ecmwf_result)

        if gfs_t is not None and ecmwf_t is not None:
            return round((gfs_t + ecmwf_t) / 2.0, 2)
        return gfs_t if gfs_t is not None else ecmwf_t

    # ------------------------------------------------------------------
    # Tahmin orkestratörü
    # ------------------------------------------------------------------

    async def _fetch_forecasts_parallel(
        self, lat: float, lon: float
    ) -> Tuple[Optional[float], Optional[float]]:
        """
        NOAA ve Open-Meteo tahminlerini eş zamanlı çeker.
        Döner: (noaa_temp_f, openmeteo_temp_c)
        """
        noaa_result, om_result = await asyncio.gather(
            self._fetch_noaa_temp_f(lat, lon),
            self._fetch_openmeteo_temp_c(lat, lon),
            return_exceptions=True,
        )

        noaa_f: Optional[float] = None
        om_c: Optional[float] = None

        if isinstance(noaa_result, Exception):
            logger.warning(f"NOAA gather exception: {noaa_result}")
        elif noaa_result is not None:
            noaa_f = float(noaa_result)

        if isinstance(om_result, Exception):
            logger.warning(f"Open-Meteo gather exception: {om_result}")
        elif om_result is not None:
            om_c = float(om_result)

        return noaa_f, om_c

    # ------------------------------------------------------------------
    # Telegram model ayrışma uyarısı
    # ------------------------------------------------------------------

    async def _send_disagreement_alert(
        self,
        city: str,
        noaa_f: float,
        noaa_c: float,
        om_c: float,
        pct_diff: float,
    ) -> None:
        """
        NOAA ve Open-Meteo tahminleri Kelvin bazında %15'ten fazla ayrışırsa
        Telegram Bot API üzerinden doğrudan mesaj gönderir.
        settings.telegram_bot_token veya telegram_chat_id boşsa sessizce atlanır.
        """
        token = settings.telegram_bot_token
        chat_id = settings.telegram_chat_id
        if not token or not chat_id:
            logger.warning(
                f"Model disagreement [{city}] {pct_diff:.1%} — Telegram yapılandırılmamış."
            )
            return

        text = (
            f"⚠️ *Weather Model Disagreement*\n"
            f"City: `{city}`\n"
            f"NOAA: `{noaa_f:.1f} °F` (`{noaa_c:.1f} °C`)\n"
            f"Open-Meteo: `{om_c:.1f} °C`\n"
            f"Divergence: `{pct_diff:.1%}` (threshold 15%)\n"
            f"_Edge score reduced due to model disagreement._"
        )
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}

        try:
            session = await self._get_session()
            async with session.post(url, json=payload) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning(
                        f"Telegram disagreement alert failed: {resp.status} {body[:120]}"
                    )
        except Exception as exc:
            logger.warning(f"Telegram disagreement alert exception: {exc}")


# ---------------------------------------------------------------------------
# Modül düzeyinde saf yardımcı fonksiyonlar
# ---------------------------------------------------------------------------

def _f_to_c(fahrenheit: float) -> float:
    """Fahrenheit → Celsius."""
    return (fahrenheit - 32.0) * 5.0 / 9.0


def _first_valid(values: list) -> Optional[float]:
    """Listede ilk geçerli (None olmayan) float değeri döner."""
    for v in values:
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return None


def _model_agreement(noaa_c: float, om_c: float) -> Tuple[bool, float]:
    """
    İki modelin göreli ayrışmasını Kelvin bazında hesaplar.
    Negatif Celsius değerlerinde yüzde hesabı sapıtmasın diye Kelvin kullanılır.

    Döner:
        agreement (bool)  — ±3 °C aralığında ise True
        pct_diff  (float) — Kelvin bazlı göreli fark (0.0–1.0)
    """
    noaa_k = noaa_c + 273.15
    om_k = om_c + 273.15
    avg_k = (noaa_k + om_k) / 2.0
    pct_diff = abs(noaa_k - om_k) / avg_k
    agreement = abs(noaa_c - om_c) <= 3.0
    return agreement, pct_diff


def _calc_edge_score(noaa_c: float, om_c: float, agreement: bool) -> float:
    """
    0.0–1.0 arası edge skoru — market fiyatıyla karşılaştırma için kullanılır.

    Taban skoru:
        model_agreement=True  → 0.65
        model_agreement=False → 0.25

    Aşırı sıcaklık bonusu (≥35 °C veya ≤0 °C):
        Her 10 °C'lik sapma için +0.05, maksimum +0.20
    """
    base = 0.65 if agreement else 0.25
    avg_c = (noaa_c + om_c) / 2.0
    extreme_delta = max(0.0, avg_c - 35.0) + max(0.0, -avg_c)
    bonus = min(0.20, (extreme_delta / 10.0) * 0.05)
    return round(min(1.0, base + bonus), 4)

