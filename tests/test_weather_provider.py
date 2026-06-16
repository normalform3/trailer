from datetime import date, timedelta

import httpx

from app.models import Coordinate
from app.models import WeatherDetail
from app.providers.weather import AmapWeatherProvider, CompositeWeatherProvider, OpenMeteoWeatherProvider, WeatherSnapshot


def test_amap_weather_provider_resolves_adcode_and_weather(monkeypatch) -> None:
    calls = []

    def fake_get(url, params, timeout):
        calls.append((url, params, timeout))
        if url.endswith("/v3/geocode/regeo"):
            return httpx.Response(
                200,
                request=httpx.Request("GET", url),
                json={
                    "status": "1",
                    "regeocode": {"addressComponent": {"adcode": "360300"}},
                },
            )
        if url.endswith("/v3/weather/weatherInfo"):
            return httpx.Response(
                200,
                request=httpx.Request("GET", url),
                json={
                    "status": "1",
                    "lives": [
                        {
                            "temperature": "23",
                            "windpower": "3级",
                        }
                    ],
                },
            )
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr(httpx, "get", fake_get)

    provider = AmapWeatherProvider(api_key="test-key")
    snapshot = provider.forecast(Coordinate(lon=114.1707, lat=27.4631))

    assert snapshot is not None
    assert snapshot.source == "amap-weather"
    assert snapshot.max_temp_c == 23
    assert snapshot.min_temp_c == 23
    assert snapshot.max_wind_kmh == 19
    assert calls[0][1]["key"] == "test-key"
    assert calls[0][1]["location"]
    assert calls[1][1]["city"] == "360300"


def test_composite_weather_provider_degrades_when_one_provider_fails() -> None:
    class FailingProvider:
        def forecast(self, coordinate):
            raise RuntimeError("down")

        def details(self, coordinate):
            raise RuntimeError("down")

    class FixedProvider:
        def forecast(self, coordinate):
            return WeatherSnapshot(
                max_temp_c=30,
                min_temp_c=20,
                precipitation_probability=70,
                wind_gust_kmh=58,
                uv_index_max=9,
                hiking_risk_notes=("降水概率较高，注意湿滑、涉水和能见度",),
                source="fixed",
            )

        def details(self, coordinate):
            return [
                WeatherDetail(
                    title="路线区域天气",
                    detail="降水概率 70%",
                    date="2026-07-01",
                    precipitation_probability=70,
                    hiking_risk_notes=["降水概率较高，注意湿滑、涉水和能见度"],
                    source="fixed",
                )
            ]

    provider = CompositeWeatherProvider([FailingProvider(), FixedProvider()])

    snapshot = provider.forecast(Coordinate(lon=114.1707, lat=27.4631))
    details = provider.details(Coordinate(lon=114.1707, lat=27.4631))

    assert snapshot is not None
    assert snapshot.source == "fixed"
    assert snapshot.wind_gust_kmh == 58
    assert details[0].date == "2026-07-01"
    assert details[0].hiking_risk_notes


def test_open_meteo_daily_forecast_uses_requested_date_range(monkeypatch) -> None:
    calls = []
    start = date.today()
    end = start + timedelta(days=1)

    def fake_get(url, params, timeout):
        calls.append((url, params, timeout))
        return httpx.Response(
            200,
            request=httpx.Request("GET", url),
            json={
                "daily": {
                    "time": [start.isoformat(), end.isoformat()],
                    "weather_code": [1, 95],
                    "temperature_2m_max": [24, 22],
                    "temperature_2m_min": [16, 15],
                    "precipitation_probability_max": [10, 80],
                    "precipitation_sum": [0.0, 12.5],
                    "wind_speed_10m_max": [12, 18],
                    "wind_gusts_10m_max": [20, 30],
                    "wind_direction_10m_dominant": [90, 180],
                    "uv_index_max": [5, 4],
                }
            },
        )

    monkeypatch.setattr(httpx, "get", fake_get)

    provider = OpenMeteoWeatherProvider()
    forecasts = provider.daily_forecast(
        Coordinate(lon=114.1707, lat=27.4631),
        start,
        end,
    )

    assert calls[0][0] == "https://api.open-meteo.com/v1/forecast"
    assert calls[0][1]["start_date"] == start.isoformat()
    assert calls[0][1]["end_date"] == end.isoformat()
    assert "forecast_days" not in calls[0][1]
    assert forecasts[0].source == "open-meteo"
    assert forecasts[0].day_weather == "晴间多云"
    assert forecasts[0].suitability_label == "适宜"
    assert forecasts[1].day_weather == "雷雨"
    assert forecasts[1].suitability_label == "不宜"
    assert forecasts[1].hiking_risk_notes


def test_open_meteo_daily_forecast_uses_seasonal_api_for_long_ranges(monkeypatch) -> None:
    calls = []

    def fake_get(url, params, timeout):
        calls.append((url, params, timeout))
        return httpx.Response(
            200,
            request=httpx.Request("GET", url),
            json={
                "daily": {
                    "time": ["2026-07-01"],
                    "weather_code": [[0, 1, 2]],
                    "temperature_2m_max": [[23, 24, 25]],
                    "temperature_2m_min": [[15, 16, 17]],
                    "precipitation_sum": [[0.0, 0.1, 0.0]],
                    "wind_speed_10m_max": [[10, 11, 12]],
                    "wind_gusts_10m_max": [[18, 19, 20]],
                    "wind_direction_10m_dominant": [[80, 90, 100]],
                }
            },
        )

    monkeypatch.setattr(httpx, "get", fake_get)

    provider = OpenMeteoWeatherProvider()
    forecasts = provider.daily_forecast(
        Coordinate(lon=114.1707, lat=27.4631),
        date(2026, 7, 1),
        date(2026, 7, 31),
    )

    assert calls[0][0] == "https://seasonal-api.open-meteo.com/v1/seasonal"
    assert "models" not in calls[0][1]
    assert "weather_code" in calls[0][1]["daily"]
    assert forecasts[0].source == "open-meteo-seasonal"
    assert forecasts[0].day_weather == "晴间多云"
    assert forecasts[0].day_temp == "24"
    assert forecasts[0].night_temp == "16"


def test_open_meteo_daily_forecast_uses_seasonal_api_for_future_dates_within_16_days(monkeypatch) -> None:
    calls = []
    future_start = date.today().replace(year=date.today().year + 1)
    future_end = future_start.replace(day=min(future_start.day + 15, 28))

    def fake_get(url, params, timeout):
        calls.append((url, params, timeout))
        return httpx.Response(
            200,
            request=httpx.Request("GET", url),
            json={
                "daily": {
                    "time": [future_start.isoformat()],
                    "weather_code": [1],
                    "temperature_2m_max": [24],
                    "temperature_2m_min": [16],
                    "precipitation_sum": [0.0],
                    "wind_speed_10m_max": [12],
                    "wind_gusts_10m_max": [20],
                    "wind_direction_10m_dominant": [90],
                }
            },
        )

    monkeypatch.setattr(httpx, "get", fake_get)

    provider = OpenMeteoWeatherProvider()
    forecasts = provider.daily_forecast(
        Coordinate(lon=100.080441, lat=27.181653),
        future_start,
        future_end,
    )

    assert calls[0][0] == "https://seasonal-api.open-meteo.com/v1/seasonal"
    assert forecasts[0].source == "open-meteo-seasonal"
