import sys
import types
from datetime import date

from app.models import Coordinate, GuideReferenceItem, GuideReferenceResearch, HikingGuideRequest, RouteAnalysis, RouteCandidate, RouteGeometry
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


def test_bailian_provider_minimal_connection_check(monkeypatch) -> None:
    calls = {}

    class FakeMultiModalConversation:
        @staticmethod
        def call(api_key, model, messages, parameters=None):
            calls["api_key"] = api_key
            calls["model"] = model
            calls["messages"] = messages
            calls["parameters"] = parameters
            return types.SimpleNamespace(
                status_code=200,
                output=types.SimpleNamespace(
                    choices=[
                        types.SimpleNamespace(
                            message=types.SimpleNamespace(
                                content=[{"text": "OK"}]
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
    result = provider.test_connection()

    assert result["ok"] is True
    assert result["reply_preview"] == "OK"
    assert calls["parameters"] == {"max_tokens": 2}
    assert calls["messages"][0]["content"][0]["text"] == "只回复 OK"


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


def test_bailian_provider_parses_tool_plan_response(monkeypatch) -> None:
    class FakeMultiModalConversation:
        @staticmethod
        def call(api_key, model, messages):
            response_json = {
                "tool_plan": {
                    "query_weather": True,
                    "query_lodging": True,
                    "query_food": False,
                    "query_supply": True,
                    "query_transport": True,
                    "compose_with_llm": True,
                    "rationale": ["用户需要完整攻略"],
                },
                "clarifying_questions": ["补充返回日期"],
                "validation_notes": ["票价需实时核对"],
                "priority_notes": ["天气优先"],
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

    decision = provider.plan_tools(
        HikingGuideRequest(destination="武功山", start_city="上海"),
        [candidate.route],
        warnings=[],
        data_sources=[],
    )

    assert decision.tool_plan.query_weather is True
    assert decision.tool_plan.query_food is False
    assert decision.tool_plan.query_transport is True
    assert decision.tool_plan.rationale == ["用户需要完整攻略"]
    assert decision.clarifying_questions == ["补充返回日期"]
    assert decision.validation_notes == ["票价需实时核对"]


def test_bailian_provider_generates_reference_research_without_touching_main_prompt(monkeypatch) -> None:
    calls = {}

    class FakeMultiModalConversation:
        @staticmethod
        def call(api_key, model, messages):
            calls["messages"] = messages
            response_json = {
                "supplemental_summary": "参考攻略提示山顶住宿紧张，仅作补充。",
                "itinerary_suggestions": ["可参考龙山村上山节奏"],
                "lodging_supply_transport_notes": ["住宿需提前核验"],
                "risk_notes": ["雨天路滑"],
                "verification_items": ["核验山顶住宿是否营业"],
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
    result = provider.generate_reference_research(
        HikingGuideRequest(destination="武功山"),
        [_make_candidate()],
        "主攻略摘要",
        GuideReferenceResearch(
            items=[
                GuideReferenceItem(
                    title="用户攻略",
                    summary="龙山村上山，住宿紧张。",
                    source="user-notes",
                )
            ]
        ),
    )

    prompt = calls["messages"][0]["content"][0]["text"]
    assert "参考攻略补充规划器" in prompt
    assert "不得覆盖主攻略结论" in prompt
    assert result.items[0].title == "用户攻略"
    assert result.supplemental_summary == "参考攻略提示山顶住宿紧张，仅作补充。"
    assert result.verification_items == ["核验山顶住宿是否营业"]


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


def test_template_provider_respects_relaxed_user_date_range() -> None:
    provider = TemplateGuideProvider()
    candidate = _make_candidate()

    draft = provider.generate_guide(
        HikingGuideRequest(
            destination="武功山",
            start_city="上海",
            date_range=(date(2026, 7, 1), date(2026, 7, 4)),
        ),
        [candidate],
        warnings=[],
        data_sources=[],
    )

    assert draft.itinerary is not None
    assert draft.itinerary.total_days == 4
    assert any("周边游览" in day.title or "抵达" in day.title for day in draft.itinerary.days)
    assert any("最短建议 1 天" in note for note in draft.itinerary.notes)


def test_template_provider_warns_when_user_date_range_is_too_short() -> None:
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
        HikingGuideRequest(
            destination="武功山",
            date_range=(date(2026, 7, 1), date(2026, 7, 1)),
        ),
        [candidate],
        warnings=[],
        data_sources=[],
    )

    assert draft.itinerary is not None
    assert draft.itinerary.total_days == 2
    assert any("短于最短建议 2 天" in note for note in draft.itinerary.notes)
