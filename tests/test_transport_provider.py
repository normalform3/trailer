from app.models import Coordinate, TransportPlan
from app.providers.transport import AmapDrivingDurationProvider, SerpApiFlightSearchProvider, StaticTransportProvider


def test_static_transport_provider_returns_generic_plan() -> None:
    provider = StaticTransportProvider()
    plan = provider.plan(
        start_city="上海",
        destination_coordinate=Coordinate(lon=114.17, lat=27.46),
        destination_name="武功山",
    )

    assert isinstance(plan, TransportPlan)
    assert plan.start_city == "上海"
    assert plan.destination_name == "武功山"
    assert {option.mode for option in plan.options} == {"driving", "flight", "rail", "charter"}
    assert all(option.requires_user_verification for option in plan.options)
    assert plan.is_same_region is False
    assert plan.warnings


def test_static_transport_plan_has_steps() -> None:
    provider = StaticTransportProvider()
    plan = provider.plan(
        start_city="北京",
        destination_coordinate=Coordinate(lon=118.17, lat=30.13),
        destination_name="黄山",
    )

    rail = next(option for option in plan.options if option.mode == "rail")
    assert len(rail.steps) >= 2
    assert any("北京" in step for step in rail.steps)
    assert any("黄山" in step for step in rail.steps)
    assert "12306" in (rail.tip or "")


def test_static_transport_plan_serialization() -> None:
    """Test that TransportPlan can be serialized via model_dump."""
    provider = StaticTransportProvider()
    plan = provider.plan(
        start_city="上海",
        destination_coordinate=Coordinate(lon=114.17, lat=27.46),
        destination_name="武功山",
    )

    data = plan.model_dump()
    assert data["start_city"] == "上海"
    assert data["destination_name"] == "武功山"
    assert isinstance(data["options"], list)
    assert len(data["options"]) == 4
    assert any(option["mode"] == "flight" for option in data["options"])


def test_serpapi_flight_provider_parses_cheapest_offer(monkeypatch) -> None:
    calls = []

    def fake_get(url, params, timeout):
        calls.append(("GET", url, params))
        return __import__("httpx").Response(
            200,
            request=__import__("httpx").Request("GET", url),
            json={
                "best_flights": [
                    {
                        "price": 980,
                        "total_duration": 135,
                        "flights": [
                            {
                                "departure_airport": {"id": "SHA", "time": "2026-07-01 08:00"},
                                "arrival_airport": {"id": "KHN", "time": "2026-07-01 10:15"},
                                "airline": "China Eastern",
                                "flight_number": "MU 1234",
                            }
                        ],
                    },
                    {"price": 1200, "total_duration": 180, "flights": []},
                ],
            },
        )

    import httpx

    monkeypatch.setattr(httpx, "get", fake_get)

    provider = SerpApiFlightSearchProvider(api_key="serpapi-key")
    option = provider.cheapest_offer("上海", "武功山", __import__("datetime").date(2026, 7, 1))

    assert option.mode == "flight"
    assert option.price_estimate == "980 CNY"
    assert option.duration_hours == 2.2
    assert option.source == "serpapi-google-flights"
    assert any("SHA" in step and "KHN" in step for step in option.steps)
    assert calls[0][2]["engine"] == "google_flights"
    assert calls[0][2]["departure_id"] == "SHA"
    assert calls[0][2]["arrival_id"] == "KHN"


def test_amap_driving_provider_parses_duration_and_distance(monkeypatch) -> None:
    calls = []

    def fake_get(url, params, timeout):
        calls.append((url, params))
        if "geocode" in url:
            return __import__("httpx").Response(
                200,
                request=__import__("httpx").Request("GET", url),
                json={"geocodes": [{"location": "121.4737,31.2304"}]},
            )
        return __import__("httpx").Response(
            200,
            request=__import__("httpx").Request("GET", url),
            json={"route": {"paths": [{"duration": "14400", "distance": "520000"}]}},
        )

    import httpx

    monkeypatch.setattr(httpx, "get", fake_get)

    provider = AmapDrivingDurationProvider(api_key="amap-key")
    option = provider.estimate("上海", Coordinate(lon=114.17, lat=27.46))

    assert option.mode == "driving"
    assert option.duration_hours == 4.0
    assert option.distance_km == 520.0
    assert option.source == "amap-driving"
    assert any("高德估算驾车约 4.0 小时" in step for step in option.steps)
    assert calls[1][1]["origin"] == "121.4737,31.2304"
