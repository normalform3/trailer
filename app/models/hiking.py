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

    @field_validator("destination")
    @classmethod
    def strip_destination(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("destination is required")
        return value


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


class TransportOption(BaseModel):
    mode: str = Field(description="driving | transit | mixed")
    duration_hours: float | None = None
    distance_km: float | None = None
    cost_estimate: str | None = None
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
    transport_plan: TransportPlan | None = None
    itinerary: Itinerary | None = None
    gear_list: GearList | None = None
    safety_guide: SafetyGuide | None = None
    recommendations: list[str]
    data_sources: list[str]
    warnings: list[str]
    disclaimer: str
