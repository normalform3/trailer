from app.models import Coordinate, Place
from app.services.route_planner import RoutePlannerService


class FixedPlaceProvider:
    def resolve(self, destination: str) -> Place:
        return Place(
            name=destination,
            coordinate=Coordinate(lon=114.17, lat=27.46),
            source="static",
            confidence=0.7,
        )


def test_fallback_planner_marks_api_planned_routes() -> None:
    planner = RoutePlannerService(place_provider=FixedPlaceProvider(), ors_api_key="")

    routes = planner.plan("武功山")

    assert len(routes) == 3
    assert routes[0].source == "api_planned"
    assert routes[0].metadata["provider"] == "fallback-planner"


def test_route_text_changes_source_to_user_text_planned() -> None:
    planner = RoutePlannerService(place_provider=FixedPlaceProvider(), ors_api_key="")

    routes = planner.plan("武功山", route_text="从龙山村上山，发云界下山")

    assert routes[0].source == "user_text_planned"
    assert routes[0].description == "从龙山村上山，发云界下山"
