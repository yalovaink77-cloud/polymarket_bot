from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import aiohttp
from loguru import logger


@dataclass
class WeatherSnapshot:
    city: str
    condition: str
    temperature_c: float
    humidity: float
    wind_speed: float
    fetched_at: datetime


class WeatherClient:
    """
    Basit bir OpenWeatherMap istemcisi.
    markets_config.json içindeki şehirler için anlık hava durumu verisi çeker.
    """

    def __init__(self, api_key: str):
        self._api_key = api_key
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=5)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def fetch_city_weather(self, city: str, country: Optional[str] = None) -> Optional[WeatherSnapshot]:
        """
        Belirtilen şehir için anlık hava durumu verisi döner.
        OpenWeatherMap "current weather" endpointini kullanır.
        """
        if not self._api_key:
            logger.warning("WEATHER_API_KEY tanımlı değil; hava durumu sorgulanmayacak.")
            return None

        query = city
        if country:
            query = f"{city},{country}"

        params = {
            "q": query,
            "appid": self._api_key,
            "units": "metric",
        }

        url = "https://api.openweathermap.org/data/2.5/weather"

        try:
            session = await self._get_session()
            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.warning(f"Weather API error for {query}: {resp.status} {text[:120]}")
                    return None
                data = await resp.json()
        except Exception as exc:
            logger.error(f"Weather API request failed for {query}: {exc}")
            return None

        weather_list = data.get("weather") or []
        main = data.get("main", {}) or {}
        wind = data.get("wind", {}) or {}

        condition = "UNKNOWN"
        if weather_list:
            condition = str(weather_list[0].get("main") or "UNKNOWN").upper()

        try:
            temp = float(main.get("temp", 0.0))
        except (TypeError, ValueError):
            temp = 0.0

        try:
            humidity = float(main.get("humidity", 0.0))
        except (TypeError, ValueError):
            humidity = 0.0

        try:
            wind_speed = float(wind.get("speed", 0.0))
        except (TypeError, ValueError):
            wind_speed = 0.0

        snapshot = WeatherSnapshot(
            city=city,
            condition=condition,
            temperature_c=temp,
            humidity=humidity,
            wind_speed=wind_speed,
            fetched_at=datetime.utcnow(),
        )
        logger.debug(
            f"Weather for {city}: cond={snapshot.condition} temp={snapshot.temperature_c:.1f}C "
            f"hum={snapshot.humidity:.0f}% wind={snapshot.wind_speed:.1f}m/s"
        )
        return snapshot

