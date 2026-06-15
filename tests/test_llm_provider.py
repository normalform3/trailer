import sys
import types

from app.models import Coordinate, HikingGuideRequest, RouteAnalysis, RouteCandidate, RouteGeometry
from app.models import RouteSource
from app.providers.llm import BailianQwenGuideProvider, TemplateGuideProvider


def _make_candidate() -> RouteCandidate:
    return RouteCandidate(
        label="API 规划路线",
        route=RouteGeometry(
            name="候选路线",
            source=RouteSource.API_PLANNED,
            confidence=0.5,
            coordinates=[
                Coordinate(lon=114.0, lat=27.0, elevation_m=100),
                Coordinate(lon=114.1, lat=27.1, elevation_m=200),
            ],
        ),
        analysis=RouteAnalysis(
            distance_km=12.3,
            estimated_duration_hours=5.0,
            elevation={"min_m": 100, "max_m": 200, "ascent_m": 100, "descent_m": 0},
            risk_level="low",
        ),
    )


def test_bailian_provider_parses_dashscope_multimodal_response(monkeypatch) -> None:
    calls = {}

    class FakeMultiModalConversation:
        @staticmethod
        def call(api_key, model, messages):
            calls["api_key"] = api_key
            calls["model"] = model
            calls["messages"] = messages
            return types.SimpleNamespace(
                status_code=200,
                output=types.SimpleNamespace(
                    choices=[
                        types.SimpleNamespace(
                            message=types.SimpleNamespace(
                                content=[
                                    {
                                        "text": '{"summary":"适合两日轻装徒步。","recommendations":["避开雷雨","提前订住宿"]}'
                                    }
                                ]
                            )
                        )
                    ]
                ),
            )

    fake_dashscope = types.SimpleNamespace(
        base_http_api_url=None,
        MultiModalConversation=FakeMultiModalConversation,
    )
    monkeypatch.setitem(sys.modules, "dashscope", fake_dashscope)

    provider = BailianQwenGuideProvider(api_key="test-key")
    candidate = _make_candidate()

    draft = provider.generate_guide(
        HikingGuideRequest(destination="武功山"),
        [candidate],
        warnings=[],
        data_sources=["路由 API/兜底规划"],
    )

    assert fake_dashscope.base_http_api_url == "https://dashscope.aliyuncs.com/api/v1"
    assert calls["api_key"] == "test-key"
    assert calls["model"] == "qwen3.7-plus"
    content = calls["messages"][0]["content"][0]["text"]
    assert "输出必须是 JSON 对象" in content
    assert "武功山" in content
    assert draft.summary == "适合两日轻装徒步。"
    assert draft.recommendations == ["避开雷雨", "提前订住宿"]
    assert draft.source == "bailian:qwen3.7-plus"


def test_bailian_provider_parses_enhanced_response(monkeypatch) -> None:
    """Test that the enhanced LLM response with itinerary/gear_list/safety_guide is parsed."""
    class FakeMultiModalConversation:
        @staticmethod
        def call(api_key, model, messages):
            response_json = {
                "summary": "两日轻装徒步，路线中等难度。",
                "recommendations": ["带足饮水", "注意防晒"],
                "itinerary": {
                    "is_multi_day": True,
                    "total_days": 2,
                    "days": [
                        {"day_number": 1, "title": "Day 1: 上山段", "distance_km": 8.0, "key_segments": ["全程 8 km"], "notes": ["注意分配体力"]},
                        {"day_number": 2, "title": "Day 2: 下山段", "distance_km": 4.3, "key_segments": ["全程 4.3 km"], "notes": []},
                    ],
                },
                "gear_list": {
                    "categories": [
                        {"category": "基础装备", "items": ["登山杖", "登山鞋"]},
                    ],
                },
                "safety_guide": {
                    "general_warnings": ["注意天气变化"],
                    "risk_points": [],
                    "emergency_contacts": ["景区救援电话"],
                    "emergency_measures": ["迷路时原地等待"],
                    "seasonal_notes": [],
                },
            }
            import json
            return types.SimpleNamespace(
                status_code=200,
                output=types.SimpleNamespace(
                    choices=[
                        types.SimpleNamespace(
                            message=types.SimpleNamespace(
                                content=[{"text": json.dumps(response_json, ensure_ascii=False)}]
                            )
                        )
                    ]
                ),
            )

    fake_dashscope = types.SimpleNamespace(
        base_http_api_url=None,
        MultiModalConversation=FakeMultiModalConversation,
    )
    monkeypatch.setitem(sys.modules, "dashscope", fake_dashscope)

    provider = BailianQwenGuideProvider(api_key="test-key")
    candidate = _make_candidate()

    draft = provider.generate_guide(
        HikingGuideRequest(destination="武功山"),
        [candidate],
        warnings=[],
        data_sources=[],
    )

    assert draft.summary == "两日轻装徒步，路线中等难度。"
    assert draft.recommendations == ["带足饮水", "注意防晒"]
    assert draft.itinerary is not None
    assert draft.itinerary.is_multi_day is True
    assert draft.itinerary.total_days == 2
    assert len(draft.itinerary.days) == 2
    assert draft.gear_list is not None
    assert len(draft.gear_list.categories) == 1
    assert draft.gear_list.categories[0].category == "基础装备"
    assert draft.safety_guide is not None
    assert draft.safety_guide.general_warnings == ["注意天气变化"]


def test_template_provider_generates_itinerary_and_gear() -> None:
    """Test that TemplateGuideProvider generates itinerary, gear_list, and safety_guide."""
    provider = TemplateGuideProvider()
    candidate = _make_candidate()

    draft = provider.generate_guide(
        HikingGuideRequest(destination="武功山", fitness_level="beginner"),
        [candidate],
        warnings=[],
        data_sources=[],
    )

    assert draft.summary
    assert draft.recommendations
    assert draft.itinerary is not None
    assert draft.itinerary.is_multi_day is False  # 12.3km, 5h < 8h
    assert draft.gear_list is not None
    assert any(cat.category == "基础装备" for cat in draft.gear_list.categories)
    assert draft.safety_guide is not None
    assert draft.safety_guide.general_warnings


def test_template_provider_multi_day_itinerary() -> None:
    """Test that long routes produce multi-day itineraries."""
    provider = TemplateGuideProvider()
    candidate = RouteCandidate(
        label="用户提供轨迹",
        route=RouteGeometry(
            name="长路线",
            source=RouteSource.USER_KML,
            confidence=0.95,
            coordinates=[
                Coordinate(lon=114.0, lat=27.0, elevation_m=500),
                Coordinate(lon=114.5, lat=27.5, elevation_m=1800),
            ],
        ),
        analysis=RouteAnalysis(
            distance_km=35.0,
            estimated_duration_hours=12.0,
            elevation={"min_m": 500, "max_m": 1800, "ascent_m": 1300, "descent_m": 0},
            risk_level="medium",
        ),
    )

    draft = provider.generate_guide(
        HikingGuideRequest(destination="武功山"),
        [candidate],
        warnings=[],
        data_sources=[],
    )

    assert draft.itinerary is not None
    assert draft.itinerary.is_multi_day is True
    assert draft.itinerary.total_days > 1
