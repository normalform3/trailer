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


class RoughTransportProvider:
    """粗粒度出行方案：只罗列可选方式，不替用户做精确导航。"""

    def __init__(self, flight_provider: FlightPriceProvider | None = None) -> None:
        self.flight_provider = flight_provider or AmadeusFlightPriceProvider()

    def plan(
        self,
        start_city: str,
        destination_coordinate: Coordinate,
        destination_name: str,
        departure_date: date | None = None,
    ) -> TransportPlan:
        options = [
            self._driving_option(destination_name),
            self._rail_option(start_city, destination_name),
            self._charter_option(destination_name),
        ]
        warnings: list[str] = []

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
                    booking_hint="需要配置 AMADEUS_CLIENT_ID / AMADEUS_CLIENT_SECRET，并补充可映射机场与出行日期",
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


class AmadeusFlightPriceProvider:
    token_url = "https://test.api.amadeus.com/v1/security/oauth2/token"
    offers_url = "https://test.api.amadeus.com/v2/shopping/flight-offers"

    def __init__(
        self,
        client_id: str | None = None,
        client_secret: str | None = None,
        timeout_s: float = 10.0,
    ) -> None:
        settings = get_settings()
        self.client_id = client_id if client_id is not None else settings.api_keys.amadeus_client_id
        self.client_secret = client_secret if client_secret is not None else settings.api_keys.amadeus_client_secret
        self.timeout_s = timeout_s

    def cheapest_offer(
        self,
        origin_city: str,
        destination_name: str,
        departure_date: date | None,
    ) -> TransportOption:
        if not self.client_id or not self.client_secret:
            raise RuntimeError("Amadeus credentials are not configured")
        if departure_date is None:
            raise RuntimeError("出行日期缺失，无法查询机票报价")

        origin = _city_to_iata(origin_city)
        destination = _destination_to_iata(destination_name)
        if not origin or not destination:
            raise RuntimeError("出发地或目的地无法映射到机场代码")

        token = self._access_token()
        response = httpx.get(
            self.offers_url,
            headers={"Authorization": f"Bearer {token}"},
            params={
                "originLocationCode": origin,
                "destinationLocationCode": destination,
                "departureDate": departure_date.isoformat(),
                "adults": 1,
                "currencyCode": "CNY",
                "max": 5,
            },
            timeout=self.timeout_s,
        )
        response.raise_for_status()
        offers = response.json().get("data") or []
        if not offers:
            raise RuntimeError("Amadeus did not return flight offers")

        cheapest = min(offers, key=lambda item: _float_or_none((item.get("price") or {}).get("grandTotal")) or float("inf"))
        price = cheapest.get("price") or {}
        amount = price.get("grandTotal")
        currency = price.get("currency") or "CNY"
        segments = _flight_segments(cheapest)
        return TransportOption(
            mode="flight",
            price_estimate=f"{amount} {currency}" if amount else None,
            cost_estimate=f"{amount} {currency}" if amount else None,
            steps=segments or [f"{origin} → {destination}，再转地面交通到登山口"],
            tip="机票价格波动较大，需以航司或出票平台实时结果为准",
            booking_hint="核对行李额、退改签、到达机场到登山口的接驳时间",
            requires_user_verification=True,
            source="amadeus-flight-offers",
        )

    def _access_token(self) -> str:
        response = httpx.post(
            self.token_url,
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            timeout=self.timeout_s,
        )
        response.raise_for_status()
        token = response.json().get("access_token")
        if not token:
            raise RuntimeError("Amadeus token response did not include access_token")
        return str(token)


# Backward-compatible aliases for existing imports/tests.
class AmapTransportProvider(RoughTransportProvider):
    pass


class StaticTransportProvider(RoughTransportProvider):
    def __init__(self) -> None:
        super().__init__(flight_provider=UnavailableFlightPriceProvider())


class UnavailableFlightPriceProvider:
    def cheapest_offer(
        self,
        origin_city: str,
        destination_name: str,
        departure_date: date | None,
    ) -> TransportOption:
        raise RuntimeError("flight price provider is not configured")


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


def _flight_segments(offer: dict[str, object]) -> list[str]:
    segments: list[str] = []
    itineraries = offer.get("itineraries") if isinstance(offer, dict) else None
    if not isinstance(itineraries, list):
        return segments
    for itinerary in itineraries[:1]:
        for segment in (itinerary.get("segments") or [])[:3]:
            dep = (segment.get("departure") or {}).get("iataCode", "")
            arr = (segment.get("arrival") or {}).get("iataCode", "")
            carrier = segment.get("carrierCode", "")
            number = segment.get("number", "")
            if dep and arr:
                flight = f"{carrier}{number}" if carrier or number else "航班"
                segments.append(f"{flight}: {dep} → {arr}")
    return segments


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
