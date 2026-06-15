from __future__ import annotations

import httpx

from app.config import get_settings
from app.models import Coordinate, Place, RouteGeometry, RouteSource
from app.providers.places import AmapPlaceProvider, PlaceProvider, StaticPlaceProvider
from app.services.coordinates import gcj02_to_wgs84


class RoutePlannerService:
    def __init__(
        self,
        place_provider: PlaceProvider | None = None,
        ors_api_key: str | None = None,
        timeout_s: float = 10.0,
    ) -> None:
        self.place_provider = place_provider or self._default_place_provider()
        self.ors_api_key = ors_api_key if ors_api_key is not None else get_settings().api_keys.ors_api_key
        self.timeout_s = timeout_s

    def plan(
        self,
        destination: str,
        route_text: str | None = None,
        max_candidates: int = 3,
    ) -> list[RouteGeometry]:
        place = self.place_provider.resolve(destination)
        if place is None:
            place = Place(
                name=destination,
                coordinate=Coordinate(lon=116.3975, lat=39.9087),
                source="fallback",
                confidence=0.25,
            )

        if self.ors_api_key:
            try:
                return self._plan_with_ors(place, route_text, max_candidates)
            except Exception:
                pass

        return self._fallback_routes(place, route_text, max_candidates)

    def _plan_with_ors(
        self,
        place: Place,
        route_text: str | None,
        max_candidates: int,
    ) -> list[RouteGeometry]:
        center = self._as_wgs84(place)
        coordinates = [
            [center.lon - 0.015, center.lat - 0.01],
            [center.lon + 0.015, center.lat + 0.01],
        ]
        response = httpx.post(
            "https://api.openrouteservice.org/v2/directions/foot-hiking/geojson",
            headers={"Authorization": self.ors_api_key},
            json={
                "coordinates": coordinates,
                "instructions": True,
                "elevation": True,
                "extra_info": ["steepness", "surface", "waytype", "traildifficulty"],
            },
            timeout=self.timeout_s,
        )
        response.raise_for_status()
        payload = response.json()
        routes: list[RouteGeometry] = []
        for index, feature in enumerate(payload.get("features") or [], start=1):
            line = feature.get("geometry", {}).get("coordinates") or []
            points = [
                Coordinate(
                    lon=float(item[0]),
                    lat=float(item[1]),
                    elevation_m=float(item[2]) if len(item) > 2 else None,
                )
                for item in line
            ]
            if len(points) < 2:
                continue
            routes.append(
                RouteGeometry(
                    name=f"{place.name} ORS 徒步候选 {index}",
                    coordinates=points,
                    source=RouteSource.USER_TEXT_PLANNED if route_text else RouteSource.API_PLANNED,
                    confidence=0.68 if route_text else 0.6,
                    description=route_text,
                    metadata={
                        "provider": "openrouteservice",
                        "place_source": place.source,
                        "ors_properties": feature.get("properties", {}),
                    },
                )
            )
            if len(routes) >= max_candidates:
                break
        if not routes:
            raise RuntimeError("OpenRouteService returned no usable routes")
        return routes

    def _fallback_routes(
        self,
        place: Place,
        route_text: str | None,
        max_candidates: int,
    ) -> list[RouteGeometry]:
        center = self._as_wgs84(place)
        templates = [
            [(-0.012, -0.008, 900), (0.002, 0.006, 1180), (0.016, 0.012, 1030)],
            [(-0.018, 0.004, 860), (0.0, 0.018, 1260), (0.018, -0.002, 910)],
            [(-0.01, -0.014, 820), (0.012, 0.0, 1120), (0.004, 0.016, 1080)],
        ]
        routes: list[RouteGeometry] = []
        for index, offsets in enumerate(templates[:max_candidates], start=1):
            points = [
                Coordinate(
                    lon=center.lon + lon_offset,
                    lat=center.lat + lat_offset,
                    elevation_m=elevation,
                )
                for lon_offset, lat_offset, elevation in offsets
            ]
            routes.append(
                RouteGeometry(
                    name=f"{place.name} 徒步规划候选 {index}",
                    coordinates=points,
                    source=RouteSource.USER_TEXT_PLANNED if route_text else RouteSource.API_PLANNED,
                    confidence=0.52 if route_text else 0.45,
                    description=route_text or "基于目的地坐标生成的规划候选，需结合真实地图核验。",
                    metadata={
                        "provider": "fallback-planner",
                        "place_source": place.source,
                        "note": "No live routing API was used.",
                    },
                )
            )
        return routes

    def _as_wgs84(self, place: Place) -> Coordinate:
        if place.source == "amap_gcj02":
            return gcj02_to_wgs84(place.coordinate)
        return place.coordinate

    def _default_place_provider(self) -> PlaceProvider:
        amap_key = get_settings().api_keys.amap_api_key
        if amap_key:
            return AmapPlaceProvider(amap_key)
        return StaticPlaceProvider()
