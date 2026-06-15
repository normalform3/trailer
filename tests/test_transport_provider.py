from app.models import Coordinate, TransportPlan
from app.providers.transport import StaticTransportProvider


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
    assert len(plan.options) == 1
    assert plan.options[0].mode == "mixed"
    assert plan.options[0].source == "static"
    assert plan.is_same_region is False
    assert plan.warnings


def test_static_transport_plan_has_steps() -> None:
    provider = StaticTransportProvider()
    plan = provider.plan(
        start_city="北京",
        destination_coordinate=Coordinate(lon=118.17, lat=30.13),
        destination_name="黄山",
    )

    assert len(plan.options[0].steps) >= 2
    assert any("北京" in step for step in plan.options[0].steps)
    assert any("黄山" in step for step in plan.options[0].steps)


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
    assert len(data["options"]) == 1
