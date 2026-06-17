from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class RouteSource(StrEnum):
    USER_KML = "user_kml"
    USER_TEXT_PLANNED = "user_text_planned"
    API_PLANNED = "api_planned"


class Coordinate(BaseModel):
    lon: float
    lat: float
    elevation_m: float | None = None


class Place(BaseModel):
    name: str
    coordinate: Coordinate
    source: str = "static"
    confidence: float = Field(default=0.5, ge=0, le=1)


class HikingGuideRequest(BaseModel):
    destination: str = Field(min_length=1)
    start_city: str | None = None
    date_range: tuple[date, date] | None = None
    fitness_level: str | None = Field(
        default=None, description="beginner, intermediate, advanced, or free text"
    )
    preferences: list[str] = Field(default_factory=list)
    route_text: str | None = Field(
        default=None,
        description="User supplied route description, e.g. start/end/trail notes.",
    )
    reference_links: list[str] = Field(
        default_factory=list,
        description="User supplied public guide links used only for supplemental reference planning.",
    )
    reference_notes: str | None = Field(
        default=None,
        description="User pasted guide notes used only for supplemental reference planning.",
    )

    @field_validator("destination")
    @classmethod
    def strip_destination(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("destination is required")
        return value

    @field_validator("reference_links")
    @classmethod
    def normalize_reference_links(cls, value: list[str]) -> list[str]:
        seen: set[str] = set()
        links: list[str] = []
        for item in value:
            link = str(item).strip()
            if not link or link in seen:
                continue
            seen.add(link)
            links.append(link)
        return links

    @field_validator("reference_notes")
    @classmethod
    def strip_reference_notes(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None


class RouteGeometry(BaseModel):
    name: str
    coordinates: list[Coordinate]
    source: RouteSource
    confidence: float = Field(ge=0, le=1)
    description: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("coordinates")
    @classmethod
    def require_at_least_two_points(cls, value: list[Coordinate]) -> list[Coordinate]:
        if len(value) < 2:
            raise ValueError("a route requires at least two coordinates")
        return value

    @property
    def start(self) -> Coordinate:
        return self.coordinates[0]

    @property
    def end(self) -> Coordinate:
        return self.coordinates[-1]


class ElevationStats(BaseModel):
    min_m: float | None = None
    max_m: float | None = None
    ascent_m: float | None = None
    descent_m: float | None = None


class RouteAnalysis(BaseModel):
    distance_km: float
    estimated_duration_hours: float
    elevation: ElevationStats
    risk_level: str
    risk_factors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class RouteCandidate(BaseModel):
    route: RouteGeometry
    analysis: RouteAnalysis
    label: str


class TravelInfoItem(BaseModel):
    title: str
    detail: str
    coordinate: Coordinate | None = None
    source: str = "static"
    confidence: float = Field(default=0.5, ge=0, le=1)
    distance_km: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class TravelResearch(BaseModel):
    weather: list[TravelInfoItem] = Field(default_factory=list)
    lodging: list[TravelInfoItem] = Field(default_factory=list)
    transport: list[TravelInfoItem] = Field(default_factory=list)
    food: list[TravelInfoItem] = Field(default_factory=list)
    supply: list[TravelInfoItem] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class GuideReferenceItem(BaseModel):
    title: str
    summary: str
    source: str = "user-reference"
    url: str | None = None
    route_clues: list[str] = Field(default_factory=list)
    lodging_clues: list[str] = Field(default_factory=list)
    supply_clues: list[str] = Field(default_factory=list)
    transport_clues: list[str] = Field(default_factory=list)
    risk_notes: list[str] = Field(default_factory=list)
    verification_items: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.45, ge=0, le=1)


class GuideReferenceResearch(BaseModel):
    items: list[GuideReferenceItem] = Field(default_factory=list)
    supplemental_summary: str | None = None
    itinerary_suggestions: list[str] = Field(default_factory=list)
    lodging_supply_transport_notes: list[str] = Field(default_factory=list)
    risk_notes: list[str] = Field(default_factory=list)
    verification_items: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class GuideToolPlan(BaseModel):
    query_weather: bool = True
    query_lodging: bool = True
    query_food: bool = True
    query_supply: bool = True
    query_transport: bool = True
    compose_with_llm: bool = True
    rationale: list[str] = Field(default_factory=list)


class GuideDecision(BaseModel):
    tool_plan: GuideToolPlan = Field(default_factory=GuideToolPlan)
    clarifying_questions: list[str] = Field(default_factory=list)
    validation_notes: list[str] = Field(default_factory=list)
    priority_notes: list[str] = Field(default_factory=list)


class AgentTraceEvent(BaseModel):
    phase: str
    title: str
    status: str = Field(description="running | completed | skipped | fallback | warning")
    detail: str | None = None
    tool_name: str | None = None
    rationale: list[str] = Field(default_factory=list)


class WeatherDetail(BaseModel):
    title: str
    detail: str
    date: str | None = None
    weather_text: str | None = None
    max_temp_c: float | None = None
    min_temp_c: float | None = None
    humidity_percent: float | None = None
    precipitation_probability: float | None = None
    precipitation_mm: float | None = None
    max_wind_kmh: float | None = None
    wind_gust_kmh: float | None = None
    wind_direction: str | None = None
    uv_index_max: float | None = None
    hiking_risk_notes: list[str] = Field(default_factory=list)
    source: str = "unknown"


class TransportOption(BaseModel):
    mode: str = Field(description="driving | transit | mixed")
    duration_hours: float | None = None
    distance_km: float | None = None
    cost_estimate: str | None = None
    price_estimate: str | None = None
    booking_hint: str | None = None
    requires_user_verification: bool = True
    steps: list[str] = Field(default_factory=list)
    tip: str | None = None
    source: str = "amap"


class TransportPlan(BaseModel):
    start_city: str
    start_coordinate: Coordinate | None = None
    destination_name: str
    destination_coordinate: Coordinate
    options: list[TransportOption] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    is_same_region: bool = True


class GearCategory(BaseModel):
    category: str = Field(description="基础装备 | 衣物防护 | 饮食补给 | 安全应急 | 电子导航 | 其他")
    items: list[str]


class GearList(BaseModel):
    categories: list[GearCategory] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class RiskPoint(BaseModel):
    location_description: str
    risk_type: str
    severity: str = Field(description="low | medium | high")
    mitigation: str


class SafetyGuide(BaseModel):
    general_warnings: list[str] = Field(default_factory=list)
    risk_points: list[RiskPoint] = Field(default_factory=list)
    emergency_contacts: list[str] = Field(default_factory=list)
    emergency_measures: list[str] = Field(default_factory=list)
    seasonal_notes: list[str] = Field(default_factory=list)


class DayPlan(BaseModel):
    day_number: int
    date: str | None = None
    title: str
    distance_km: float | None = None
    elevation_gain_m: float | None = None
    key_segments: list[str] = Field(default_factory=list)
    lodging_suggestion: str | None = None
    notes: list[str] = Field(default_factory=list)


class Itinerary(BaseModel):
    is_multi_day: bool = False
    total_days: int = 1
    days: list[DayPlan] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class HikingGuideResponse(BaseModel):
    destination: str
    summary: str
    route_candidates: list[RouteCandidate]
    travel_research: TravelResearch | None = None
    reference_research: GuideReferenceResearch | None = None
    transport_plan: TransportPlan | None = None
    weather_details: list[WeatherDetail] = Field(default_factory=list)
    itinerary: Itinerary | None = None
    gear_list: GearList | None = None
    safety_guide: SafetyGuide | None = None
    recommendations: list[str]
    data_sources: list[str]
    llm_usage: list[str] = Field(default_factory=list)
    agent_trace: list[AgentTraceEvent] = Field(default_factory=list)
    clarifying_questions: list[str] = Field(default_factory=list)
    validation_notes: list[str] = Field(default_factory=list)
    warnings: list[str]
    disclaimer: str
