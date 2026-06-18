from __future__ import annotations

from app.models import Coordinate, ElevationStats, RouteAnalysis, RouteGeometry
from app.providers.elevation import ElevationProvider, ExistingElevationProvider
from app.providers.weather import WeatherSnapshot
from app.services.geo import line_distance_m


# KML/GPS altitude commonly oscillates by a few metres even on level ground.
# Treat smaller reversals as measurement noise instead of accumulating every
# saw-tooth as real ascent/descent.
ELEVATION_REVERSAL_THRESHOLD_M = 5.0


class RouteAnalysisService:
    def __init__(self, elevation_provider: ElevationProvider | None = None) -> None:
        self.elevation_provider = elevation_provider or ExistingElevationProvider()

    def analyze(
        self,
        route: RouteGeometry,
        weather: WeatherSnapshot | None = None,
    ) -> tuple[RouteGeometry, RouteAnalysis]:
        warnings: list[str] = []
        coordinates = route.coordinates
        if any(point.elevation_m is None for point in coordinates):
            try:
                coordinates = self.elevation_provider.enrich(coordinates)
                route = route.model_copy(update={"coordinates": coordinates})
            except Exception as exc:  # noqa: BLE001 - provider failures should degrade.
                warnings.append(f"海拔服务暂不可用：{exc}")

        distance_km = line_distance_m(route.coordinates) / 1000
        elevation = self._elevation_stats(route.coordinates)
        duration_hours = self._estimate_duration_hours(distance_km, elevation.ascent_m)
        risk_factors = self._risk_factors(distance_km, elevation, weather)
        risk_level = self._risk_level(risk_factors)

        if elevation.ascent_m is None:
            warnings.append("缺少完整海拔数据，累计爬升/下降和耗时估算精度较低")
        if weather is None:
            warnings.append("缺少天气数据，请出发前核对当地预警和景区公告")

        return route, RouteAnalysis(
            distance_km=round(distance_km, 2),
            estimated_duration_hours=round(duration_hours, 1),
            elevation=elevation,
            risk_level=risk_level,
            risk_factors=risk_factors,
            warnings=warnings,
        )

    def _elevation_stats(self, coordinates: list[Coordinate]) -> ElevationStats:
        elevations = [point.elevation_m for point in coordinates]
        if any(value is None for value in elevations):
            known = [value for value in elevations if value is not None]
            return ElevationStats(
                min_m=round(min(known), 1) if known else None,
                max_m=round(max(known), 1) if known else None,
                ascent_m=None,
                descent_m=None,
            )

        typed_elevations = [float(value) for value in elevations]
        significant_points = self._significant_elevation_points(typed_elevations)
        ascent = 0.0
        descent = 0.0
        for start, end in zip(significant_points, significant_points[1:], strict=False):
            delta = end - start
            if delta > 0:
                ascent += delta
            elif delta < 0:
                descent -= delta

        return ElevationStats(
            min_m=round(min(typed_elevations), 1),
            max_m=round(max(typed_elevations), 1),
            ascent_m=round(ascent, 1),
            descent_m=round(descent, 1),
        )

    def _significant_elevation_points(self, elevations: list[float]) -> list[float]:
        """Collapse small altitude reversals while retaining real gradual slopes."""
        if len(elevations) < 2:
            return elevations

        points = [elevations[0]]
        anchor = elevations[0]
        extreme = anchor
        trend = 0  # 1 climbing, -1 descending, 0 not established

        for elevation in elevations[1:]:
            if trend == 0:
                if elevation - anchor >= ELEVATION_REVERSAL_THRESHOLD_M:
                    trend = 1
                    extreme = elevation
                elif anchor - elevation >= ELEVATION_REVERSAL_THRESHOLD_M:
                    trend = -1
                    extreme = elevation
            elif trend > 0:
                if elevation > extreme:
                    extreme = elevation
                elif extreme - elevation >= ELEVATION_REVERSAL_THRESHOLD_M:
                    points.append(extreme)
                    trend = -1
                    extreme = elevation
            else:
                if elevation < extreme:
                    extreme = elevation
                elif elevation - extreme >= ELEVATION_REVERSAL_THRESHOLD_M:
                    points.append(extreme)
                    trend = 1
                    extreme = elevation

        if trend != 0 and extreme != points[-1]:
            points.append(extreme)
        return points

    def _estimate_duration_hours(self, distance_km: float, ascent_m: float | None) -> float:
        base = distance_km / 4.0
        if ascent_m is not None:
            base += ascent_m / 500.0
        return max(base, 0.2)

    def _risk_factors(
        self,
        distance_km: float,
        elevation: ElevationStats,
        weather: WeatherSnapshot | None,
    ) -> list[str]:
        factors: list[str] = []
        if distance_km >= 20:
            factors.append("路线较长")
        if elevation.ascent_m is not None and elevation.ascent_m >= 1200:
            factors.append("累计爬升较大")
        if elevation.max_m is not None and elevation.max_m >= 3000:
            factors.append("高海拔风险")
        if weather:
            if weather.precipitation_probability is not None and weather.precipitation_probability >= 60:
                factors.append("降水概率较高")
            if weather.max_wind_kmh is not None and weather.max_wind_kmh >= 40:
                factors.append("大风风险")
            if weather.max_temp_c is not None and weather.max_temp_c >= 32:
                factors.append("高温风险")
            if weather.min_temp_c is not None and weather.min_temp_c <= 0:
                factors.append("低温风险")
        return factors

    def _risk_level(self, risk_factors: list[str]) -> str:
        if len(risk_factors) >= 3:
            return "high"
        if risk_factors:
            return "medium"
        return "low"
