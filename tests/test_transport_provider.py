from app.models import Coordinate, TransportPlan
from app.providers.transport import AmadeusFlightPriceProvider, StaticTransportProvider


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


def test_amadeus_flight_provider_parses_cheapest_offer(monkeypatch) -> None:
    calls = []

    def fake_post(url, data, timeout):
        calls.append(("POST", url, data))
        return __import__("httpx").Response(
            200,
            request=__import__("httpx").Request("POST", url),
            json={"access_token": "token"},
        )

    def fake_get(url, headers, params, timeout):
        calls.append(("GET", url, params))
        return __import__("httpx").Response(
            200,
            request=__import__("httpx").Request("GET", url),
            json={
                "data": [
                    {
                        "price": {"grandTotal": "980.00", "currency": "CNY"},
                        "itineraries": [{"segments": [{"departure": {"iataCode": "SHA"}, "arrival": {"iataCode": "KHN"}, "carrierCode": "MU", "number": "1234"}]}],
                    },
                    {"price": {"grandTotal": "1200.00", "currency": "CNY"}},
                ]
            },
        )

    import httpx

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr(httpx, "get", fake_get)

    provider = AmadeusFlightPriceProvider(client_id="id", client_secret="secret")
    option = provider.cheapest_offer("上海", "武功山", __import__("datetime").date(2026, 7, 1))

    assert option.mode == "flight"
    assert option.price_estimate == "980.00 CNY"
    assert option.source == "amadeus-flight-offers"
    assert any("SHA" in step and "KHN" in step for step in option.steps)
