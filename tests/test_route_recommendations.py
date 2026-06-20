from __future__ import annotations

from fastapi.testclient import TestClient

from app import main as main_module
from app.models import (
    Coordinate,
    IntentFieldState,
    RecommendationEvidence,
    RouteRecommendationIntent,
    RouteRecommendationRequest,
)
from app.providers import route_discovery as discovery_module
from app.providers.route_discovery import (
    AmapRoutePlaceVerifier,
    BailianRouteDiscoveryProvider,
    RawRouteCandidate,
    VerifiedRoutePlace,
    _best_poi_match,
    _parse_json_object,
    _parse_route_search_lines,
)
from app.services.route_recommendations import (
    RouteRecommendationService,
    RouteRecommendationUnavailable,
)


class FixedIntentProvider:
    def __init__(self, intent: RouteRecommendationIntent) -> None:
        self.intent = intent
        self.calls = []

    def parse_intent(self, query, clarification_answers):
        self.calls.append((query, clarification_answers))
        return self.intent


class FixedSearchProvider:
    def __init__(self, candidates: list[RawRouteCandidate]) -> None:
        self.candidates = candidates
        self.queries: list[str] = []

    def search(self, intent, search_queries):
        self.queries = search_queries
        return self.candidates


class FixedVerifier:
    def __init__(self, places: dict[str, VerifiedRoutePlace] | None = None, enabled: bool = True) -> None:
        self.places = places or {}
        self._enabled = enabled

    @property
    def enabled(self):
        return self._enabled

    def verify(self, name, destination_region):
        return self.places.get(name)


class MissingKeyIntentProvider:
    def parse_intent(self, query, clarification_answers):
        raise RuntimeError("未配置 DASHSCOPE_API_KEY 或 BAILIAN_API_KEY")


class FailingSearchProvider:
    def search(self, intent, search_queries):
        raise RuntimeError("DashScope request failed (500): secret provider detail")


def evidence(domain: str, suffix: str = "route") -> RecommendationEvidence:
    return RecommendationEvidence(
        title=f"{domain} 路线资料",
        url=f"https://{domain}/{suffix}",
        summary="公开路线介绍",
    )


def candidate(
    name: str,
    *,
    difficulty: str | None = "简单",
    duration: float | None = 5,
    sources: list[RecommendationEvidence] | None = None,
    aliases: list[str] | None = None,
) -> RawRouteCandidate:
    return RawRouteCandidate(
        name=name,
        aliases=aliases or [],
        region="浙江省杭州市",
        difficulty=difficulty,
        duration_hours=duration,
        distance_km=10,
        scenery=["溪流", "竹林"],
        transport_notes=["公交换乘景区接驳可达"],
        evidence=sources or [evidence("example.com")],
    )


def intent(**updates) -> RouteRecommendationIntent:
    base = RouteRecommendationIntent(
        destination_region="杭州",
        fitness_level="beginner",
        max_duration_hours=6,
        scenery_preferences=["溪流"],
        transport_preference="公共交通优先",
        exclusions=[],
        field_states={
            "destination_region": IntentFieldState.EXPLICIT,
            "fitness_level": IntentFieldState.EXPLICIT,
            "max_duration_hours": IntentFieldState.EXPLICIT,
            "transport_preference": IntentFieldState.EXPLICIT,
        },
    )
    return base.model_copy(update=updates)


def verified(name: str) -> VerifiedRoutePlace:
    return VerifiedRoutePlace(
        name=name,
        region="浙江省杭州市余杭区",
        address="径山镇",
        coordinate=Coordinate(lon=119.8, lat=30.4),
    )


def test_recommendation_ranks_evidence_backed_matches() -> None:
    good = candidate("径山古道")
    unknown = candidate("午潮山", difficulty=None, duration=None)
    service = RouteRecommendationService(
        intent_provider=FixedIntentProvider(intent()),
        search_provider=FixedSearchProvider([unknown, good]),
        place_verifier=FixedVerifier({"径山古道": verified("径山古道"), "午潮山": verified("午潮山")}),
    )

    response = service.recommend(RouteRecommendationRequest(query="杭州周边新手溪流路线"))

    assert [item.name for item in response.candidates] == ["径山古道", "午潮山"]
    assert response.candidates[0].match_score > response.candidates[1].match_score
    assert response.candidates[0].coordinate_system == "gcj02"
    assert "难度" in response.candidates[1].unknown_fields


def test_recommendation_emits_visible_search_phases() -> None:
    search = FixedSearchProvider([candidate("径山古道")])
    service = RouteRecommendationService(
        intent_provider=FixedIntentProvider(intent()),
        search_provider=search,
        place_verifier=FixedVerifier({"径山古道": verified("径山古道")}),
    )
    events = []

    service.recommend(RouteRecommendationRequest(query="杭州周边徒步"), on_event=events.append)

    phases = [event["phase"] for event in events]
    assert phases[:4] == ["intent", "intent", "query", "search"]
    assert "verify" in phases
    assert phases[-1] == "rank"
    assert events[-1]["route_names"] == ["径山古道"]


def test_candidate_without_poi_requires_two_independent_domains() -> None:
    one_source = candidate("单源路线", sources=[evidence("same.example")])
    two_sources = candidate(
        "双源路线",
        sources=[evidence("one.example"), evidence("two.example")],
    )
    service = RouteRecommendationService(
        intent_provider=FixedIntentProvider(intent()),
        search_provider=FixedSearchProvider([one_source, two_sources]),
        place_verifier=FixedVerifier(enabled=False),
    )

    response = service.recommend(RouteRecommendationRequest(query="杭州徒步"))

    assert [item.name for item in response.candidates] == ["双源路线"]
    assert response.candidates[0].coordinate is None
    assert any("高德" in warning for warning in response.warnings)


def test_aliases_merge_sources_before_evidence_gate() -> None:
    first = candidate("径山古道", aliases=["径山步道"], sources=[evidence("one.example")])
    second = candidate("径山步道", aliases=["径山古道"], sources=[evidence("two.example")])
    service = RouteRecommendationService(
        intent_provider=FixedIntentProvider(intent()),
        search_provider=FixedSearchProvider([first, second]),
        place_verifier=FixedVerifier(enabled=False),
    )

    response = service.recommend(RouteRecommendationRequest(query="杭州古道"))

    assert len(response.candidates) == 1
    assert len(response.candidates[0].evidence) == 2


def test_hard_constraints_remove_advanced_or_overlong_routes() -> None:
    advanced = candidate("高难路线", difficulty="高难进阶", sources=[evidence("a.example"), evidence("b.example")])
    overlong = candidate("长线", duration=9, sources=[evidence("c.example"), evidence("d.example")])
    service = RouteRecommendationService(
        intent_provider=FixedIntentProvider(intent()),
        search_provider=FixedSearchProvider([advanced, overlong]),
        place_verifier=FixedVerifier(enabled=False),
    )

    response = service.recommend(RouteRecommendationRequest(query="新手六小时内"))

    assert response.candidates == []
    assert any("没有找到" in warning for warning in response.warnings)


def test_unknown_destination_generates_one_clarifying_question() -> None:
    unknown_intent = RouteRecommendationIntent(field_states={})
    service = RouteRecommendationService(
        intent_provider=FixedIntentProvider(unknown_intent),
        search_provider=FixedSearchProvider([]),
        place_verifier=FixedVerifier(enabled=False),
    )

    response = service.recommend(RouteRecommendationRequest(query="想找一条风景好的路线"))

    assert response.clarifying_question is not None
    assert response.clarifying_question.id == "destination_region"
    assert response.intent.field_states["destination_region"] == IntentFieldState.UNKNOWN


def test_clarification_answers_are_forwarded_and_affect_queries() -> None:
    parser = FixedIntentProvider(intent(destination_region="上海", max_duration_hours=4))
    search = FixedSearchProvider([])
    service = RouteRecommendationService(parser, search, FixedVerifier(enabled=False))

    service.recommend(
        RouteRecommendationRequest(
            query="想徒步",
            clarification_answers={"destination_region": "上海周边", "max_duration_hours": "4小时"},
        )
    )

    assert parser.calls[0][1]["destination_region"] == "上海周边"
    assert any("上海" in query and "4小时" in query for query in search.queries)


def test_request_rejects_overlong_or_prompt_only_input() -> None:
    client = TestClient(main_module.app)
    assert client.post("/api/v1/route-recommendations", json={"query": "x"}).status_code == 422
    assert client.post("/api/v1/route-recommendations", json={"query": "徒" * 1001}).status_code == 422


def test_api_contract_returns_nullable_metrics_and_sources(monkeypatch) -> None:
    route = candidate("径山古道", duration=None, sources=[evidence("one.example"), evidence("two.example")])
    service = RouteRecommendationService(
        FixedIntentProvider(intent()),
        FixedSearchProvider([route]),
        FixedVerifier(enabled=False),
    )
    monkeypatch.setattr(main_module, "_route_recommendation_service", service)
    client = TestClient(main_module.app)

    response = client.post("/api/v1/route-recommendations", json={"query": "杭州周边徒步"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["candidates"][0]["duration_hours"] is None
    assert payload["candidates"][0]["evidence"][0]["url"].startswith("https://")
    assert payload["data_sources"] == ["bailian-web-search"]


def test_api_returns_503_when_bailian_is_not_configured(monkeypatch) -> None:
    service = RouteRecommendationService(
        MissingKeyIntentProvider(),
        FixedSearchProvider([]),
        FixedVerifier(enabled=False),
    )
    monkeypatch.setattr(main_module, "_route_recommendation_service", service)
    client = TestClient(main_module.app)

    response = client.post("/api/v1/route-recommendations", json={"query": "杭州周边徒步"})

    assert response.status_code == 503
    assert "DASHSCOPE" in response.json()["detail"]


def test_api_hides_raw_provider_errors(monkeypatch) -> None:
    service = RouteRecommendationService(
        FixedIntentProvider(intent()),
        FailingSearchProvider(),
        FixedVerifier(enabled=False),
    )
    monkeypatch.setattr(main_module, "_route_recommendation_service", service)
    client = TestClient(main_module.app)

    response = client.post("/api/v1/route-recommendations", json={"query": "杭州周边徒步"})

    assert response.status_code == 502
    assert "secret provider detail" not in response.json()["detail"]
    assert "百炼联网检索暂不可用" in response.json()["detail"]


def test_json_parser_accepts_fenced_json_and_rejects_arrays() -> None:
    assert _parse_json_object('```json\n{"candidates": []}\n```') == {"candidates": []}
    try:
        _parse_json_object("[]")
    except RuntimeError as exc:
        assert "JSON 对象" in str(exc)
    else:
        raise AssertionError("array response should be rejected")


def test_amap_matcher_rejects_unrelated_pois() -> None:
    pois = [
        {"name": "径山古道游客中心", "location": "119.8,30.4"},
        {"name": "完全不同景区", "location": "120.0,30.0"},
    ]
    assert _best_poi_match("径山古道", pois)["name"] == "径山古道游客中心"
    assert _best_poi_match("九溪十八涧", pois) is None


def test_amap_verifier_returns_gcj02_coordinate(monkeypatch) -> None:
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "pois": [
                    {
                        "name": "径山古道",
                        "location": "119.801,30.402",
                        "pname": "浙江省",
                        "cityname": "杭州市",
                        "adname": "余杭区",
                        "address": "径山镇",
                    }
                ]
            }

    monkeypatch.setattr(discovery_module.httpx, "get", lambda *args, **kwargs: Response())
    verifier = AmapRoutePlaceVerifier(api_key="test-key")

    place = verifier.verify("径山古道", "杭州")

    assert place is not None
    assert place.coordinate_system == "gcj02"
    assert place.coordinate.lon == 119.801
    assert "杭州市" in place.region


def test_responses_web_search_keeps_only_cited_urls(monkeypatch) -> None:
    class Response:
        status_code = 200

        def json(self):
            return {
                "output": [
                    {
                        "type": "web_search_call",
                        "action": {
                            "query": "杭州 徒步路线",
                            "sources": [{"type": "url", "url": "https://source.example/route"}],
                        },
                    },
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "ROUTE_NAME: 径山古道 || REGION: 杭州 || SOURCE: https://source.example/route",
                                "annotations": [
                                    {"type": "url_citation", "title": "真实来源", "url": "https://source.example/route"}
                                ],
                            }
                        ],
                    },
                ]
            }

    monkeypatch.setattr(discovery_module.httpx, "post", lambda *args, **kwargs: Response())
    provider = BailianRouteDiscoveryProvider(api_key="test-key", base_url="https://example.test")

    result = provider.search(intent(), ["杭州 徒步路线"])

    assert provider.last_search_actions == ["杭州 徒步路线"]
    assert [item.url for item in result[0].evidence] == ["https://source.example/route"]


def test_route_line_parser_rejects_urls_not_returned_by_search_tool() -> None:
    sources = [evidence("source.example")]
    text = (
        "ROUTE_NAME: 真实路线 || REGION: 杭州 || SOURCE: https://source.example/route\n"
        "ROUTE_NAME: 伪造路线 || REGION: 杭州 || SOURCE: https://fake.example/route"
    )

    parsed = _parse_route_search_lines(text, sources, "杭州")

    assert [item.name for item in parsed] == ["真实路线"]


def test_stream_endpoint_emits_trace_before_final(monkeypatch) -> None:
    service = RouteRecommendationService(
        FixedIntentProvider(intent()),
        FixedSearchProvider([candidate("径山古道")]),
        FixedVerifier({"径山古道": verified("径山古道")}),
    )
    monkeypatch.setattr(main_module, "_route_recommendation_service", service)
    client = TestClient(main_module.app)

    response = client.post("/api/v1/route-recommendations/stream", json={"query": "杭州周边徒步"})

    assert response.status_code == 200
    assert '"event": "trace"' in response.text
    assert response.text.index('"event": "trace"') < response.text.index('"event": "final"')


def test_unavailable_error_type_is_public() -> None:
    assert issubclass(RouteRecommendationUnavailable, RuntimeError)
