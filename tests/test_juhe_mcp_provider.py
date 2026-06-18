from datetime import date

import pytest

from app.providers.juhe_mcp import JuheMcpClient, JuheMcpTicketProvider


class RecordingMcpClient:
    def __init__(self, responses: dict[str, object]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, object]]] = []

    def call_tool(self, name: str, arguments: dict[str, object]) -> object:
        self.calls.append((name, arguments))
        return self.responses[name]


def test_juhe_mcp_client_only_allows_read_only_query_tools() -> None:
    assert JuheMcpClient.allowed_tools == {"query_train_tickets", "get_flight_info"}


def test_juhe_mcp_flight_provider_parses_cheapest_offer() -> None:
    client = RecordingMcpClient(
        {
            "get_flight_info": {
                "result": {
                    "flightInfo": [
                        {
                            "flightNo": "MU1234",
                            "departureName": "虹桥国际机场",
                            "arrivalName": "昌北国际机场",
                            "departureTime": "08:00",
                            "arrivalTime": "09:40",
                            "duration": "01h40m",
                            "ticketPrice": 680,
                        },
                        {"flightNo": "MU5678", "ticketPrice": 920},
                    ]
                }
            }
        }
    )
    provider = JuheMcpTicketProvider(client=client)  # type: ignore[arg-type]

    option = provider.cheapest_offer("上海", "武功山", date(2026, 7, 1))

    assert option.source == "juhe-mcp-flight"
    assert option.price_estimate == "¥680"
    assert option.duration_hours == 1.7
    assert "MU1234" in option.steps[0]
    assert client.calls == [
        (
            "get_flight_info",
            {
                "departure": "上海",
                "arrival": "南昌",
                "departureDate": "2026-07-01",
                "maxSegments": 2,
            },
        )
    ]


def test_juhe_mcp_train_provider_includes_price_and_availability() -> None:
    client = RecordingMcpClient(
        {
            "query_train_tickets": {
                "result": [
                    {
                        "train_no": "G1371",
                        "departure_station": "上海虹桥",
                        "arrival_station": "萍乡北",
                        "departure_time": "07:22",
                        "arrival_time": "12:11",
                        "prices": [
                            {"seat_name": "二等座", "price": 471.5, "num": "有"},
                            {"seat_name": "一等座", "price": 781, "num": "3"},
                        ],
                    }
                ]
            }
        }
    )
    provider = JuheMcpTicketProvider(client=client)  # type: ignore[arg-type]

    option = provider.rail_offer("上海", "武功山", date(2026, 7, 1))

    assert option.source == "juhe-mcp-train"
    assert option.price_estimate == "¥471.50"
    assert "G1371" in option.steps[0]
    assert "二等座 有" in option.steps[0]
    assert client.calls[0][0] == "query_train_tickets"
    assert client.calls[0][1]["arrival_station"] == "萍乡北"


def test_juhe_mcp_queries_require_departure_date() -> None:
    client = RecordingMcpClient({})
    provider = JuheMcpTicketProvider(client=client)  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="出行日期缺失"):
        provider.cheapest_offer("上海", "武功山", None)
    with pytest.raises(RuntimeError, match="出行日期缺失"):
        provider.rail_offer("上海", "武功山", None)
    assert client.calls == []
