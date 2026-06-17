from __future__ import annotations

from datetime import date
from typing import Protocol

import httpx

from app.config import get_settings
from app.models import Coordinate, TransportOption, TransportPlan


class TransportPlanningProvider(Protocol):
    def plan(
        self,
        start_city: str,
        destination_coordinate: Coordinate,
        destination_name: str,
        departure_date: date | None = None,
    ) -> TransportPlan:
        raise NotImplementedError


class FlightPriceProvider(Protocol):
    def cheapest_offer(
        self,
        origin_city: str,
        destination_name: str,
        departure_date: date | None,
    ) -> TransportOption:
        raise NotImplementedError


class DrivingDurationProvider(Protocol):
    def estimate(
        self,
        start_city: str,
        destination_coordinate: Coordinate,
    ) -> TransportOption:
        raise NotImplementedError


class RoughTransportProvider:
    """粗粒度出行方案：只罗列可选方式，不替用户做精确导航。"""

    def __init__(
        self,
        flight_provider: FlightPriceProvider | None = None,
        driving_provider: DrivingDurationProvider | None = None,
    ) -> None:
        self.flight_provider = flight_provider or SerpApiFlightSearchProvider()
        settings = get_settings()
        self.driving_provider = (
            driving_provider
            if driving_provider is not None
            else AmapDrivingDurationProvider(settings.api_keys.amap_api_key)
        )

    def plan(
        self,
        start_city: str,
        destination_coordinate: Coordinate,
        destination_name: str,
        departure_date: date | None = None,
    ) -> TransportPlan:
        warnings: list[str] = []
        try:
            driving_option = self.driving_provider.estimate(start_city, destination_coordinate)
            driving_option.steps.insert(0, f"自驾到 {destination_name} 附近停车点或登山口")
        except Exception as exc:  # noqa: BLE001 - driving estimate is optional.
            driving_option = self._driving_option(destination_name)
            warnings.append(f"高德自驾耗时暂不可用：{exc}")

        options = [
            driving_option,
            self._rail_option(start_city, destination_name),
            self._charter_option(destination_name),
        ]

        try:
            options.insert(1, self.flight_provider.cheapest_offer(start_city, destination_name, departure_date))
        except Exception as exc:  # noqa: BLE001 - ticket providers are optional.
            options.insert(
                1,
                TransportOption(
                    mode="flight",
                    steps=[
                        f"查询 {start_city} 附近机场至 {destination_name} 周边机场的机票",
                        "再接高铁、汽车或包车前往登山口",
                    ],
                    tip="机票报价暂不可用，请在配置票价 API 后重试或自行核对航司/OTA",
                    booking_hint="需要配置 SERPAPI_API_KEY，并补充可映射机场与出行日期",
                    requires_user_verification=True,
                    source="flight-placeholder",
                ),
            )
            warnings.append(f"机票报价查询暂不可用：{exc}")

        return TransportPlan(
            start_city=start_city,
            destination_coordinate=destination_coordinate,
            destination_name=destination_name,
            options=options,
            warnings=_dedupe(warnings),
            is_same_region=False,
        )

    def _driving_option(self, destination_name: str) -> TransportOption:
        return TransportOption(
            mode="driving",
            steps=[
                f"自驾到 {destination_name} 附近镇区或景区停车点",
                "出发前自行使用导航确认实时路况、停车场开放和夜间山路限制",
                "把最后一段接驳、返程取车和备用下撤点提前写入行程",
            ],
            tip="自驾不在本系统内做精确路线规划，以用户实时导航为准",
            booking_hint="自行核对停车点、山路限行、景区换乘和返程取车",
            requires_user_verification=True,
            source="rough-driving",
        )

    def _rail_option(self, start_city: str, destination_name: str) -> TransportOption:
        return TransportOption(
            mode="rail",
            steps=[
                f"从 {start_city} 查询高铁/火车至 {destination_name} 周边高铁站或地级市",
                "再转当地客运、景区交通、网约车或包车到登山口",
                "优先核对到达后是否还能赶上末班接驳",
            ],
            tip="火车票价暂不接非公开 12306 接口，请以铁路 12306 为准",
            booking_hint="铁路 12306 / 官方售票渠道核对车次、票价、余票和改签规则",
            requires_user_verification=True,
            source="rail-placeholder",
        )

    def _charter_option(self, destination_name: str) -> TransportOption:
        return TransportOption(
            mode="charter",
            steps=[
                f"到达 {destination_name} 周边城镇后，联系民宿、客栈或当地司机确认接驳",
                "提前约定上山口、下山口、等待时间、夜间加价和取消规则",
            ],
            tip="山区最后一段交通波动大，建议保留司机电话和备选下撤接驳",
            booking_hint="通过住宿方、当地客运站或正规平台核实",
            requires_user_verification=True,
            source="rough-charter",
        )


class SerpApiFlightSearchProvider:
    search_url = "https://serpapi.com/search"

    def __init__(
        self,
        api_key: str | None = None,
        timeout_s: float = 10.0,
    ) -> None:
        settings = get_settings()
        self.api_key = api_key if api_key is not None else settings.api_keys.serpapi_api_key
        self.timeout_s = timeout_s

    def cheapest_offer(
        self,
        origin_city: str,
        destination_name: str,
        departure_date: date | None,
    ) -> TransportOption:
        if not self.api_key:
            raise RuntimeError("SERPAPI_API_KEY is not configured")
        if departure_date is None:
            raise RuntimeError("出行日期缺失，无法查询机票报价")

        origin = _city_to_iata(origin_city)
        destination = _destination_to_iata(destination_name)
        if not origin or not destination:
            raise RuntimeError("出发地或目的地无法映射到机场代码")

        response = httpx.get(
            self.search_url,
            params={
                "engine": "google_flights",
                "api_key": self.api_key,
                "departure_id": origin,
                "arrival_id": destination,
                "outbound_date": departure_date.isoformat(),
                "type": "2",
                "currency": "CNY",
                "hl": "zh-cn",
                "gl": "cn",
            },
            timeout=self.timeout_s,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("error"):
            raise RuntimeError(f"SerpApi flight search failed: {payload['error']}")
        offers = (payload.get("best_flights") or []) + (payload.get("other_flights") or [])
        if not offers:
            raise RuntimeError("SerpApi did not return flight offers")

        cheapest = min(
            offers,
            key=lambda item: price if (price := _price_value(item.get("price"))) is not None else float("inf"),
        )
        amount = cheapest.get("price")
        duration_minutes = _float_or_none(cheapest.get("total_duration"))
        duration_hours = round(duration_minutes / 60, 1) if duration_minutes is not None else None
        segments = _serpapi_flight_steps(cheapest)
        if duration_minutes is not None:
            segments.insert(0, f"Google Flights 估算总耗时约 {_format_minutes(duration_minutes)}")
        return TransportOption(
            mode="flight",
            price_estimate=_format_price(amount, "CNY"),
            cost_estimate=_format_price(amount, "CNY"),
            duration_hours=duration_hours,
            steps=segments or [f"{origin} → {destination}，再转地面交通到登山口"],
            tip="机票价格来自 Google Flights 搜索结果，波动较大，需以航司或出票平台实时结果为准",
            booking_hint="核对行李额、退改签、到达机场到登山口的接驳时间",
            requires_user_verification=True,
            source="serpapi-google-flights",
        )


class AmapDrivingDurationProvider:
    geocode_url = "https://restapi.amap.com/v3/geocode/geo"
    driving_url = "https://restapi.amap.com/v3/direction/driving"

    def __init__(self, api_key: str | None = None, timeout_s: float = 8.0) -> None:
        self.api_key = api_key
        self.timeout_s = timeout_s

    def estimate(
        self,
        start_city: str,
        destination_coordinate: Coordinate,
    ) -> TransportOption:
        if not self.api_key:
            raise RuntimeError("AMAP_API_KEY is not configured")
        origin = self._geocode(start_city)
        destination = f"{destination_coordinate.lon},{destination_coordinate.lat}"
        response = httpx.get(
            self.driving_url,
            params={
                "key": self.api_key,
                "origin": origin,
                "destination": destination,
                "strategy": 10,
            },
            timeout=self.timeout_s,
        )
        response.raise_for_status()
        payload = response.json()
        route = payload.get("route") or {}
        paths = route.get("paths") or []
        if not paths:
            raise RuntimeError("Amap did not return driving paths")
        path = paths[0]
        duration_hours = _float_or_none(path.get("duration"))
        distance_km = _float_or_none(path.get("distance"))
        duration_hours = round(duration_hours / 3600, 1) if duration_hours is not None else None
        distance_km = round(distance_km / 1000, 1) if distance_km is not None else None
        summary = []
        if duration_hours is not None:
            summary.append(f"高德估算驾车约 {duration_hours:.1f} 小时")
        if distance_km is not None:
            summary.append(f"约 {distance_km:.0f} km")
        return TransportOption(
            mode="driving",
            duration_hours=duration_hours,
            distance_km=distance_km,
            steps=[
                "；".join(summary) if summary else "高德已返回自驾估算",
                "出发前仍需用导航确认实时路况、停车场开放和夜间山路限制",
                "把最后一段接驳、返程取车和备用下撤点提前写入行程",
            ],
            tip="自驾耗时为高德接口粗略估算，不替代出发当天实时导航",
            booking_hint="核对停车点、山路限行、景区换乘和返程取车",
            requires_user_verification=True,
            source="amap-driving",
        )

    def _geocode(self, start_city: str) -> str:
        response = httpx.get(
            self.geocode_url,
            params={"key": self.api_key, "address": start_city},
            timeout=self.timeout_s,
        )
        response.raise_for_status()
        geocodes = response.json().get("geocodes") or []
        if not geocodes or not geocodes[0].get("location"):
            raise RuntimeError("Amap did not geocode start city")
        return str(geocodes[0]["location"])


# Backward-compatible aliases for existing imports/tests.
class AmapTransportProvider(RoughTransportProvider):
    pass


class StaticTransportProvider(RoughTransportProvider):
    def __init__(self) -> None:
        super().__init__(
            flight_provider=UnavailableFlightPriceProvider(),
            driving_provider=UnavailableDrivingDurationProvider(),
        )


class UnavailableFlightPriceProvider:
    def cheapest_offer(
        self,
        origin_city: str,
        destination_name: str,
        departure_date: date | None,
    ) -> TransportOption:
        raise RuntimeError("flight price provider is not configured")


class UnavailableDrivingDurationProvider:
    def estimate(
        self,
        start_city: str,
        destination_coordinate: Coordinate,
    ) -> TransportOption:
        raise RuntimeError("driving duration provider is not configured")


def _city_to_iata(value: str) -> str | None:
    text = value.strip().lower()
    mapping = {
        "北京": "BJS",
        "上海": "SHA",
        "广州": "CAN",
        "深圳": "SZX",
        "成都": "CTU",
        "杭州": "HGH",
        "南京": "NKG",
        "武汉": "WUH",
        "西安": "SIA",
        "重庆": "CKG",
    }
    for keyword, code in mapping.items():
        if keyword.lower() in text:
            return code
    if len(value.strip()) == 3 and value.isalpha():
        return value.upper()
    return None


def _destination_to_iata(value: str) -> str | None:
    mapping = {
        "武功山": "KHN",
        "黄山": "TXN",
        "四姑娘山": "CTU",
        "峨眉山": "CTU",
        "张家界": "DYG",
    }
    for keyword, code in mapping.items():
        if keyword in value:
            return code
    return _city_to_iata(value)


def _serpapi_flight_steps(offer: dict[str, object]) -> list[str]:
    segments: list[str] = []
    flights = offer.get("flights") if isinstance(offer, dict) else None
    if not isinstance(flights, list):
        return segments
    for segment in flights[:3]:
        if not isinstance(segment, dict):
            continue
        departure = segment.get("departure_airport") or {}
        arrival = segment.get("arrival_airport") or {}
        if not isinstance(departure, dict) or not isinstance(arrival, dict):
            continue
        dep = departure.get("id", "")
        arr = arrival.get("id", "")
        dep_time = departure.get("time")
        arr_time = arrival.get("time")
        airline = segment.get("airline", "")
        flight_number = segment.get("flight_number", "")
        label = str(flight_number or airline or "航班")
        if airline and flight_number and str(airline) not in label:
            label = f"{airline} {label}"
        if dep and arr:
            time_hint = f"（{dep_time} → {arr_time}）" if dep_time and arr_time else ""
            segments.append(f"{label}: {dep} → {arr}{time_hint}")
    layovers = offer.get("layovers") if isinstance(offer, dict) else None
    if isinstance(layovers, list) and layovers:
        stops = []
        for layover in layovers[:2]:
            if isinstance(layover, dict) and layover.get("id"):
                stops.append(str(layover["id"]))
        if stops:
            segments.append(f"中转：{'、'.join(stops)}")
    return segments


def _price_value(value: object) -> float | None:
    if isinstance(value, str):
        normalized = "".join(ch for ch in value if ch.isdigit() or ch == ".")
        return _float_or_none(normalized)
    return _float_or_none(value)


def _format_price(value: object, currency: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, int):
        return f"{value} {currency}"
    if isinstance(value, float):
        return f"{value:.0f} {currency}" if value.is_integer() else f"{value:.2f} {currency}"
    text = str(value).strip()
    if not text:
        return None
    if any(token in text.upper() for token in (currency, "USD", "CNY")) or any(symbol in text for symbol in ("¥", "￥", "$")):
        return text
    return f"{text} {currency}"


def _format_minutes(value: float) -> str:
    minutes = int(round(value))
    hours, remaining_minutes = divmod(minutes, 60)
    if hours and remaining_minutes:
        return f"{hours} 小时 {remaining_minutes} 分钟"
    if hours:
        return f"{hours} 小时"
    return f"{remaining_minutes} 分钟"


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
