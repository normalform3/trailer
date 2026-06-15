from __future__ import annotations

from math import cos, pi, sin, sqrt

from app.models import Coordinate

EE = 0.00669342162296594323
A = 6378245.0


def out_of_china(lon: float, lat: float) -> bool:
    return lon < 72.004 or lon > 137.8347 or lat < 0.8293 or lat > 55.8271


def _transform_lat(lon: float, lat: float) -> float:
    ret = -100.0 + 2.0 * lon + 3.0 * lat + 0.2 * lat * lat
    ret += 0.1 * lon * lat + 0.2 * sqrt(abs(lon))
    ret += (20.0 * sin(6.0 * lon * pi) + 20.0 * sin(2.0 * lon * pi)) * 2.0 / 3.0
    ret += (20.0 * sin(lat * pi) + 40.0 * sin(lat / 3.0 * pi)) * 2.0 / 3.0
    ret += (160.0 * sin(lat / 12.0 * pi) + 320 * sin(lat * pi / 30.0)) * 2.0 / 3.0
    return ret


def _transform_lon(lon: float, lat: float) -> float:
    ret = 300.0 + lon + 2.0 * lat + 0.1 * lon * lon
    ret += 0.1 * lon * lat + 0.1 * sqrt(abs(lon))
    ret += (20.0 * sin(6.0 * lon * pi) + 20.0 * sin(2.0 * lon * pi)) * 2.0 / 3.0
    ret += (20.0 * sin(lon * pi) + 40.0 * sin(lon / 3.0 * pi)) * 2.0 / 3.0
    ret += (150.0 * sin(lon / 12.0 * pi) + 300.0 * sin(lon / 30.0 * pi)) * 2.0 / 3.0
    return ret


def wgs84_to_gcj02(point: Coordinate) -> Coordinate:
    if out_of_china(point.lon, point.lat):
        return point

    dlat = _transform_lat(point.lon - 105.0, point.lat - 35.0)
    dlon = _transform_lon(point.lon - 105.0, point.lat - 35.0)
    radlat = point.lat / 180.0 * pi
    magic = sin(radlat)
    magic = 1 - EE * magic * magic
    sqrt_magic = sqrt(magic)
    dlat = (dlat * 180.0) / ((A * (1 - EE)) / (magic * sqrt_magic) * pi)
    dlon = (dlon * 180.0) / (A / sqrt_magic * cos(radlat) * pi)
    return Coordinate(
        lon=point.lon + dlon,
        lat=point.lat + dlat,
        elevation_m=point.elevation_m,
    )


def gcj02_to_wgs84(point: Coordinate) -> Coordinate:
    if out_of_china(point.lon, point.lat):
        return point
    converted = wgs84_to_gcj02(point)
    return Coordinate(
        lon=point.lon * 2 - converted.lon,
        lat=point.lat * 2 - converted.lat,
        elevation_m=point.elevation_m,
    )
