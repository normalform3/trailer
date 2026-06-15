from __future__ import annotations

import xml.etree.ElementTree as ET

from pydantic import ValidationError

from app.models import Coordinate, RouteGeometry, RouteSource


class RouteIngestionError(ValueError):
    pass


class RouteIngestionService:
    def parse_kml(self, content: bytes | str) -> list[RouteGeometry]:
        if isinstance(content, bytes):
            text = content.decode("utf-8-sig")
        else:
            text = content

        if not text.strip():
            raise RouteIngestionError("KML file is empty")

        try:
            root = ET.fromstring(text)
        except ET.ParseError as exc:
            raise RouteIngestionError(f"Invalid KML XML: {exc}") from exc

        routes: list[RouteGeometry] = []
        has_placemarks = False
        has_non_linestring = False
        for index, placemark in enumerate(self._find_by_local_name(root, "Placemark"), start=1):
            has_placemarks = True
            name = self._child_text(placemark, "name") or f"KML Route {index}"
            description = self._child_text(placemark, "description")

            # 1. 优先处理标准 LineString
            line_strings = self._find_by_local_name(placemark, "LineString")
            for line_index, line_string in enumerate(line_strings, start=1):
                coordinates_text = self._child_text(line_string, "coordinates")
                if not coordinates_text:
                    continue
                coordinates = self._parse_coordinates(coordinates_text)
                route_name = name if line_index == 1 else f"{name} #{line_index}"
                try:
                    routes.append(
                        RouteGeometry(
                            name=route_name,
                            coordinates=coordinates,
                            source=RouteSource.USER_KML,
                            confidence=0.95,
                            description=description,
                            metadata={"placemark_index": index},
                        )
                    )
                except ValidationError as exc:
                    raise RouteIngestionError(
                        f"KML 路线 '{route_name}' 坐标点不足（至少需要 2 个点）"
                    ) from exc

            # 2. 支持 gx:Track（两步路等 App 导出格式）
            gx_tracks = self._find_by_local_name(placemark, "Track")
            for track_index, gx_track in enumerate(gx_tracks, start=1):
                coordinates = self._parse_gx_coords(gx_track)
                if not coordinates:
                    continue
                route_name = name if track_index == 1 else f"{name} #{track_index}"
                try:
                    routes.append(
                        RouteGeometry(
                            name=route_name,
                            coordinates=coordinates,
                            source=RouteSource.USER_KML,
                            confidence=0.95,
                            description=description,
                            metadata={"placemark_index": index, "type": "gx:Track"},
                        )
                    )
                except ValidationError as exc:
                    raise RouteIngestionError(
                        f"KML 路线 '{route_name}' 坐标点不足（至少需要 2 个点）"
                    ) from exc

            if not line_strings and not gx_tracks:
                has_non_linestring = True

        if not routes:
            if not has_placemarks:
                raise RouteIngestionError("KML 中未找到任何 Placemark 要素")
            if has_non_linestring:
                raise RouteIngestionError(
                    "KML 中的 Placemark 不含 LineString / gx:Track 轨迹（可能是 Point 或 Polygon 类型）。"
                    "请在两步路/奥维等软件中导出轨迹（Track/Route）而非标注点。"
                )
            raise RouteIngestionError("KML 中未找到有效的 LineString 路线")

        return routes

    def _parse_gx_coords(self, gx_track: ET.Element) -> list[Coordinate]:
        """解析 gx:Track 内的 gx:coord 元素（格式：lon lat alt，空格分隔）"""
        coordinates: list[Coordinate] = []
        for coord_el in self._find_by_local_name(gx_track, "coord"):
            if not coord_el.text:
                continue
            parts = coord_el.text.strip().split()
            if len(parts) < 2:
                continue
            try:
                lon = float(parts[0])
                lat = float(parts[1])
                elevation = float(parts[2]) if len(parts) >= 3 else None
            except ValueError as exc:
                raise RouteIngestionError(
                    f"Invalid gx:coord value: {coord_el.text.strip()!r}"
                ) from exc
            coordinates.append(Coordinate(lon=lon, lat=lat, elevation_m=elevation))
        return coordinates

    def _parse_coordinates(self, coordinates_text: str) -> list[Coordinate]:
        coordinates: list[Coordinate] = []
        for token in coordinates_text.replace("\n", " ").split():
            parts = token.split(",")
            if len(parts) < 2:
                continue
            try:
                lon = float(parts[0])
                lat = float(parts[1])
                elevation = float(parts[2]) if len(parts) >= 3 and parts[2] else None
            except ValueError as exc:
                raise RouteIngestionError(f"Invalid KML coordinate token: {token}") from exc
            coordinates.append(Coordinate(lon=lon, lat=lat, elevation_m=elevation))
        return coordinates

    def _child_text(self, element: ET.Element, local_name: str) -> str | None:
        for child in self._find_by_local_name(element, local_name):
            if child.text and child.text.strip():
                return child.text.strip()
        return None

    def _find_by_local_name(self, element: ET.Element, local_name: str) -> list[ET.Element]:
        return [node for node in element.iter() if _local_name(node.tag) == local_name]


def _local_name(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[1]
    return tag
