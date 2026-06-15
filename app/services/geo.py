from __future__ import annotations

from math import asin, cos, radians, sin, sqrt

from app.models import Coordinate

EARTH_RADIUS_M = 6_371_000


def haversine_m(a: Coordinate, b: Coordinate) -> float:
    lat1 = radians(a.lat)
    lat2 = radians(b.lat)
    dlat = radians(b.lat - a.lat)
    dlon = radians(b.lon - a.lon)
    h = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_M * asin(sqrt(h))


def line_distance_m(coordinates: list[Coordinate]) -> float:
    return sum(
        haversine_m(start, end)
        for start, end in zip(coordinates, coordinates[1:], strict=False)
    )
