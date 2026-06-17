from datetime import date, timedelta

import pytest

from app import main as main_module
from app.models import Coordinate
from app.providers.weather import DailyForecast


def _daily_forecast(forecast_date: date, source: str) -> DailyForecast:
    return DailyForecast(
        date=forecast_date.isoformat(),
        day_weather="晴",
        night_weather="晴",
        day_temp="24",
        night_temp="16",
        day_wind="东",
        night_wind="东",
        day_power="3",
        night_power="3",
        source=source,
    )


def test_normalize_weather_date_range_defaults_to_seven_days() -> None:
    today = date.today()

    range_start, range_end, warnings = main_module._normalize_weather_date_range(None, None)

    assert range_start == today
    assert range_end == today + timedelta(days=6)
    assert warnings == []


def test_normalize_weather_date_range_truncates_after_sixteen_days() -> None:
    today = date.today()

    range_start, range_end, warnings = main_module._normalize_weather_date_range(
        today.isoformat(),
        (today + timedelta(days=29)).isoformat(),
    )

    assert range_start == today
    assert range_end == today + timedelta(days=15)
    assert "最多支持未来 16 天" in warnings[0]


def test_normalize_weather_date_range_rejects_start_after_forecast_window() -> None:
    today = date.today()

    with pytest.raises(ValueError, match="最多支持未来 16 天"):
        main_module._normalize_weather_date_range(
            (today + timedelta(days=16)).isoformat(),
            (today + timedelta(days=17)).isoformat(),
        )


def test_weather_forecasts_use_amap_for_near_days_and_open_meteo_for_remainder(monkeypatch) -> None:
    today = date.today()
    open_meteo_calls = []

    class AmapProvider:
        def daily_forecast(self, coordinate: Coordinate):
            return [_daily_forecast(today + timedelta(days=offset), "amap-forecast") for offset in range(4)]

    class OpenMeteoProvider:
        def daily_forecast(self, coordinate: Coordinate, start_date: date, end_date: date):
            open_meteo_calls.append((start_date, end_date))
            return [
                _daily_forecast(start_date + timedelta(days=offset), "open-meteo")
                for offset in range((end_date - start_date).days + 1)
            ]

    monkeypatch.setattr(main_module, "_amap_weather", AmapProvider())
    monkeypatch.setattr(main_module, "_open_meteo_weather", OpenMeteoProvider())

    forecasts, warnings = main_module._weather_forecasts_for_range(
        Coordinate(lon=121.4737, lat=31.2304),
        today,
        today + timedelta(days=6),
    )

    assert warnings == []
    assert len(forecasts) == 7
    assert [forecast.source for forecast in forecasts] == ["amap-forecast"] * 4 + ["open-meteo"] * 3
    assert open_meteo_calls == [(today + timedelta(days=4), today + timedelta(days=6))]
