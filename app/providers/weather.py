from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import httpx

from app.config import get_settings
from app.models import Coordinate
from app.services.coordinates import wgs84_to_gcj02


@dataclass(frozen=True)
class WeatherSnapshot:
    max_temp_c: float | None = None
    min_temp_c: float | None = None
    precipitation_probability: float | None = None
    max_wind_kmh: float | None = None
    source: str = "unknown"


@dataclass(frozen=True)
class DailyForecast:
    """Single day weather forecast."""
    date: str  # YYYY-MM-DD
    day_weather: str  # e.g. 晴, 多云, 小雨
    night_weather: str
    day_temp: str  # e.g. "30"
    night_temp: str  # e.g. "18"
    day_wind: str  # e.g. 东
    night_wind: str
    day_power: str  # e.g. "≤3"
    night_power: str
    source: str = "amap-forecast"

    @property
    def is_suitable_for_hiking(self) -> bool:
        """Determine if weather is suitable for hiking."""
        bad_keywords = ["暴", "大雨", "中雨", "小雨", "雷", "雪", "冰雹", "沙尘", "台风"]
        weather_text = self.day_weather + self.night_weather
        return not any(kw in weather_text for kw in bad_keywords)

    @property
    def suitability_label(self) -> str:
        if not self.is_suitable_for_hiking:
            return "不宜"
        good_keywords = ["晴", "多云", "阴"]
        if any(kw in self.day_weather for kw in good_keywords):
            return "适宜"
        return "一般"


class WeatherProvider(Protocol):
    def forecast(self, coordinate: Coordinate) -> WeatherSnapshot | None:
        raise NotImplementedError


class NoopWeatherProvider:
    def forecast(self, coordinate: Coordinate) -> WeatherSnapshot | None:
        return None


class AmapWeatherProvider:
    def __init__(self, api_key: str | None = None, timeout_s: float = 8.0) -> None:
        self.api_key = api_key if api_key is not None else get_settings().api_keys.amap_api_key
        self.timeout_s = timeout_s

    def forecast(self, coordinate: Coordinate) -> WeatherSnapshot | None:
        if not self.api_key:
            raise RuntimeError("AMAP_API_KEY is not configured")

        adcode = self._adcode_for_coordinate(coordinate)
        response = httpx.get(
            "https://restapi.amap.com/v3/weather/weatherInfo",
            params={
                "key": self.api_key,
                "city": adcode,
                "extensions": "base",
                "output": "JSON",
            },
            timeout=self.timeout_s,
        )
        response.raise_for_status()
        payload = response.json()
        self._raise_for_amap_error(payload, "weatherInfo")
        lives = payload.get("lives") or []
        if not lives:
            return None

        live = lives[0]
        temperature = _float_or_none(live.get("temperature"))
        wind_power = _wind_power_to_kmh(live.get("windpower"))
        return WeatherSnapshot(
            max_temp_c=temperature,
            min_temp_c=temperature,
            precipitation_probability=None,
            max_wind_kmh=wind_power,
            source="amap-weather",
        )

    def daily_forecast(self, coordinate: Coordinate) -> list[DailyForecast]:
        """Get multi-day weather forecast for a coordinate.

        Returns up to 4 days of forecast from Amap's "all" extensions.
        """
        if not self.api_key:
            raise RuntimeError("AMAP_API_KEY is not configured")

        adcode = self._adcode_for_coordinate(coordinate)
        response = httpx.get(
            "https://restapi.amap.com/v3/weather/weatherInfo",
            params={
                "key": self.api_key,
                "city": adcode,
                "extensions": "all",
                "output": "JSON",
            },
            timeout=self.timeout_s,
        )
        response.raise_for_status()
        payload = response.json()
        self._raise_for_amap_error(payload, "weatherInfo-forecast")

        forecasts_data = (payload.get("forecasts") or [{}])[0].get("casts") or []
        results: list[DailyForecast] = []
        for cast in forecasts_data:
            results.append(DailyForecast(
                date=cast.get("date", ""),
                day_weather=cast.get("dayweather", ""),
                night_weather=cast.get("nightweather", ""),
                day_temp=cast.get("daytemp", ""),
                night_temp=cast.get("nighttemp", ""),
                day_wind=cast.get("daywind", ""),
                night_wind=cast.get("nightwind", ""),
                day_power=cast.get("daypower", ""),
                night_power=cast.get("nightpower", ""),
                source="amap-forecast",
            ))
        return results

    def _adcode_for_coordinate(self, coordinate: Coordinate) -> str:
        amap_point = wgs84_to_gcj02(coordinate)
        response = httpx.get(
            "https://restapi.amap.com/v3/geocode/regeo",
            params={
                "key": self.api_key,
                "location": f"{amap_point.lon},{amap_point.lat}",
                "extensions": "base",
                "output": "JSON",
            },
            timeout=self.timeout_s,
        )
        response.raise_for_status()
        payload = response.json()
        self._raise_for_amap_error(payload, "regeo")
        address_component = (payload.get("regeocode") or {}).get("addressComponent") or {}
        adcode = str(address_component.get("adcode") or "").strip()
        if not adcode:
            raise RuntimeError("Amap reverse geocode did not include adcode")
        return adcode

    def _raise_for_amap_error(self, payload: dict[str, object], operation: str) -> None:
        if str(payload.get("status")) == "1":
            return
        info = payload.get("info") or "Amap request failed"
        infocode = payload.get("infocode") or "unknown"
        raise RuntimeError(f"Amap {operation} failed ({infocode}): {info}")


class OpenMeteoWeatherProvider:
    def __init__(self, timeout_s: float = 8.0) -> None:
        self.timeout_s = timeout_s

    def forecast(self, coordinate: Coordinate) -> WeatherSnapshot | None:
        response = httpx.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": coordinate.lat,
                "longitude": coordinate.lon,
                "daily": ",".join(
                    [
                        "temperature_2m_max",
                        "temperature_2m_min",
                        "precipitation_probability_max",
                        "wind_speed_10m_max",
                    ]
                ),
                "forecast_days": 1,
                "timezone": "auto",
            },
            timeout=self.timeout_s,
        )
        response.raise_for_status()
        daily = response.json().get("daily") or {}
        return WeatherSnapshot(
            max_temp_c=_first(daily.get("temperature_2m_max")),
            min_temp_c=_first(daily.get("temperature_2m_min")),
            precipitation_probability=_first(daily.get("precipitation_probability_max")),
            max_wind_kmh=_first(daily.get("wind_speed_10m_max")),
            source="open-meteo",
        )


def _first(values: list[float] | None) -> float | None:
    if not values:
        return None
    return values[0]


def _float_or_none(value: object) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _wind_power_to_kmh(value: object) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    text = text.removesuffix("级").strip()
    if text in {"无风", "微风"}:
        return 0.0
    for marker in ("≤", "<"):
        if text.startswith(marker):
            return _beaufort_to_max_kmh(_float_or_none(text.removeprefix(marker)))
    if "-" in text:
        high = text.split("-")[-1]
        return _beaufort_to_max_kmh(_float_or_none(high))
    return _beaufort_to_max_kmh(_float_or_none(text))


def _beaufort_to_max_kmh(level: float | None) -> float | None:
    if level is None:
        return None
    table = {
        0: 1,
        1: 5,
        2: 11,
        3: 19,
        4: 28,
        5: 38,
        6: 49,
        7: 61,
        8: 74,
        9: 88,
        10: 102,
        11: 117,
        12: 133,
    }
    return float(table.get(round(level), 133))
