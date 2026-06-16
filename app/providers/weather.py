from __future__ import annotations

from dataclasses import dataclass
from datetime import date as date_type, timedelta
from typing import Protocol

import httpx

from app.config import get_settings
from app.models import Coordinate, WeatherDetail
from app.services.coordinates import wgs84_to_gcj02


@dataclass(frozen=True)
class WeatherSnapshot:
    max_temp_c: float | None = None
    min_temp_c: float | None = None
    precipitation_probability: float | None = None
    max_wind_kmh: float | None = None
    weather_text: str | None = None
    humidity_percent: float | None = None
    precipitation_mm: float | None = None
    wind_gust_kmh: float | None = None
    wind_direction: str | None = None
    uv_index_max: float | None = None
    hiking_risk_notes: tuple[str, ...] = ()
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
    humidity_percent: float | None = None
    precipitation_probability: float | None = None
    precipitation_mm: float | None = None
    wind_gust_kmh: float | None = None
    uv_index_max: float | None = None
    hiking_risk_notes: tuple[str, ...] = ()
    source: str = "amap-forecast"

    @property
    def is_suitable_for_hiking(self) -> bool:
        """Determine if weather is suitable for hiking."""
        bad_keywords = ["暴", "雨", "雷", "雪", "冰雹", "沙尘", "台风"]
        weather_text = self.day_weather + self.night_weather
        if any(kw in weather_text for kw in bad_keywords):
            return False
        max_temp = _float_or_none(self.day_temp)
        min_temp = _float_or_none(self.night_temp)
        max_wind = _wind_power_to_kmh(self.day_power)
        return not (
            (self.precipitation_probability is not None and self.precipitation_probability >= 60)
            or (self.precipitation_mm is not None and self.precipitation_mm >= 10)
            or (max_wind is not None and max_wind >= 40)
            or (self.wind_gust_kmh is not None and self.wind_gust_kmh >= 55)
            or (max_temp is not None and max_temp >= 35)
            or (min_temp is not None and min_temp <= -5)
        )

    @property
    def suitability_label(self) -> str:
        if not self.is_suitable_for_hiking:
            return "不宜"
        if self.hiking_risk_notes:
            return "一般"
        good_keywords = ["晴", "多云", "阴"]
        if any(kw in self.day_weather for kw in good_keywords):
            return "适宜"
        return "适宜"


class WeatherProvider(Protocol):
    def forecast(self, coordinate: Coordinate) -> WeatherSnapshot | None:
        raise NotImplementedError

    def details(self, coordinate: Coordinate) -> list[WeatherDetail]:
        raise NotImplementedError


class NoopWeatherProvider:
    def forecast(self, coordinate: Coordinate) -> WeatherSnapshot | None:
        return None

    def details(self, coordinate: Coordinate) -> list[WeatherDetail]:
        return []


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
        humidity = _float_or_none(live.get("humidity"))
        return WeatherSnapshot(
            max_temp_c=temperature,
            min_temp_c=temperature,
            precipitation_probability=None,
            max_wind_kmh=wind_power,
            weather_text=str(live.get("weather") or "").strip() or None,
            humidity_percent=humidity,
            wind_direction=str(live.get("winddirection") or "").strip() or None,
            hiking_risk_notes=tuple(_risk_notes(
                weather_text=str(live.get("weather") or ""),
                max_temp_c=temperature,
                min_temp_c=temperature,
                humidity_percent=humidity,
                max_wind_kmh=wind_power,
            )),
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
            day_temp = _float_or_none(cast.get("daytemp"))
            night_temp = _float_or_none(cast.get("nighttemp"))
            max_wind = _wind_power_to_kmh(cast.get("daypower"))
            weather_text = " / ".join(
                part for part in (str(cast.get("dayweather") or ""), str(cast.get("nightweather") or "")) if part
            )
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
                hiking_risk_notes=tuple(_risk_notes(
                    weather_text=weather_text,
                    max_temp_c=day_temp,
                    min_temp_c=night_temp,
                    max_wind_kmh=max_wind,
                )),
                source="amap-forecast",
            ))
        return results

    def details(self, coordinate: Coordinate) -> list[WeatherDetail]:
        return [_detail_from_daily(item) for item in self.daily_forecast(coordinate)]

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
                        "precipitation_sum",
                        "wind_speed_10m_max",
                        "wind_gusts_10m_max",
                        "wind_direction_10m_dominant",
                        "uv_index_max",
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
            precipitation_mm=_first(daily.get("precipitation_sum")),
            max_wind_kmh=_first(daily.get("wind_speed_10m_max")),
            wind_gust_kmh=_first(daily.get("wind_gusts_10m_max")),
            wind_direction=_wind_direction_label(_first(daily.get("wind_direction_10m_dominant"))),
            uv_index_max=_first(daily.get("uv_index_max")),
            hiking_risk_notes=tuple(_risk_notes(
                max_temp_c=_first(daily.get("temperature_2m_max")),
                min_temp_c=_first(daily.get("temperature_2m_min")),
                precipitation_probability=_first(daily.get("precipitation_probability_max")),
                precipitation_mm=_first(daily.get("precipitation_sum")),
                max_wind_kmh=_first(daily.get("wind_speed_10m_max")),
                wind_gust_kmh=_first(daily.get("wind_gusts_10m_max")),
                uv_index_max=_first(daily.get("uv_index_max")),
            )),
            source="open-meteo",
        )

    def details(self, coordinate: Coordinate) -> list[WeatherDetail]:
        daily = self._forecast_daily_payload(coordinate, forecast_days=4)
        results: list[WeatherDetail] = []
        for index, date in enumerate(daily.get("time") or []):
            max_temp = _at(daily.get("temperature_2m_max"), index)
            min_temp = _at(daily.get("temperature_2m_min"), index)
            rain_prob = _at(daily.get("precipitation_probability_max"), index)
            rain_mm = _at(daily.get("precipitation_sum"), index)
            wind = _at(daily.get("wind_speed_10m_max"), index)
            gust = _at(daily.get("wind_gusts_10m_max"), index)
            uv = _at(daily.get("uv_index_max"), index)
            direction = _wind_direction_label(_at(daily.get("wind_direction_10m_dominant"), index))
            risks = _risk_notes(
                max_temp_c=max_temp,
                min_temp_c=min_temp,
                precipitation_probability=rain_prob,
                precipitation_mm=rain_mm,
                max_wind_kmh=wind,
                wind_gust_kmh=gust,
                uv_index_max=uv,
            )
            results.append(
                WeatherDetail(
                    title="路线区域天气",
                    detail=_weather_detail_text(
                        max_temp_c=max_temp,
                        min_temp_c=min_temp,
                        precipitation_probability=rain_prob,
                        precipitation_mm=rain_mm,
                        max_wind_kmh=wind,
                        wind_gust_kmh=gust,
                        uv_index_max=uv,
                    ),
                    date=str(date),
                    max_temp_c=max_temp,
                    min_temp_c=min_temp,
                    precipitation_probability=rain_prob,
                    precipitation_mm=rain_mm,
                    max_wind_kmh=wind,
                    wind_gust_kmh=gust,
                    wind_direction=direction,
                    uv_index_max=uv,
                    hiking_risk_notes=risks,
                    source="open-meteo",
                )
            )
        return results

    def daily_forecast(
        self,
        coordinate: Coordinate,
        start_date: date_type | None = None,
        end_date: date_type | None = None,
    ) -> list[DailyForecast]:
        forecast_horizon_end = date_type.today() + timedelta(days=15)
        if start_date and end_date and (
            (end_date - start_date).days + 1 > 16 or end_date > forecast_horizon_end
        ):
            daily = self._seasonal_daily_payload(coordinate, start_date, end_date)
            return _daily_forecasts_from_open_meteo(daily, "open-meteo-seasonal")

        daily = self._forecast_daily_payload(
            coordinate,
            start_date=start_date,
            end_date=end_date,
            forecast_days=None if start_date and end_date else 4,
        )
        return _daily_forecasts_from_open_meteo(daily, "open-meteo")

    def _forecast_daily_payload(
        self,
        coordinate: Coordinate,
        start_date: date_type | None = None,
        end_date: date_type | None = None,
        forecast_days: int | None = None,
    ) -> dict[str, object]:
        params: dict[str, object] = {
            "latitude": coordinate.lat,
            "longitude": coordinate.lon,
            "daily": ",".join(
                [
                    "weather_code",
                    "temperature_2m_max",
                    "temperature_2m_min",
                    "precipitation_probability_max",
                    "precipitation_sum",
                    "wind_speed_10m_max",
                    "wind_gusts_10m_max",
                    "wind_direction_10m_dominant",
                    "uv_index_max",
                ]
            ),
            "timezone": "auto",
        }
        if start_date and end_date:
            params["start_date"] = start_date.isoformat()
            params["end_date"] = end_date.isoformat()
        elif forecast_days is not None:
            params["forecast_days"] = forecast_days

        response = httpx.get(
            "https://api.open-meteo.com/v1/forecast",
            params=params,
            timeout=self.timeout_s,
        )
        response.raise_for_status()
        return response.json().get("daily") or {}

    def _seasonal_daily_payload(
        self,
        coordinate: Coordinate,
        start_date: date_type,
        end_date: date_type,
    ) -> dict[str, object]:
        response = httpx.get(
            "https://seasonal-api.open-meteo.com/v1/seasonal",
            params={
                "latitude": coordinate.lat,
                "longitude": coordinate.lon,
                "daily": ",".join(
                    [
                        "weather_code",
                        "temperature_2m_max",
                        "temperature_2m_min",
                        "precipitation_sum",
                        "wind_speed_10m_max",
                        "wind_gusts_10m_max",
                        "wind_direction_10m_dominant",
                    ]
                ),
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "timezone": "auto",
            },
            timeout=self.timeout_s,
        )
        response.raise_for_status()
        return response.json().get("daily") or {}


class CompositeWeatherProvider:
    def __init__(
        self,
        providers: list[WeatherProvider],
    ) -> None:
        self.providers = providers

    def forecast(self, coordinate: Coordinate) -> WeatherSnapshot | None:
        snapshots: list[WeatherSnapshot] = []
        for provider in self.providers:
            try:
                snapshot = provider.forecast(coordinate)
            except Exception:
                continue
            if snapshot:
                snapshots.append(snapshot)
        if not snapshots:
            return None
        return _merge_snapshots(snapshots)

    def details(self, coordinate: Coordinate) -> list[WeatherDetail]:
        merged: dict[str, WeatherDetail] = {}
        for provider in self.providers:
            try:
                details = provider.details(coordinate)
            except Exception:
                continue
            for detail in details:
                key = detail.date or detail.source
                merged[key] = _merge_detail(merged.get(key), detail)
        return list(merged.values())


def _first(values: list[float] | None) -> float | None:
    if not values:
        return None
    return values[0]


def _at(values: list[object] | None, index: int) -> float | None:
    if not values or index >= len(values):
        return None
    return _float_or_none(values[index])


def _float_or_none(value: object) -> float | None:
    if isinstance(value, list):
        numbers = [_float_or_none(item) for item in value]
        clean_numbers = [item for item in numbers if item is not None]
        if not clean_numbers:
            return None
        return sum(clean_numbers) / len(clean_numbers)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _daily_forecasts_from_open_meteo(daily: dict[str, object], source: str) -> list[DailyForecast]:
    results: list[DailyForecast] = []
    for index, date in enumerate(daily.get("time") or []):
        weather_code = _at(daily.get("weather_code"), index)
        weather_text = _weather_code_label(weather_code)
        max_temp = _at(daily.get("temperature_2m_max"), index)
        min_temp = _at(daily.get("temperature_2m_min"), index)
        rain_prob = _at(daily.get("precipitation_probability_max"), index)
        rain_mm = _at(daily.get("precipitation_sum"), index)
        wind = _at(daily.get("wind_speed_10m_max"), index)
        gust = _at(daily.get("wind_gusts_10m_max"), index)
        uv = _at(daily.get("uv_index_max"), index)
        direction = _wind_direction_label(_at(daily.get("wind_direction_10m_dominant"), index)) or ""
        results.append(
            DailyForecast(
                date=str(date),
                day_weather=weather_text,
                night_weather=weather_text,
                day_temp=_format_number(max_temp),
                night_temp=_format_number(min_temp),
                day_wind=direction,
                night_wind=direction,
                day_power=_kmh_to_beaufort_label(wind),
                night_power=_kmh_to_beaufort_label(wind),
                precipitation_probability=rain_prob,
                precipitation_mm=rain_mm,
                wind_gust_kmh=gust,
                uv_index_max=uv,
                hiking_risk_notes=tuple(_risk_notes(
                    weather_text=weather_text,
                    max_temp_c=max_temp,
                    min_temp_c=min_temp,
                    precipitation_probability=rain_prob,
                    precipitation_mm=rain_mm,
                    max_wind_kmh=wind,
                    wind_gust_kmh=gust,
                    uv_index_max=uv,
                )),
                source=source,
            )
        )
    return results


def _format_number(value: float | None) -> str:
    if value is None:
        return ""
    if value == round(value):
        return str(round(value))
    return f"{value:.1f}"


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


def _kmh_to_beaufort_label(value: float | None) -> str:
    if value is None:
        return ""
    thresholds = [
        (1, "0"),
        (5, "1"),
        (11, "2"),
        (19, "3"),
        (28, "4"),
        (38, "5"),
        (49, "6"),
        (61, "7"),
        (74, "8"),
        (88, "9"),
        (102, "10"),
        (117, "11"),
    ]
    for max_kmh, label in thresholds:
        if value <= max_kmh:
            return label
    return "12"


def _merge_snapshots(snapshots: list[WeatherSnapshot]) -> WeatherSnapshot:
    primary = snapshots[0]
    for snapshot in snapshots[1:]:
        primary = WeatherSnapshot(
            max_temp_c=primary.max_temp_c if primary.max_temp_c is not None else snapshot.max_temp_c,
            min_temp_c=primary.min_temp_c if primary.min_temp_c is not None else snapshot.min_temp_c,
            precipitation_probability=(
                primary.precipitation_probability
                if primary.precipitation_probability is not None
                else snapshot.precipitation_probability
            ),
            max_wind_kmh=primary.max_wind_kmh if primary.max_wind_kmh is not None else snapshot.max_wind_kmh,
            weather_text=primary.weather_text or snapshot.weather_text,
            humidity_percent=primary.humidity_percent if primary.humidity_percent is not None else snapshot.humidity_percent,
            precipitation_mm=primary.precipitation_mm if primary.precipitation_mm is not None else snapshot.precipitation_mm,
            wind_gust_kmh=primary.wind_gust_kmh if primary.wind_gust_kmh is not None else snapshot.wind_gust_kmh,
            wind_direction=primary.wind_direction or snapshot.wind_direction,
            uv_index_max=primary.uv_index_max if primary.uv_index_max is not None else snapshot.uv_index_max,
            hiking_risk_notes=tuple(dict.fromkeys([*primary.hiking_risk_notes, *snapshot.hiking_risk_notes])),
            source=f"{primary.source}+{snapshot.source}",
        )
    return primary


def _merge_detail(current: WeatherDetail | None, incoming: WeatherDetail) -> WeatherDetail:
    if current is None:
        return incoming
    risks = list(dict.fromkeys([*current.hiking_risk_notes, *incoming.hiking_risk_notes]))
    return WeatherDetail(
        title=current.title,
        detail=_weather_detail_text(
            max_temp_c=current.max_temp_c if current.max_temp_c is not None else incoming.max_temp_c,
            min_temp_c=current.min_temp_c if current.min_temp_c is not None else incoming.min_temp_c,
            humidity_percent=current.humidity_percent if current.humidity_percent is not None else incoming.humidity_percent,
            precipitation_probability=(
                current.precipitation_probability
                if current.precipitation_probability is not None
                else incoming.precipitation_probability
            ),
            precipitation_mm=current.precipitation_mm if current.precipitation_mm is not None else incoming.precipitation_mm,
            max_wind_kmh=current.max_wind_kmh if current.max_wind_kmh is not None else incoming.max_wind_kmh,
            wind_gust_kmh=current.wind_gust_kmh if current.wind_gust_kmh is not None else incoming.wind_gust_kmh,
            uv_index_max=current.uv_index_max if current.uv_index_max is not None else incoming.uv_index_max,
        ),
        date=current.date or incoming.date,
        weather_text=current.weather_text or incoming.weather_text,
        max_temp_c=current.max_temp_c if current.max_temp_c is not None else incoming.max_temp_c,
        min_temp_c=current.min_temp_c if current.min_temp_c is not None else incoming.min_temp_c,
        humidity_percent=current.humidity_percent if current.humidity_percent is not None else incoming.humidity_percent,
        precipitation_probability=(
            current.precipitation_probability
            if current.precipitation_probability is not None
            else incoming.precipitation_probability
        ),
        precipitation_mm=current.precipitation_mm if current.precipitation_mm is not None else incoming.precipitation_mm,
        max_wind_kmh=current.max_wind_kmh if current.max_wind_kmh is not None else incoming.max_wind_kmh,
        wind_gust_kmh=current.wind_gust_kmh if current.wind_gust_kmh is not None else incoming.wind_gust_kmh,
        wind_direction=current.wind_direction or incoming.wind_direction,
        uv_index_max=current.uv_index_max if current.uv_index_max is not None else incoming.uv_index_max,
        hiking_risk_notes=risks,
        source=f"{current.source}+{incoming.source}",
    )


def _detail_from_daily(item: DailyForecast) -> WeatherDetail:
    max_temp = _float_or_none(item.day_temp)
    min_temp = _float_or_none(item.night_temp)
    max_wind = _wind_power_to_kmh(item.day_power)
    weather_text = " / ".join(part for part in (item.day_weather, item.night_weather) if part)
    risks = list(item.hiking_risk_notes) or _risk_notes(
        weather_text=weather_text,
        max_temp_c=max_temp,
        min_temp_c=min_temp,
        max_wind_kmh=max_wind,
    )
    return WeatherDetail(
        title="路线区域天气",
        detail=_weather_detail_text(
            weather_text=weather_text,
            max_temp_c=max_temp,
            min_temp_c=min_temp,
            humidity_percent=item.humidity_percent,
            precipitation_probability=item.precipitation_probability,
            precipitation_mm=item.precipitation_mm,
            max_wind_kmh=max_wind,
            wind_gust_kmh=item.wind_gust_kmh,
            uv_index_max=item.uv_index_max,
        ),
        date=item.date,
        weather_text=weather_text,
        max_temp_c=max_temp,
        min_temp_c=min_temp,
        humidity_percent=item.humidity_percent,
        precipitation_probability=item.precipitation_probability,
        precipitation_mm=item.precipitation_mm,
        max_wind_kmh=max_wind,
        wind_gust_kmh=item.wind_gust_kmh,
        wind_direction=item.day_wind,
        uv_index_max=item.uv_index_max,
        hiking_risk_notes=risks,
        source=item.source,
    )


def _weather_detail_text(
    weather_text: str | None = None,
    max_temp_c: float | None = None,
    min_temp_c: float | None = None,
    humidity_percent: float | None = None,
    precipitation_probability: float | None = None,
    precipitation_mm: float | None = None,
    max_wind_kmh: float | None = None,
    wind_gust_kmh: float | None = None,
    uv_index_max: float | None = None,
) -> str:
    parts: list[str] = []
    if weather_text:
        parts.append(weather_text)
    if min_temp_c is not None or max_temp_c is not None:
        parts.append(f"气温 {min_temp_c if min_temp_c is not None else '未知'}-{max_temp_c if max_temp_c is not None else '未知'} C")
    if humidity_percent is not None:
        parts.append(f"湿度 {humidity_percent:.0f}%")
    if precipitation_probability is not None:
        parts.append(f"降水概率 {precipitation_probability:.0f}%")
    if precipitation_mm is not None:
        parts.append(f"降水量 {precipitation_mm:.1f} mm")
    if max_wind_kmh is not None:
        parts.append(f"最大风速 {max_wind_kmh:.0f} km/h")
    if wind_gust_kmh is not None:
        parts.append(f"阵风 {wind_gust_kmh:.0f} km/h")
    if uv_index_max is not None:
        parts.append(f"UV {uv_index_max:.1f}")
    return "，".join(parts) if parts else "已获取天气数据，但缺少关键字段。"


def _risk_notes(
    weather_text: str | None = None,
    max_temp_c: float | None = None,
    min_temp_c: float | None = None,
    humidity_percent: float | None = None,
    precipitation_probability: float | None = None,
    precipitation_mm: float | None = None,
    max_wind_kmh: float | None = None,
    wind_gust_kmh: float | None = None,
    uv_index_max: float | None = None,
) -> list[str]:
    notes: list[str] = []
    text = weather_text or ""
    if any(keyword in text for keyword in ("雷", "暴", "大雨", "中雨", "雪", "冰雹")):
        notes.append("存在明显恶劣天气信号，建议推迟或准备下撤方案")
    if precipitation_probability is not None and precipitation_probability >= 60:
        notes.append("降水概率较高，注意湿滑、涉水和能见度")
    if precipitation_mm is not None and precipitation_mm >= 10:
        notes.append("累计降水偏多，山路泥泞和溪沟涨水风险增加")
    if max_wind_kmh is not None and max_wind_kmh >= 40:
        notes.append("风速较大，山脊和开阔地段需谨慎")
    if wind_gust_kmh is not None and wind_gust_kmh >= 55:
        notes.append("阵风较强，避免长时间暴露在山脊")
    if max_temp_c is not None and max_temp_c >= 32:
        notes.append("高温风险，避开正午并增加补水")
    if min_temp_c is not None and min_temp_c <= 0:
        notes.append("低温风险，携带保暖层并防止失温")
    if humidity_percent is not None and humidity_percent >= 85 and max_temp_c is not None and max_temp_c >= 28:
        notes.append("闷热潮湿，注意中暑和电解质补充")
    if uv_index_max is not None and uv_index_max >= 8:
        notes.append("紫外线强，注意防晒和眼部保护")
    return notes


def _wind_direction_label(degrees: float | None) -> str | None:
    if degrees is None:
        return None
    labels = ["北", "东北", "东", "东南", "南", "西南", "西", "西北"]
    return labels[round(degrees / 45) % 8]


def _weather_code_label(code: float | None) -> str:
    if code is None:
        return "天气趋势"
    value = int(code)
    labels = {
        0: "晴",
        1: "晴间多云",
        2: "多云",
        3: "阴",
        45: "雾",
        48: "雾凇",
        51: "小毛毛雨",
        53: "毛毛雨",
        55: "大毛毛雨",
        56: "冻毛毛雨",
        57: "强冻毛毛雨",
        61: "小雨",
        63: "中雨",
        65: "大雨",
        66: "冻雨",
        67: "强冻雨",
        71: "小雪",
        73: "中雪",
        75: "大雪",
        77: "雪粒",
        80: "阵雨",
        81: "强阵雨",
        82: "暴雨",
        85: "阵雪",
        86: "强阵雪",
        95: "雷雨",
        96: "雷雨伴冰雹",
        99: "强雷雨伴冰雹",
    }
    return labels.get(value, "天气趋势")
