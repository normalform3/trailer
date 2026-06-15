from app.models import Coordinate, RouteGeometry, RouteSource
from app.providers.weather import WeatherSnapshot
from app.services.route_analysis import RouteAnalysisService


class FailingElevationProvider:
    def enrich(self, coordinates: list[Coordinate]) -> list[Coordinate]:
        raise RuntimeError("boom")


def test_route_analysis_distance_elevation_and_weather_risk() -> None:
    route = RouteGeometry(
        name="test",
        source=RouteSource.USER_KML,
        confidence=0.9,
        coordinates=[
            Coordinate(lon=0, lat=0, elevation_m=100),
            Coordinate(lon=0.01, lat=0, elevation_m=300),
            Coordinate(lon=0.02, lat=0, elevation_m=200),
        ],
    )
    weather = WeatherSnapshot(
        max_temp_c=34,
        min_temp_c=8,
        precipitation_probability=70,
        max_wind_kmh=20,
        source="test",
    )

    _, analysis = RouteAnalysisService().analyze(route, weather)

    assert analysis.distance_km > 2
    assert analysis.elevation.ascent_m == 200
    assert analysis.elevation.descent_m == 100
    assert "降水概率较高" in analysis.risk_factors
    assert "高温风险" in analysis.risk_factors
    assert analysis.risk_level == "medium"


def test_route_analysis_degrades_when_elevation_provider_fails() -> None:
    route = RouteGeometry(
        name="test",
        source=RouteSource.API_PLANNED,
        confidence=0.5,
        coordinates=[
            Coordinate(lon=0, lat=0),
            Coordinate(lon=0.01, lat=0),
        ],
    )

    _, analysis = RouteAnalysisService(FailingElevationProvider()).analyze(route)

    assert analysis.elevation.ascent_m is None
    assert any("海拔服务暂不可用" in warning for warning in analysis.warnings)
    assert any("缺少完整海拔数据" in warning for warning in analysis.warnings)
