from datetime import date, timedelta

from app import main as main_module
from app.models import Coordinate
from app.providers.weather import DailyForecast


def test_weather_forecasts_for_long_range_falls_back_to_available_forecast(monkeypatch) -> None:
    today = date.today()
    calls = []

    class FlakyRangeProvider:
        def daily_forecast(self, coordinate: Coordinate, start_date: date, end_date: date):
            calls.append((start_date, end_date))
            if (end_date - start_date).days + 1 > 16:
                raise RuntimeError("seasonal timeout")
            return [
                DailyForecast(
                    date=start_date.isoformat(),
                    day_weather="晴",
                    night_weather="晴",
                    day_temp="24",
                    night_temp="16",
                    day_wind="东",
                    night_wind="东",
                    day_power="3",
                    night_power="3",
                    source="open-meteo",
                )
            ]

    monkeypatch.setattr(main_module, "_amap_weather", None)
    monkeypatch.setattr(main_module, "_open_meteo_weather", FlakyRangeProvider())

    forecasts, warnings = main_module._weather_forecasts_for_range(
        Coordinate(lon=121.4737, lat=31.2304),
        today,
        today + timedelta(days=29),
    )

    assert forecasts[0].date == today.isoformat()
    assert calls == [(today, today + timedelta(days=29)), (today, today + timedelta(days=15))]
    assert "长周期趋势服务暂不可用" in warnings[0]


def test_weather_forecasts_for_future_range_returns_warning_when_only_seasonal_fails(monkeypatch) -> None:
    today = date.today()
    range_start = today + timedelta(days=30)
    range_end = range_start + timedelta(days=15)
    calls = []

    class FailingSeasonalProvider:
        def daily_forecast(self, coordinate: Coordinate, start_date: date, end_date: date):
            calls.append((start_date, end_date))
            raise RuntimeError("seasonal timeout")

    monkeypatch.setattr(main_module, "_amap_weather", None)
    monkeypatch.setattr(main_module, "_open_meteo_weather", FailingSeasonalProvider())

    forecasts, warnings = main_module._weather_forecasts_for_range(
        Coordinate(lon=100.080441, lat=27.181653),
        range_start,
        range_end,
    )

    assert forecasts == []
    assert calls == [(range_start, range_end)]
    assert "超出常规 16 天预报窗口" in warnings[0]
