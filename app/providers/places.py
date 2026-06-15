from __future__ import annotations

from typing import Protocol

import httpx

from app.models import Coordinate, Place


class PlaceProvider(Protocol):
    def resolve(self, destination: str) -> Place | None:
        raise NotImplementedError


class StaticPlaceProvider:
    def __init__(self) -> None:
        self._places = {
            "武功山": Place(
                name="武功山",
                coordinate=Coordinate(lon=114.1707, lat=27.4631),
                source="static",
                confidence=0.72,
            ),
            "四姑娘山": Place(
                name="四姑娘山",
                coordinate=Coordinate(lon=102.9003, lat=31.1018),
                source="static",
                confidence=0.72,
            ),
            "黄山": Place(
                name="黄山",
                coordinate=Coordinate(lon=118.1660, lat=30.1320),
                source="static",
                confidence=0.72,
            ),
        }

    def resolve(self, destination: str) -> Place | None:
        for keyword, place in self._places.items():
            if keyword in destination:
                return place
        return None


class AmapPlaceProvider:
    def __init__(self, api_key: str, timeout_s: float = 8.0) -> None:
        self.api_key = api_key
        self.timeout_s = timeout_s

    def resolve(self, destination: str) -> Place | None:
        response = httpx.get(
            "https://restapi.amap.com/v3/geocode/geo",
            params={"key": self.api_key, "address": destination},
            timeout=self.timeout_s,
        )
        response.raise_for_status()
        payload = response.json()
        geocodes = payload.get("geocodes") or []
        if not geocodes:
            return None
        first = geocodes[0]
        location = first.get("location")
        if not location:
            return None
        lon_s, lat_s = location.split(",", 1)
        return Place(
            name=first.get("formatted_address") or destination,
            coordinate=Coordinate(lon=float(lon_s), lat=float(lat_s)),
            source="amap_gcj02",
            confidence=0.84,
        )
