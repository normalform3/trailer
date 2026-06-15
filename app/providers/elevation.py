from __future__ import annotations

from typing import Protocol

import httpx

from app.models import Coordinate


class ElevationProvider(Protocol):
    def enrich(self, coordinates: list[Coordinate]) -> list[Coordinate]:
        raise NotImplementedError


class ExistingElevationProvider:
    def enrich(self, coordinates: list[Coordinate]) -> list[Coordinate]:
        return coordinates


class OpenTopoDataElevationProvider:
    def __init__(
        self,
        dataset: str = "srtm90m",
        base_url: str = "https://api.opentopodata.org/v1",
        timeout_s: float = 10.0,
    ) -> None:
        self.dataset = dataset
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s

    def enrich(self, coordinates: list[Coordinate]) -> list[Coordinate]:
        if not coordinates:
            return []

        enriched: list[Coordinate] = []
        for chunk_start in range(0, len(coordinates), 100):
            chunk = coordinates[chunk_start : chunk_start + 100]
            locations = "|".join(f"{point.lat},{point.lon}" for point in chunk)
            response = httpx.get(
                f"{self.base_url}/{self.dataset}",
                params={"locations": locations},
                timeout=self.timeout_s,
            )
            response.raise_for_status()
            payload = response.json()
            results = payload.get("results") or []
            if len(results) != len(chunk):
                raise RuntimeError("Elevation response point count mismatch")
            for point, result in zip(chunk, results, strict=True):
                elevation = result.get("elevation")
                enriched.append(
                    Coordinate(lon=point.lon, lat=point.lat, elevation_m=elevation)
                )
        return enriched
