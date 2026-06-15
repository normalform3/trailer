import httpx

from app.models import Coordinate
from app.providers.weather import AmapWeatherProvider


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
