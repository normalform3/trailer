from __future__ import annotations

from typing import Protocol

import httpx

from app.config import get_settings
from app.models import Coordinate, TransportOption, TransportPlan
from app.providers.places import AmapPlaceProvider
from app.services.coordinates import wgs84_to_gcj02


class TransportPlanningProvider(Protocol):
    def plan(
        self,
        start_city: str,
        destination_coordinate: Coordinate,
        destination_name: str,
    ) -> TransportPlan:
        raise NotImplementedError


class AmapTransportProvider:
    """使用高德地图驾车 + 公交综合路线 API 规划交通方案。"""

    def __init__(self, api_key: str | None = None, timeout_s: float = 10.0) -> None:
        settings = get_settings()
        self.api_key = api_key or settings.api_keys.amap_api_key
        self.timeout_s = timeout_s
        self.place_provider = AmapPlaceProvider(self.api_key, timeout_s)

    def plan(
        self,
        start_city: str,
        destination_coordinate: Coordinate,
        destination_name: str,
    ) -> TransportPlan:
        if not self.api_key:
            raise RuntimeError("AMAP_API_KEY is not configured")

        # 1. geocode 出发城市获取坐标
        start_place = self.place_provider.resolve(start_city)
        if not start_place or not start_place.coordinate:
            return self._fallback_plan(start_city, destination_coordinate, destination_name)

        start_gcj02 = wgs84_to_gcj02(start_place.coordinate)
        dest_gcj02 = wgs84_to_gcj02(destination_coordinate)

        # 2. 判断是否同城
        is_same = self._is_same_region(start_gcj02, dest_gcj02)

        options: list[TransportOption] = []
        warnings: list[str] = []

        # 3. 驾车方案
        try:
            driving = self._query_driving(start_gcj02, dest_gcj02)
            if driving:
                options.append(driving)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"驾车路线查询暂不可用：{exc}")

        # 4. 公交方案
        try:
            transit = self._query_transit(start_gcj02, dest_gcj02)
            if transit:
                options.append(transit)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"公交路线查询暂不可用：{exc}")

        if not options:
            return self._fallback_plan(start_city, destination_coordinate, destination_name, extra_warnings=warnings)

        return TransportPlan(
            start_city=start_city,
            start_coordinate=start_place.coordinate,
            destination_name=destination_name,
            destination_coordinate=destination_coordinate,
            options=options,
            warnings=warnings,
            is_same_region=is_same,
        )

    def _query_driving(self, origin: Coordinate, destination: Coordinate) -> TransportOption | None:
        """调用高德驾车路线规划 API。"""
        response = httpx.get(
            "https://restapi.amap.com/v3/direction/driving",
            params={
                "key": self.api_key,
                "origin": f"{origin.lon},{origin.lat}",
                "destination": f"{destination.lon},{destination.lat}",
                "strategy": 0,  # 速度优先
                "extensions": "base",
            },
            timeout=self.timeout_s,
        )
        response.raise_for_status()
        payload = response.json()
        self._raise_for_amap_error(payload, "driving")

        paths = (payload.get("route") or {}).get("paths") or []
        if not paths:
            return None

        path = paths[0]
        distance_m = _float_or_none(path.get("distance"))
        duration_s = _float_or_none(path.get("duration"))

        # 提取关键步骤（合并短步骤，取最多 5 条）
        raw_steps = path.get("steps") or []
        steps: list[str] = []
        for step in raw_steps:
            instruction = str(step.get("instruction") or "").strip()
            if instruction:
                steps.append(instruction)
        steps = steps[:5]

        distance_km = round(distance_m / 1000, 1) if distance_m is not None else None
        duration_hours = round(duration_s / 3600, 1) if duration_s is not None else None

        # 费用粗算
        cost_estimate = None
        if distance_km is not None:
            cost_estimate = f"约 {round(distance_km * 0.5)} 元（油费粗算）"

        return TransportOption(
            mode="driving",
            duration_hours=duration_hours,
            distance_km=distance_km,
            cost_estimate=cost_estimate,
            steps=steps,
            tip="自驾请注意山区路况和停车位置",
            source="amap-driving",
        )

    def _query_transit(self, origin: Coordinate, destination: Coordinate) -> TransportOption | None:
        """调用高德公交路线规划 API。"""
        # 获取出发和目的地城市的 adcode
        origin_adcode = self._adcode_for_coordinate(origin)
        dest_adcode = self._adcode_for_coordinate(destination)

        response = httpx.get(
            "https://restapi.amap.com/v3/direction/transit/integrated",
            params={
                "key": self.api_key,
                "origin": f"{origin.lon},{origin.lat}",
                "destination": f"{destination.lon},{destination.lat}",
                "city": origin_adcode,
                "cityd": dest_adcode,
                "strategy": 0,  # 最快捷
            },
            timeout=self.timeout_s,
        )
        response.raise_for_status()
        payload = response.json()
        self._raise_for_amap_error(payload, "transit")

        transits = (payload.get("route") or {}).get("transits") or []
        if not transits:
            return None

        best = transits[0]
        distance_m = _float_or_none(best.get("distance"))
        duration_s = _float_or_none(best.get("duration"))
        cost_str = best.get("cost")

        # 提取公交段描述
        steps: list[str] = []
        segments = best.get("segments") or []
        for seg in segments[:6]:
            bus_info = seg.get("bus") or {}
            bus_lines = bus_info.get("buslines") or []
            if bus_lines:
                line = bus_lines[0]
                name = str(line.get("name") or "")
                dep = (line.get("departure_stop") or {}).get("name", "")
                arr = (line.get("arrival_stop") or {}).get("name", "")
                if name:
                    steps.append(f"{name}：{dep} → {arr}")
            else:
                walking = seg.get("walking") or {}
                walk_dist = _float_or_none(walking.get("distance"))
                if walk_dist is not None and walk_dist > 0:
                    steps.append(f"步行约 {round(walk_dist / 1000, 1)} km")

        distance_km = round(distance_m / 1000, 1) if distance_m is not None else None
        duration_hours = round(duration_s / 3600, 1) if duration_s is not None else None
        cost_estimate = f"约 {cost_str} 元" if cost_str else None

        return TransportOption(
            mode="transit",
            duration_hours=duration_hours,
            distance_km=distance_km,
            cost_estimate=cost_estimate,
            steps=steps,
            tip="请提前查询末班车时间和余票",
            source="amap-transit",
        )

    def _adcode_for_coordinate(self, coordinate: Coordinate) -> str:
        """通过逆地理编码获取 adcode。"""
        response = httpx.get(
            "https://restapi.amap.com/v3/geocode/regeo",
            params={
                "key": self.api_key,
                "location": f"{coordinate.lon},{coordinate.lat}",
                "extensions": "base",
                "output": "JSON",
            },
            timeout=self.timeout_s,
        )
        response.raise_for_status()
        payload = response.json()
        self._raise_for_amap_error(payload, "regeo-transport")
        address_component = (payload.get("regeocode") or {}).get("addressComponent") or {}
        adcode = str(address_component.get("adcode") or "").strip()
        if not adcode:
            raise RuntimeError("Amap reverse geocode did not include adcode")
        return adcode

    def _is_same_region(self, origin_gcj02: Coordinate, dest_gcj02: Coordinate) -> bool:
        """判断出发地和目的地是否同城（比较省份代码）。"""
        try:
            origin_adcode = self._adcode_for_coordinate(origin_gcj02)
            dest_adcode = self._adcode_for_coordinate(dest_gcj02)
            # 省份代码为前 2 位
            return origin_adcode[:2] == dest_adcode[:2]
        except Exception:  # noqa: BLE001
            return False

    def _fallback_plan(
        self,
        start_city: str,
        destination_coordinate: Coordinate,
        destination_name: str,
        extra_warnings: list[str] | None = None,
    ) -> TransportPlan:
        """当 API 不可用时的降级方案。"""
        return TransportPlan(
            start_city=start_city,
            destination_coordinate=destination_coordinate,
            destination_name=destination_name,
            options=[
                TransportOption(
                    mode="mixed",
                    steps=[
                        f"从{start_city}出发，建议查询铁路/长途客运到达目的地附近站点",
                        f"再转当地接驳到达路线起点（{destination_name}）",
                        "出发前确认末班车时间、票价和余票",
                    ],
                    tip="交通规划需自行查询，本结果为通用建议",
                    source="static",
                )
            ],
            warnings=_dedupe([
                "交通规划服务暂不可用，请自行查询具体班次和路线",
                *(extra_warnings or []),
            ]),
            is_same_region=False,
        )

    def _raise_for_amap_error(self, payload: dict[str, object], operation: str) -> None:
        if str(payload.get("status")) == "1":
            return
        info = payload.get("info") or "Amap request failed"
        infocode = payload.get("infocode") or "unknown"
        raise RuntimeError(f"Amap {operation} failed ({infocode}): {info}")


class StaticTransportProvider:
    """静态降级交通规划，返回通用建议模板。"""

    def plan(
        self,
        start_city: str,
        destination_coordinate: Coordinate,
        destination_name: str,
    ) -> TransportPlan:
        return TransportPlan(
            start_city=start_city,
            destination_coordinate=destination_coordinate,
            destination_name=destination_name,
            options=[
                TransportOption(
                    mode="mixed",
                    steps=[
                        f"从{start_city}出发，建议查询铁路/长途客运到达目的地附近站点",
                        f"再转当地接驳到达路线起点（{destination_name}）",
                        "出发前确认末班车时间、票价和余票",
                    ],
                    tip="交通规划需自行查询，本结果为通用建议",
                    source="static",
                )
            ],
            warnings=["交通规划服务暂不可用，请自行查询具体班次和路线"],
            is_same_region=False,
        )


def _float_or_none(value: object) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result
