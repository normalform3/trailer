from __future__ import annotations

from typing import Iterable, Protocol

import httpx

from app.config import get_settings
from app.models import Coordinate, HikingGuideRequest, RouteCandidate, RouteGeometry, TravelInfoItem, TravelResearch
from app.services.coordinates import wgs84_to_gcj02
from app.providers.weather import WeatherSnapshot


class TravelResearchProvider(Protocol):
    def collect(
        self,
        request: HikingGuideRequest,
        candidates: list[RouteCandidate],
        weather_snapshots: list[WeatherSnapshot | None],
        include_lodging: bool = True,
        include_food: bool = True,
        include_supply: bool = True,
    ) -> TravelResearch:
        raise NotImplementedError


class DefaultTravelResearchProvider:
    def __init__(
        self,
        amap_api_key: str | None = None,
        timeout_s: float = 8.0,
    ) -> None:
        api_key = amap_api_key if amap_api_key is not None else get_settings().api_keys.amap_api_key
        self.poi_provider = AmapPOIResearchProvider(api_key, timeout_s=timeout_s) if api_key else None
        self.fallback_provider = StaticTravelResearchProvider()

    def collect(
        self,
        request: HikingGuideRequest,
        candidates: list[RouteCandidate],
        weather_snapshots: list[WeatherSnapshot | None],
        include_lodging: bool = True,
        include_food: bool = True,
        include_supply: bool = True,
    ) -> TravelResearch:
        fallback = self.fallback_provider.collect(
            request,
            candidates,
            weather_snapshots,
            include_lodging=include_lodging,
            include_food=include_food,
            include_supply=include_supply,
        )
        if not self.poi_provider or not candidates:
            return fallback

        try:
            poi_research = self.poi_provider.collect(
                request,
                candidates,
                weather_snapshots,
                include_lodging=include_lodging,
                include_food=include_food,
                include_supply=include_supply,
            )
        except Exception as exc:  # noqa: BLE001 - POI failure should not block guide generation.
            fallback.warnings.append(f"高德 POI 查询暂不可用，已使用静态信息兜底：{exc}")
            return fallback

        return TravelResearch(
            weather=fallback.weather,
            lodging=poi_research.lodging or fallback.lodging,
            transport=poi_research.transport or fallback.transport,
            food=poi_research.food or fallback.food,
            supply=poi_research.supply or fallback.supply,
            next_steps=_dedupe([*fallback.next_steps, *poi_research.next_steps]),
            warnings=_dedupe([*fallback.warnings, *poi_research.warnings]),
        )


class StaticTravelResearchProvider:
    def collect(
        self,
        request: HikingGuideRequest,
        candidates: list[RouteCandidate],
        weather_snapshots: list[WeatherSnapshot | None],
        include_lodging: bool = True,
        include_food: bool = True,
        include_supply: bool = True,
    ) -> TravelResearch:
        best = candidates[0] if candidates else None
        destination = request.destination
        weather_items = _weather_items(weather_snapshots)
        lodging, transport, food, supply = self._destination_items(destination, request.start_city)
        if not include_lodging:
            lodging = []
        if not include_food:
            food = []
        if not include_supply:
            supply = []
        next_steps = [
            "在两步路等外部平台核对原路线的最新评论、封山绕行和实际通行情况。",
            "出发前一天再次核对天气预警、景区/林区公告、住宿余房和返程末班时间。",
        ]
        warnings: list[str] = []

        if best and best.route.source == "user_kml":
            next_steps.insert(0, "本次已以用户上传 KML 为主线，信息搜集应围绕轨迹起点、终点和下撤点展开。")
        if not weather_items:
            weather_items.append(
                TravelInfoItem(
                    title="天气待核实",
                    detail="当前没有拿到实时天气数据，请在出发前使用官方天气预警或 Open-Meteo/高德等服务复核。",
                    source="fallback",
                    confidence=0.35,
                )
            )
            warnings.append("缺少可展示的天气快照")

        return TravelResearch(
            weather=weather_items,
            lodging=lodging,
            transport=transport,
            food=food,
            supply=supply,
            next_steps=next_steps,
            warnings=warnings,
        )

    def _destination_items(
        self,
        destination: str,
        start_city: str | None,
    ) -> tuple[list[TravelInfoItem], list[TravelInfoItem], list[TravelInfoItem], list[TravelInfoItem]]:
        if "武功山" in destination:
            return (
                [
                    _item("住宿重点", "优先核对金顶、观音宕、发云界、龙山村等常见节点附近客栈或帐篷营地。"),
                    _item("订房提醒", "热门周末和节假日需要提前确认余房、热水、晚餐和次日早餐。"),
                ],
                [
                    _item("外部交通", _traffic_detail(start_city, "萍乡北/宜春等高铁站，再转景区或登山口接驳")),
                    _item("返程约束", "先查下山口到高铁站/汽车站的末班车或包车可达性，避免把返程压到夜间。"),
                ],
                [
                    _item("餐饮节点", "重点核对登山口、金顶、观音宕、发云界附近是否可晚餐和早餐。"),
                ],
                [
                    _item("补给提醒", "山脊线路补给不稳定，建议确认水源/小卖部营业并自带高热量路餐。"),
                    _item("应急点", "记录景区服务点、客栈电话、下撤口和可通车路口。"),
                ],
            )

        if "四姑娘山" in destination:
            return (
                [_item("住宿重点", "优先核对日隆镇/四姑娘山镇住宿，确认是否能寄存行李和安排早出发。")],
                [_item("外部交通", _traffic_detail(start_city, "成都方向班车/包车到四姑娘山镇，山区车程受天气影响明显"))],
                [_item("餐饮节点", "镇上餐饮选择更多，沟内行程建议自备午餐和热饮。")],
                [
                    _item("高海拔补给", "提前准备防寒层、补水、电解质和高海拔不适的下撤预案。"),
                    _item("门票与开放", "核对景区开放沟线、入沟时间、观光车和户外活动备案要求。"),
                ],
            )

        if "黄山" in destination:
            return (
                [_item("住宿重点", "山上酒店和山下汤口镇是两类完全不同方案，需先确定是否看日出。")],
                [_item("外部交通", _traffic_detail(start_city, "黄山北站/汤口换乘中心，再转景区交通车"))],
                [_item("餐饮节点", "山上餐饮价格较高，建议核对酒店晚餐、早餐和自带路餐策略。")],
                [_item("补给提醒", "确认索道运营、景区交通车时间、饮水点和临时封闭路段。")],
            )

        return (
            [_item("住宿待查", "围绕 KML 起点、终点、计划下撤口搜索酒店、客栈、营地或村镇住宿。")],
            [_item("交通待查", _traffic_detail(start_city, "最近高铁站/汽车站/停车场，再确认到登山口的最后一段接驳"))],
            [_item("餐饮待查", "核对起点村镇、住宿点和终点附近是否有晚餐、早餐和热水。")],
            [_item("补给待查", "标记便利店、药店、卫生院、救援点、水源和可通车下撤点。")],
        )


class AmapPOIResearchProvider:
    def __init__(self, api_key: str, timeout_s: float = 8.0, radius_m: int = 5000) -> None:
        self.api_key = api_key
        self.timeout_s = timeout_s
        self.radius_m = radius_m

    def collect(
        self,
        request: HikingGuideRequest,
        candidates: list[RouteCandidate],
        weather_snapshots: list[WeatherSnapshot | None],
        include_lodging: bool = True,
        include_food: bool = True,
        include_supply: bool = True,
    ) -> TravelResearch:
        if not candidates:
            return TravelResearch()

        route = candidates[0].route
        # 路线采样点：起点 + 25%/50%/75% + 终点
        sample_points = self._sample_route_points(route)

        all_lodging: list[TravelInfoItem] = []
        all_transport: list[TravelInfoItem] = []
        all_food: list[TravelInfoItem] = []
        all_supply: list[TravelInfoItem] = []

        for point in sample_points:
            if include_lodging:
                all_lodging.extend(self._query(point, "酒店|客栈|民宿|帐篷营地", "住宿", radius_m=10000))
            all_transport.extend(self._query(point, "停车场|公交站|汽车站|火车站", "交通", radius_m=5000))
            if include_food:
                all_food.extend(self._query(point, "餐馆|饭店|小吃|农家乐", "餐饮", radius_m=3000))
            if include_supply:
                all_supply.extend(self._query(point, "便利店|超市|药店|卫生院", "补给", radius_m=5000))

        return TravelResearch(
            lodging=self._dedupe_items(all_lodging)[:10],
            transport=self._dedupe_items(all_transport)[:8],
            food=self._dedupe_items(all_food)[:10],
            supply=self._dedupe_items(all_supply)[:8],
            next_steps=["高德 POI 结果基于路线采样点搜索，仍需按实际起终点和下撤点复核。"],
        )

    def _sample_route_points(self, route: RouteGeometry) -> list[Coordinate]:
        """在路线上采样关键点：起点、25%、50%、75%、终点。"""
        coords = route.coordinates
        total = len(coords)
        if total <= 2:
            return [coords[0], coords[-1]]
        indices = {0, total - 1}
        for pct in (0.25, 0.5, 0.75):
            indices.add(min(int(total * pct), total - 1))
        return [coords[i] for i in sorted(indices)]

    def _dedupe_items(self, items: list[TravelInfoItem]) -> list[TravelInfoItem]:
        """按 title 去重 POI 结果。"""
        seen: set[str] = set()
        result: list[TravelInfoItem] = []
        for item in items:
            if item.title not in seen:
                seen.add(item.title)
                result.append(item)
        return result

    def _query(self, center: Coordinate, keywords: str, label: str, radius_m: int | None = None) -> list[TravelInfoItem]:
        effective_radius = radius_m if radius_m is not None else self.radius_m
        amap_center = wgs84_to_gcj02(center)
        response = httpx.get(
            "https://restapi.amap.com/v3/place/around",
            params={
                "key": self.api_key,
                "location": f"{amap_center.lon},{amap_center.lat}",
                "keywords": keywords,
                "radius": effective_radius,
                "offset": 5,
                "page": 1,
                "extensions": "base",
            },
            timeout=self.timeout_s,
        )
        response.raise_for_status()
        payload = response.json()
        pois = payload.get("pois") or []
        items: list[TravelInfoItem] = []
        for poi in pois[:5]:
            title = str(poi.get("name") or "").strip()
            address = str(poi.get("address") or "").strip()
            distance_m = _float_or_none(poi.get("distance"))
            coordinate = _coordinate_from_location(poi.get("location"))
            if not title:
                continue
            detail = address or f"轨迹起点附近{label} POI"
            if distance_m is not None:
                detail = f"{detail}，距轨迹起点约 {round(distance_m / 1000, 2)} km"
            items.append(
                TravelInfoItem(
                    title=title,
                    detail=detail,
                    source="amap-poi",
                    confidence=0.72,
                    coordinate=coordinate,
                    distance_km=round(distance_m / 1000, 2) if distance_m is not None else None,
                    metadata={
                        "type": poi.get("type"),
                        "tel": poi.get("tel"),
                        "address": address,
                        "coord_system": "amap_gcj02" if coordinate else None,
                    },
                )
            )
        return items


def _weather_items(weather_snapshots: Iterable[WeatherSnapshot | None]) -> list[TravelInfoItem]:
    items: list[TravelInfoItem] = []
    seen: set[tuple[object, ...]] = set()
    for snapshot in weather_snapshots:
        if not snapshot:
            continue
        key = (
            snapshot.source,
            snapshot.weather_text,
            snapshot.min_temp_c,
            snapshot.max_temp_c,
            snapshot.precipitation_probability,
            snapshot.precipitation_mm,
            snapshot.max_wind_kmh,
            snapshot.wind_gust_kmh,
        )
        if key in seen:
            continue
        seen.add(key)
        parts = []
        if snapshot.weather_text:
            parts.append(snapshot.weather_text)
        if snapshot.min_temp_c is not None or snapshot.max_temp_c is not None:
            min_temp = snapshot.min_temp_c if snapshot.min_temp_c is not None else "未知"
            max_temp = snapshot.max_temp_c if snapshot.max_temp_c is not None else "未知"
            parts.append(f"气温 {min_temp}-{max_temp} C")
        if snapshot.humidity_percent is not None:
            parts.append(f"湿度 {snapshot.humidity_percent}%")
        if snapshot.precipitation_probability is not None:
            parts.append(f"降水概率 {snapshot.precipitation_probability}%")
        if snapshot.precipitation_mm is not None:
            parts.append(f"降水量 {snapshot.precipitation_mm} mm")
        if snapshot.max_wind_kmh is not None:
            parts.append(f"最大风速 {snapshot.max_wind_kmh} km/h")
        if snapshot.wind_gust_kmh is not None:
            parts.append(f"阵风 {snapshot.wind_gust_kmh} km/h")
        if snapshot.uv_index_max is not None:
            parts.append(f"UV {snapshot.uv_index_max}")
        items.append(
            TravelInfoItem(
                title="路线起点天气",
                detail="，".join(parts) if parts else "已获取天气快照，但缺少关键日值字段。",
                source=snapshot.source,
                confidence=0.78,
                metadata={
                    "weather_text": snapshot.weather_text,
                    "humidity_percent": snapshot.humidity_percent,
                    "precipitation_probability": snapshot.precipitation_probability,
                    "precipitation_mm": snapshot.precipitation_mm,
                    "max_wind_kmh": snapshot.max_wind_kmh,
                    "wind_gust_kmh": snapshot.wind_gust_kmh,
                    "wind_direction": snapshot.wind_direction,
                    "uv_index_max": snapshot.uv_index_max,
                    "hiking_risk_notes": list(snapshot.hiking_risk_notes),
                },
            )
        )
    return items


def _item(title: str, detail: str) -> TravelInfoItem:
    return TravelInfoItem(title=title, detail=detail, source="fallback", confidence=0.45)


def _traffic_detail(start_city: str | None, final_leg: str) -> str:
    if start_city:
        return f"建议从{start_city}出发先查铁路/长途客运到达节点：{final_leg}。"
    return f"建议先补充出发城市，再查询铁路/长途客运到达节点：{final_leg}。"


def _float_or_none(value: object) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coordinate_from_location(value: object) -> Coordinate | None:
    if not isinstance(value, str) or "," not in value:
        return None
    lon_s, lat_s = value.split(",", 1)
    try:
        return Coordinate(lon=float(lon_s), lat=float(lat_s))
    except ValueError:
        return None


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result
