from __future__ import annotations

import json
import re
from datetime import date
from typing import Iterator

import httpx

from app.models import TransportOption


class JuheMcpClient:
    """Small MCP client restricted to the two read-only ticket tools."""

    base_url = "https://mcp.juhe.cn/sse"
    allowed_tools = frozenset({"query_train_tickets", "get_flight_info"})

    def __init__(self, token: str, timeout_s: float = 15.0) -> None:
        if not token.strip():
            raise ValueError("JUHE_MCP_TOKEN is empty")
        self._token = token.strip()
        self.timeout_s = timeout_s

    def call_tool(self, name: str, arguments: dict[str, object]) -> object:
        if name not in self.allowed_tools:
            raise ValueError(f"MCP tool is not allowed: {name}")

        url = httpx.URL(self.base_url).copy_add_param("token", self._token)
        timeout = httpx.Timeout(self.timeout_s, connect=min(self.timeout_s, 8.0))
        try:
            with httpx.Client(timeout=timeout, follow_redirects=True) as client:
                with client.stream("GET", url, headers={"Accept": "text/event-stream"}) as response:
                    response.raise_for_status()
                    lines = response.iter_lines()
                    endpoint = self._next_sse_data(lines)
                    messages_url = response.url.join(endpoint)

                    self._post(
                        client,
                        messages_url,
                        {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "initialize",
                            "params": {
                                "protocolVersion": "2024-11-05",
                                "capabilities": {},
                                "clientInfo": {"name": "trailer", "version": "0.1.0"},
                            },
                        },
                    )
                    self._next_rpc_result(lines, 1)
                    self._post(
                        client,
                        messages_url,
                        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
                    )
                    self._post(
                        client,
                        messages_url,
                        {
                            "jsonrpc": "2.0",
                            "id": 2,
                            "method": "tools/call",
                            "params": {"name": name, "arguments": arguments},
                        },
                    )
                    result = self._next_rpc_result(lines, 2)
        except httpx.HTTPError as exc:
            # Do not include the request URL: it contains the MCP token.
            raise RuntimeError(f"聚合 MCP 网络请求失败（{type(exc).__name__}）") from exc

        if isinstance(result, dict) and result.get("isError"):
            raise RuntimeError("聚合 MCP 查询返回错误")
        return _tool_payload(result)

    @staticmethod
    def _post(client: httpx.Client, url: httpx.URL, payload: dict[str, object]) -> None:
        response = client.post(url, json=payload)
        response.raise_for_status()

    @staticmethod
    def _next_sse_data(lines: Iterator[str]) -> str:
        for line in lines:
            if line.startswith("data:"):
                value = line[5:].strip()
                if value:
                    return value
        raise RuntimeError("聚合 MCP SSE 连接意外关闭")

    @classmethod
    def _next_rpc_result(cls, lines: Iterator[str], request_id: int) -> object:
        while True:
            raw = cls._next_sse_data(lines)
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(message, dict) or message.get("id") != request_id:
                continue
            if "error" in message:
                raise RuntimeError("聚合 MCP 协议请求失败")
            return message.get("result")


class JuheMcpTicketProvider:
    def __init__(self, token: str | None = None, client: JuheMcpClient | None = None) -> None:
        if client is None and not token:
            raise ValueError("JUHE_MCP_TOKEN is not configured")
        self.client = client or JuheMcpClient(token or "")

    def cheapest_offer(
        self,
        origin_city: str,
        destination_name: str,
        departure_date: date | None,
    ) -> TransportOption:
        if departure_date is None:
            raise RuntimeError("出行日期缺失，无法查询机票")
        arrival_city = _destination_to_flight_city(destination_name)
        payload = self.client.call_tool(
            "get_flight_info",
            {
                "departure": origin_city,
                "arrival": arrival_city,
                "departureDate": departure_date.isoformat(),
                "maxSegments": 2,
            },
        )
        offers = _find_records(payload, marker_keys=("flightNo", "ticketPrice"), list_key="flightInfo")
        if not offers:
            return _text_flight_option(payload, origin_city, arrival_city)

        offer = min(offers, key=lambda item: _number(item.get("ticketPrice")) or float("inf"))
        price = _number(offer.get("ticketPrice"))
        duration_hours = _duration_hours(offer.get("duration"))
        flight_no = str(offer.get("flightNo") or "航班")
        departure = str(offer.get("departureName") or origin_city)
        arrival = str(offer.get("arrivalName") or arrival_city)
        times = _time_range(offer.get("departureTime"), offer.get("arrivalTime"))
        return TransportOption(
            mode="flight",
            duration_hours=duration_hours,
            price_estimate=_yuan(price),
            cost_estimate=_yuan(price),
            steps=[f"{flight_no}：{departure} → {arrival}{times}", "抵达后转乘地面交通前往登山口"],
            tip="机票信息来自聚合 MCP，价格和舱位可能变化，请在出票平台复核",
            booking_hint="仅提供查询，不在本系统内订票或支付",
            requires_user_verification=True,
            source="juhe-mcp-flight",
        )

    def rail_offer(
        self,
        origin_city: str,
        destination_name: str,
        departure_date: date | None,
    ) -> TransportOption:
        if departure_date is None:
            raise RuntimeError("出行日期缺失，无法查询火车票")
        arrival_station = _destination_to_rail_station(destination_name)
        payload = self.client.call_tool(
            "query_train_tickets",
            {
                "departure_station": origin_city,
                "arrival_station": arrival_station,
                "date": departure_date.isoformat(),
                "filter": "GDFS",
            },
        )
        trains = _find_records(payload, marker_keys=("train_no", "departure_time"))
        if not trains:
            return _text_rail_option(payload, origin_city, arrival_station)

        steps: list[str] = []
        prices: list[float] = []
        for train in trains[:3]:
            train_no = str(train.get("train_no") or train.get("trainNo") or "车次")
            departure = str(train.get("departure_station") or origin_city)
            arrival = str(train.get("arrival_station") or arrival_station)
            times = _time_range(train.get("departure_time"), train.get("arrival_time"))
            seat_hint, seat_prices = _train_seat_hint(train.get("prices"))
            prices.extend(seat_prices)
            steps.append(f"{train_no}：{departure} → {arrival}{times}{seat_hint}")
        steps.append("到站后转乘当地交通前往登山口")
        cheapest = min(prices) if prices else None
        return TransportOption(
            mode="rail",
            price_estimate=_yuan(cheapest),
            cost_estimate=_yuan(cheapest),
            steps=steps,
            tip="车次、票价和余票来自聚合 MCP，请以铁路 12306 实时结果为准",
            booking_hint="仅提供查询，不在本系统内订票或支付",
            requires_user_verification=True,
            source="juhe-mcp-train",
        )


def _tool_payload(result: object) -> object:
    if not isinstance(result, dict):
        return result
    structured = result.get("structuredContent")
    if structured is not None:
        return structured
    texts = [
        item.get("text", "")
        for item in result.get("content", [])
        if isinstance(item, dict) and item.get("type") == "text"
    ]
    text = "\n".join(part for part in texts if part).strip()
    if not text:
        return result
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _find_records(payload: object, marker_keys: tuple[str, ...], list_key: str | None = None) -> list[dict[str, object]]:
    if isinstance(payload, list):
        records = [item for item in payload if isinstance(item, dict)]
        if records and any(any(key in item for key in marker_keys) for item in records):
            return records
        for item in records:
            found = _find_records(item, marker_keys, list_key)
            if found:
                return found
    if isinstance(payload, dict):
        if list_key and isinstance(payload.get(list_key), list):
            return [item for item in payload[list_key] if isinstance(item, dict)]
        for value in payload.values():
            found = _find_records(value, marker_keys, list_key)
            if found:
                return found
    return []


def _destination_to_flight_city(destination: str) -> str:
    mapping = {"武功山": "南昌", "黄山": "黄山", "四姑娘山": "成都", "峨眉山": "成都", "张家界": "张家界"}
    return next((city for keyword, city in mapping.items() if keyword in destination), destination)


def _destination_to_rail_station(destination: str) -> str:
    mapping = {"武功山": "萍乡北", "黄山": "黄山北", "四姑娘山": "成都东", "峨眉山": "峨眉山", "张家界": "张家界西"}
    return next((station for keyword, station in mapping.items() if keyword in destination), destination)


def _text_flight_option(payload: object, origin: str, arrival: str) -> TransportOption:
    text = _safe_text(payload)
    if not text:
        raise RuntimeError("聚合 MCP 未返回航班结果")
    return TransportOption(
        mode="flight",
        steps=[text, f"{origin} → {arrival}，抵达后转乘地面交通前往登山口"],
        tip="机票信息来自聚合 MCP，请在出票平台复核",
        booking_hint="仅提供查询，不在本系统内订票或支付",
        source="juhe-mcp-flight",
    )


def _text_rail_option(payload: object, origin: str, arrival: str) -> TransportOption:
    text = _safe_text(payload)
    if not text:
        raise RuntimeError("聚合 MCP 未返回火车票结果")
    return TransportOption(
        mode="rail",
        steps=[text, f"{origin} → {arrival}，到站后转乘当地交通前往登山口"],
        tip="火车票信息来自聚合 MCP，请以铁路 12306 实时结果为准",
        booking_hint="仅提供查询，不在本系统内订票或支付",
        source="juhe-mcp-train",
    )


def _safe_text(payload: object) -> str:
    if not isinstance(payload, str):
        return ""
    return " ".join(payload.split())[:800]


def _number(value: object) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        match = re.search(r"\d+(?:\.\d+)?", value.replace(",", ""))
        return float(match.group()) if match else None
    return None


def _yuan(value: float | None) -> str | None:
    if value is None:
        return None
    return f"¥{value:.0f}" if value.is_integer() else f"¥{value:.2f}"


def _duration_hours(value: object) -> float | None:
    if isinstance(value, (int, float)):
        return round(float(value) / 60, 1)
    if not isinstance(value, str):
        return None
    hours = re.search(r"(\d+)\s*h", value, re.IGNORECASE)
    minutes = re.search(r"(\d+)\s*m", value, re.IGNORECASE)
    if not hours and not minutes:
        return None
    return round((int(hours.group(1)) if hours else 0) + (int(minutes.group(1)) if minutes else 0) / 60, 1)


def _time_range(start: object, end: object) -> str:
    return f"（{start} → {end}）" if start and end else ""


def _train_seat_hint(value: object) -> tuple[str, list[float]]:
    if not isinstance(value, list):
        return "", []
    labels: list[str] = []
    prices: list[float] = []
    for seat in value[:3]:
        if not isinstance(seat, dict):
            continue
        price = _number(seat.get("price"))
        if price is not None:
            prices.append(price)
        name = seat.get("seat_name") or seat.get("seatName")
        remaining = seat.get("num")
        if name:
            labels.append(f"{name} {remaining}" if remaining is not None else str(name))
    return (f"；{'、'.join(labels)}" if labels else ""), prices
