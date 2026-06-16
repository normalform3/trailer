from __future__ import annotations

from datetime import date

from app.models import (
    Coordinate,
    HikingGuideRequest,
    RouteCandidate,
    TransportPlan,
    TravelResearch,
    WeatherDetail,
)
from app.providers.llm import TemplateGuideProvider
from app.providers.transport import TransportPlanningProvider
from app.providers.travel_research import TravelResearchProvider
from app.providers.weather import WeatherProvider, WeatherSnapshot


class GuideToolRegistry:
    """Typed wrappers around deterministic guide tools."""

    def __init__(
        self,
        weather_provider: WeatherProvider,
        travel_research_provider: TravelResearchProvider,
        transport_provider: TransportPlanningProvider,
        template_provider: TemplateGuideProvider,
    ) -> None:
        self.weather_provider = weather_provider
        self.travel_research_provider = travel_research_provider
        self.transport_provider = transport_provider
        self.template_provider = template_provider

    def weather_tool(self, coordinate: Coordinate) -> tuple[WeatherSnapshot | None, list[WeatherDetail]]:
        snapshot = self.weather_provider.forecast(coordinate)
        try:
            details = self.weather_provider.details(coordinate)
        except Exception:
            details = []
        return snapshot, details

    def poi_tool(
        self,
        request: HikingGuideRequest,
        candidates: list[RouteCandidate],
        weather_snapshots: list[WeatherSnapshot | None],
        include_lodging: bool,
        include_food: bool,
        include_supply: bool,
    ) -> TravelResearch:
        return self.travel_research_provider.collect(
            request,
            candidates,
            weather_snapshots,
            include_lodging=include_lodging,
            include_food=include_food,
            include_supply=include_supply,
        )

    def transport_options_tool(
        self,
        start_city: str,
        destination_coordinate: Coordinate,
        destination_name: str,
        departure_date: date | None,
    ) -> TransportPlan:
        return self.transport_provider.plan(
            start_city=start_city,
            destination_coordinate=destination_coordinate,
            destination_name=destination_name,
            departure_date=departure_date,
        )

    def guide_composer_tool(self) -> TemplateGuideProvider:
        return self.template_provider
