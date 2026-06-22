from __future__ import annotations

import sqlite3

from app.models import IntentFieldState, RecommendationEvidence, RouteRecommendationIntent, RouteRecommendationRequest
from app.providers.route_discovery import RawRouteCandidate
from app.services.route_knowledge import SQLiteRouteKnowledgeRepository
from app.services.route_knowledge_seed import starter_routes
from app.services.route_recommendations import RouteRecommendationService


class FixedIntentProvider:
    def __init__(self, intent: RouteRecommendationIntent) -> None:
        self.intent = intent

    def parse_intent(self, query, clarification_answers):
        return self.intent


class CountingSearchProvider:
    def __init__(self, candidates: list[RawRouteCandidate] | None = None) -> None:
        self.candidates = candidates or []
        self.calls = 0

    def search(self, intent, search_queries):
        self.calls += 1
        return self.candidates


class DisabledVerifier:
    enabled = False

    def verify(self, name, destination_region):
        return None


class FixedKnowledgeRepository:
    def __init__(self, candidates: list[RawRouteCandidate]) -> None:
        self.candidates = candidates

    def search(self, intent, limit=8):
        return self.candidates[:limit]


class FailingKnowledgeRepository:
    def search(self, intent, limit=8):
        raise sqlite3.OperationalError("database unavailable")


def knowledge_candidate(name: str, region: str = "浙江省杭州市") -> RawRouteCandidate:
    return RawRouteCandidate(
        name=name,
        region=region,
        evidence=[
            RecommendationEvidence(
                title="路线知识库来源",
                url=f"https://example.org/{name}",
                source_type="editorial",
            )
        ],
        retrieval_source="knowledge_base",
        popularity_label="省内精选",
        editorial_rank=2,
        official_status="unverified",
    )


def hangzhou_intent() -> RouteRecommendationIntent:
    return RouteRecommendationIntent(
        destination_region="杭州",
        field_states={"destination_region": IntentFieldState.EXPLICIT},
    )


def test_starter_corpus_has_fifteen_routes_for_each_target_region() -> None:
    records = starter_routes()

    assert len(records) == 150
    assert {record["province"] for record in records} == {
        "浙江", "广东", "福建", "江西", "安徽", "四川", "云南", "陕西", "北京", "河北"
    }
    assert all(sum(1 for item in records if item["province"] == province) == 15 for province in {item["province"] for item in records})


def test_sqlite_repository_filters_by_region_and_scenery(tmp_path) -> None:
    repository = SQLiteRouteKnowledgeRepository(tmp_path / "routes.sqlite3")
    route_names = [item.name for item in repository.search(
        RouteRecommendationIntent(destination_region="浙江", scenery_preferences=["溪流"]),
        limit=10,
    )]

    assert "径山古道" in route_names
    assert route_names
    assert all("浙江省" in item.region for item in repository.search(RouteRecommendationIntent(destination_region="浙江"), limit=10))


def test_sqlite_repository_alias_lookup_and_blocked_route_filter(tmp_path) -> None:
    repository = SQLiteRouteKnowledgeRepository(tmp_path / "routes.sqlite3")

    assert repository.search(RouteRecommendationIntent(destination_region="虎跳峡高路"), limit=3)[0].name == "虎跳峡高路徒步线路"
    assert repository.search(RouteRecommendationIntent(destination_region="鳌太"), limit=3) == []


def test_sqlite_repository_falls_back_when_fts_is_disabled(tmp_path) -> None:
    repository = SQLiteRouteKnowledgeRepository(tmp_path / "routes.sqlite3")
    repository.search(RouteRecommendationIntent(destination_region="杭州"), limit=3)
    repository._fts_enabled = False

    results = repository.search(RouteRecommendationIntent(destination_region="杭州"), limit=3)

    assert len(results) == 3
    assert all(item.retrieval_source == "knowledge_base" for item in results)


def test_repository_creates_normalized_schema(tmp_path) -> None:
    database_path = tmp_path / "routes.sqlite3"
    repository = SQLiteRouteKnowledgeRepository(database_path)
    repository.search(RouteRecommendationIntent(destination_region="杭州"), limit=3)

    with sqlite3.connect(database_path) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'view')")}

    assert {"routes", "route_aliases", "route_sources", "route_tags"}.issubset(tables)


def test_local_candidates_skip_live_search_when_three_are_available() -> None:
    search = CountingSearchProvider()
    service = RouteRecommendationService(
        intent_provider=FixedIntentProvider(hangzhou_intent()),
        search_provider=search,
        place_verifier=DisabledVerifier(),
        knowledge_repository=FixedKnowledgeRepository(
            [knowledge_candidate("径山古道"), knowledge_candidate("九溪十八涧"), knowledge_candidate("十里琅珰")]
        ),
    )

    response = service.recommend(RouteRecommendationRequest(query="杭州周边徒步"))

    assert search.calls == 0
    assert len(response.candidates) == 3
    assert response.data_sources == ["route-knowledge-base"]
    assert all(item.retrieval_source == "knowledge_base" for item in response.candidates)


def test_featured_view_uses_same_knowledge_repository_without_llm_or_web() -> None:
    search = CountingSearchProvider()
    service = RouteRecommendationService(
        intent_provider=FixedIntentProvider(hangzhou_intent()),
        search_provider=search,
        place_verifier=DisabledVerifier(),
        knowledge_repository=FixedKnowledgeRepository(
            [knowledge_candidate("虎跳峡高路徒步线路", "云南省迪庆"), knowledge_candidate("径山古道")]
        ),
    )

    response = service.featured(limit=2)

    assert search.calls == 0
    assert [item.name for item in response.candidates] == ["虎跳峡高路徒步线路", "径山古道"]
    assert response.data_sources == ["route-knowledge-base"]


def test_live_search_supplements_and_deduplicates_a_short_local_result() -> None:
    local = knowledge_candidate("径山古道")
    live_duplicate = RawRouteCandidate(
        name="径山古道",
        region="杭州",
        evidence=[RecommendationEvidence(title="网页来源", url="https://live.example/route")],
    )
    live_other = RawRouteCandidate(
        name="九溪十八涧",
        region="杭州",
        evidence=[
            RecommendationEvidence(title="来源一", url="https://one.example/route"),
            RecommendationEvidence(title="来源二", url="https://two.example/route"),
        ],
    )
    search = CountingSearchProvider([live_duplicate, live_other])
    service = RouteRecommendationService(
        intent_provider=FixedIntentProvider(hangzhou_intent()),
        search_provider=search,
        place_verifier=DisabledVerifier(),
        knowledge_repository=FixedKnowledgeRepository([local]),
    )

    response = service.recommend(RouteRecommendationRequest(query="杭州周边徒步"))

    assert search.calls == 1
    assert [item.name for item in response.candidates] == ["径山古道", "九溪十八涧"]
    assert len(response.candidates[0].evidence) == 2
    assert response.data_sources == ["route-knowledge-base", "bailian-web-search"]


def test_broken_knowledge_database_degrades_to_live_search() -> None:
    live = RawRouteCandidate(
        name="联网路线",
        region="杭州",
        evidence=[
            RecommendationEvidence(title="来源一", url="https://one.example/live"),
            RecommendationEvidence(title="来源二", url="https://two.example/live"),
        ],
    )
    search = CountingSearchProvider([live])
    service = RouteRecommendationService(
        intent_provider=FixedIntentProvider(hangzhou_intent()),
        search_provider=search,
        place_verifier=DisabledVerifier(),
        knowledge_repository=FailingKnowledgeRepository(),
    )

    response = service.recommend(RouteRecommendationRequest(query="杭州周边徒步"))

    assert search.calls == 1
    assert [item.name for item in response.candidates] == ["联网路线"]
    assert any("知识库暂不可用" in warning for warning in response.warnings)
    assert response.data_sources == ["bailian-web-search"]
