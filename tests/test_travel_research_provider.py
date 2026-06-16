import httpx

from app.models import Coordinate, HikingGuideRequest, RouteAnalysis, RouteCandidate, RouteGeometry, RouteSource
from app.providers.travel_research import AmapPOIResearchProvider


def _candidate() -> RouteCandidate:
    return RouteCandidate(
        label="用户提供轨迹",
        route=RouteGeometry(
            name="测试路线",
            source=RouteSource.USER_KML,
            confidence=0.9,
            coordinates=[
                Coordinate(lon=114.0, lat=27.0),
                Coordinate(lon=114.01, lat=27.01),
            ],
        ),
        analysis=RouteAnalysis(
            distance_km=2.0,
            estimated_duration_hours=1.0,
            elevation={},
            risk_level="low",
        ),
    )


def test_amap_poi_research_collects_category_coordinates(monkeypatch) -> None:
    calls = []

    def fake_get(url, params, timeout):
        calls.append(params)
        keywords = params["keywords"]
        if "酒店" in keywords:
            poi = {"name": "山脚客栈", "address": "龙山村", "distance": "860", "location": "114.1001,27.1001", "type": "住宿服务", "tel": "123"}
        elif "餐馆" in keywords:
            poi = {"name": "登山口饭庄", "address": "登山口", "distance": "420", "location": "114.1002,27.1002", "type": "餐饮服务", "tel": ""}
        elif "便利店" in keywords:
            poi = {"name": "补给小店", "address": "村口", "distance": "500", "location": "114.1003,27.1003", "type": "购物服务", "tel": ""}
        else:
            poi = {"name": "路线停车场", "address": "入口", "distance": "1000", "location": "114.1004,27.1004", "type": "交通设施", "tel": ""}
        return httpx.Response(200, request=httpx.Request("GET", url), json={"pois": [poi]})

    monkeypatch.setattr(httpx, "get", fake_get)

    research = AmapPOIResearchProvider("test-key").collect(
        HikingGuideRequest(destination="武功山"),
        [_candidate()],
        [],
    )

    assert len(calls) == 8
    assert research.lodging[0].title == "山脚客栈"
    assert research.lodging[0].coordinate == Coordinate(lon=114.1001, lat=27.1001)
    assert research.lodging[0].metadata["address"] == "龙山村"
    assert research.lodging[0].metadata["coord_system"] == "amap_gcj02"
    assert research.food[0].coordinate == Coordinate(lon=114.1002, lat=27.1002)
    assert research.supply[0].coordinate == Coordinate(lon=114.1003, lat=27.1003)


def test_amap_poi_research_handles_missing_location(monkeypatch) -> None:
    def fake_get(url, params, timeout):
        poi = {"name": "无坐标餐馆", "address": "山下", "distance": "300", "type": "餐饮服务"}
        return httpx.Response(200, request=httpx.Request("GET", url), json={"pois": [poi]})

    monkeypatch.setattr(httpx, "get", fake_get)

    items = AmapPOIResearchProvider("test-key")._query(
        Coordinate(lon=114.0, lat=27.0),
        "餐馆",
        "餐饮",
    )

    assert items[0].title == "无坐标餐馆"
    assert items[0].coordinate is None
