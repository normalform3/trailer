from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app import main as main_module
from app.models import (
    IntentFieldState,
    RecommendationEvidence,
    RouteImportApplyRequest,
    RouteImportExtractedCandidate,
    RouteImportRequest,
    RouteKnowledgeCreate,
    RouteKnowledgeUpdate,
    RouteRecommendationIntent,
    RouteRecommendationRequest,
)
from app.providers.route_discovery import RawRouteCandidate
from app.services.route_knowledge import SQLiteRouteKnowledgeRepository
from app.services.route_knowledge_import import RouteKnowledgeImportService
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


class FixedRouteImportExtractor:
    def __init__(self, candidates: list[RouteImportExtractedCandidate]) -> None:
        self.candidates = candidates

    def extract(self, text, title=None, source_url=None):
        return self.candidates


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


def route_payload(**updates) -> RouteKnowledgeCreate:
    payload = {
        "id": "custom-test-route",
        "name": "测试古道",
        "province": "浙江",
        "city": "杭州",
        "summary": "适合管理页测试的路线知识卡。",
        "aliases": ["测试步道"],
        "tags": ["溪流", "竹林"],
        "transport_notes": ["公交到达后需步行接驳"],
        "editorial_rank": 2,
        "risk_level": "normal",
        "risk_notes": ["雨后石阶湿滑，需核验。"],
        "last_verified_at": "2026-06-23",
        "sources": [
            {
                "title": "测试来源",
                "url": "https://example.org/test-route",
                "source_type": "editorial",
                "summary": "公开路线介绍。",
            }
        ],
    }
    payload.update(updates)
    return RouteKnowledgeCreate.model_validate(payload)


def update_payload(**updates) -> RouteKnowledgeUpdate:
    data = route_payload().model_dump()
    data.pop("id", None)
    data.update(updates)
    return RouteKnowledgeUpdate.model_validate(data)


def import_candidate(**updates) -> RouteImportExtractedCandidate:
    payload = {
        "name": "测试古道",
        "region": "浙江省杭州市",
        "province": "浙江",
        "city": "杭州",
        "summary": "导入资料中的测试路线。",
        "tags": ["溪流", "古道"],
        "distance_km": 12.5,
        "duration_hours": 5.0,
        "ascent_m": 650,
        "risk_notes": ["需核验入口开放状态。"],
    }
    payload.update(updates)
    return RouteImportExtractedCandidate.model_validate(payload)


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


def test_repository_crud_updates_fts_and_search_visibility(tmp_path) -> None:
    repository = SQLiteRouteKnowledgeRepository(tmp_path / "routes.sqlite3")

    created = repository.create_record(route_payload())
    assert created.id == "custom-test-route"
    assert created.aliases == ["测试步道"]
    assert created.tags == ["溪流", "竹林"]
    assert created.source_count == 1

    updated = repository.update_record(
        created.id,
        update_payload(name="测试瀑布线", tags=["瀑布"], status="active"),
    )

    assert updated is not None
    assert updated.name == "测试瀑布线"
    waterfall_names = [item.name for item in repository.search(RouteRecommendationIntent(scenery_preferences=["瀑布"]), limit=20)]
    assert "测试瀑布线" in waterfall_names

    archived = repository.archive_record(created.id)

    assert archived is not None
    assert archived.status == "archived"
    assert all(item.name != "测试瀑布线" for item in repository.search(RouteRecommendationIntent(scenery_preferences=["瀑布"]), limit=20))


def test_repository_hard_delete_removes_related_rows(tmp_path) -> None:
    database_path = tmp_path / "routes.sqlite3"
    repository = SQLiteRouteKnowledgeRepository(database_path)
    repository.create_record(route_payload())

    assert repository.delete_record("custom-test-route") is True
    assert repository.get_record("custom-test-route") is None
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM route_aliases WHERE route_id = 'custom-test-route'").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM route_sources WHERE route_id = 'custom-test-route'").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM route_tags WHERE route_id = 'custom-test-route'").fetchone()[0] == 0


def test_repository_creates_import_job_and_matches_existing_routes(tmp_path) -> None:
    repository = SQLiteRouteKnowledgeRepository(tmp_path / "routes.sqlite3")
    repository.create_record(route_payload())

    job = repository.create_import_job(
        source_url="https://example.org/routes",
        raw_text="中国十大徒步路线资料",
        title="中国十大徒步路线",
        warnings=[],
        candidates=[
            import_candidate(name="测试步道"),
            import_candidate(name="测试古道", region="四川省甘孜州", province="四川", city="甘孜"),
            import_candidate(name="新发现海岸线", region="福建省宁德市", province="福建", city="宁德", tags=["海岸"]),
        ],
    )

    assert job.source_url == "https://example.org/routes"
    assert len(job.candidates) == 3
    alias_match = next(item for item in job.candidates if item.name == "测试步道")
    same_name_other_region = next(item for item in job.candidates if item.province == "四川")
    new_candidate = next(item for item in job.candidates if item.name == "新发现海岸线")
    assert alias_match.matched_route_id == "custom-test-route"
    assert alias_match.suggested_action == "merge"
    assert same_name_other_region.matched_route_id == "custom-test-route"
    assert same_name_other_region.suggested_action == "needs_review"
    assert new_candidate.suggested_action == "create"


def test_import_service_applies_create_merge_and_ignore(tmp_path) -> None:
    repository = SQLiteRouteKnowledgeRepository(tmp_path / "routes.sqlite3")
    repository.create_record(route_payload())
    service = RouteKnowledgeImportService(
        repository,
        extractor=FixedRouteImportExtractor(
            [
                import_candidate(name="新发现海岸线", region="福建省宁德市", province="福建", city="宁德", tags=["海岸"]),
                import_candidate(name="测试古道资料别名", tags=["瀑布"]),
                import_candidate(name="忽略路线", region="广东省广州市", province="广东", city="广州"),
            ]
        ),
    )
    job = service.create_job(
        RouteImportRequest(
            source_url="https://example.org/top-routes",
            raw_text="1. 新发现海岸线，福建宁德海岸徒步。2. 测试古道资料别名，杭州瀑布古道。3. 忽略路线。",
        )
    )
    by_name = {candidate.name: candidate for candidate in job.candidates}

    result = service.apply_job(
        job.id,
        RouteImportApplyRequest(
            decisions=[
                {"candidate_id": by_name["新发现海岸线"].id, "action": "create"},
                {"candidate_id": by_name["测试古道资料别名"].id, "action": "merge", "target_route_id": "custom-test-route"},
                {"candidate_id": by_name["忽略路线"].id, "action": "ignore"},
            ]
        ),
    )

    assert result.created_count == 1
    assert result.merged_count == 1
    assert result.ignored_count == 1
    assert "测试古道资料别名" in repository.get_record("custom-test-route").aliases
    assert "瀑布" in repository.get_record("custom-test-route").tags
    search_names = [item.name for item in repository.search(RouteRecommendationIntent(scenery_preferences=["海岸"]), limit=30)]
    assert "新发现海岸线" in search_names
    refreshed = repository.get_import_job(job.id)
    assert refreshed is not None
    assert {item.name: item.status for item in refreshed.candidates}["忽略路线"] == "ignored"


def test_route_knowledge_payload_rejects_non_http_source_url() -> None:
    with pytest.raises(ValidationError):
        route_payload(sources=[{"title": "本地文件", "url": "file:///tmp/route.md", "source_type": "note"}])


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


def test_route_knowledge_api_crud_and_delete_confirmation(tmp_path, monkeypatch) -> None:
    repository = SQLiteRouteKnowledgeRepository(tmp_path / "routes.sqlite3")
    monkeypatch.setattr(main_module, "_route_knowledge_repository", repository)
    monkeypatch.setattr(main_module, "_route_import_service", RouteKnowledgeImportService(repository, extractor=FixedRouteImportExtractor([])))
    monkeypatch.setattr(
        main_module,
        "_route_recommendation_service",
        RouteRecommendationService(
            intent_provider=FixedIntentProvider(hangzhou_intent()),
            search_provider=CountingSearchProvider(),
            place_verifier=DisabledVerifier(),
            knowledge_repository=repository,
        ),
    )
    client = TestClient(main_module.app)
    payload = route_payload().model_dump(mode="json")

    created = client.post("/api/v1/route-knowledge", json=payload)
    assert created.status_code == 201
    assert created.json()["id"] == "custom-test-route"

    listed = client.get("/api/v1/route-knowledge", params={"query": "测试", "status": "active"})
    assert listed.status_code == 200
    assert any(item["id"] == "custom-test-route" for item in listed.json()["records"])

    updated_payload = update_payload(name="测试瀑布线", tags=["瀑布"], status="active").model_dump(mode="json")
    updated = client.put("/api/v1/route-knowledge/custom-test-route", json=updated_payload)
    assert updated.status_code == 200
    assert updated.json()["name"] == "测试瀑布线"

    archive = client.delete("/api/v1/route-knowledge/custom-test-route", params={"mode": "archive"})
    assert archive.status_code == 200
    assert client.get("/api/v1/route-knowledge/custom-test-route").json()["status"] == "archived"
    featured = client.get("/api/v1/route-recommendations/featured", params={"region": "杭州", "limit": 12})
    assert "测试瀑布线" not in [item["name"] for item in featured.json()["candidates"]]

    missing_confirm = client.delete("/api/v1/route-knowledge/custom-test-route", params={"mode": "hard"})
    assert missing_confirm.status_code == 400

    hard_delete = client.delete(
        "/api/v1/route-knowledge/custom-test-route",
        params={"mode": "hard", "confirm": "custom-test-route"},
    )
    assert hard_delete.status_code == 200
    assert client.get("/api/v1/route-knowledge/custom-test-route").status_code == 404


def test_route_import_api_creates_reads_and_applies_draft(tmp_path, monkeypatch) -> None:
    repository = SQLiteRouteKnowledgeRepository(tmp_path / "routes.sqlite3")
    import_service = RouteKnowledgeImportService(
        repository,
        extractor=FixedRouteImportExtractor(
            [
                import_candidate(
                    name="宁德海岸线",
                    region="福建省宁德市",
                    province="福建",
                    city="宁德",
                    tags=["海岸", "长线"],
                )
            ]
        ),
    )
    monkeypatch.setattr(import_service, "_fetch_url", lambda url: ("", "测试资料", url))
    monkeypatch.setattr(main_module, "_route_knowledge_repository", repository)
    monkeypatch.setattr(main_module, "_route_import_service", import_service)
    monkeypatch.setattr(
        main_module,
        "_route_recommendation_service",
        RouteRecommendationService(
            intent_provider=FixedIntentProvider(RouteRecommendationIntent(destination_region="宁德")),
            search_provider=CountingSearchProvider(),
            place_verifier=DisabledVerifier(),
            knowledge_repository=repository,
        ),
    )
    client = TestClient(main_module.app)

    invalid = client.post("/api/v1/route-knowledge/import-jobs", json={})
    assert invalid.status_code == 422

    created = client.post(
        "/api/v1/route-knowledge/import-jobs",
        json={
            "source_url": "https://example.org/top-routes",
            "raw_text": "中国十大徒步路线：宁德海岸线，福建宁德海岸徒步。",
        },
    )
    assert created.status_code == 201
    job = created.json()
    assert job["candidates"][0]["name"] == "宁德海岸线"

    fetched = client.get(f"/api/v1/route-knowledge/import-jobs/{job['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["id"] == job["id"]

    applied = client.post(
        f"/api/v1/route-knowledge/import-jobs/{job['id']}/apply",
        json={"decisions": [{"candidate_id": job["candidates"][0]["id"], "action": "create"}]},
    )
    assert applied.status_code == 200
    assert applied.json()["created_count"] == 1

    featured = client.get("/api/v1/route-recommendations/featured", params={"region": "宁德", "limit": 12})
    assert featured.status_code == 200
    assert "宁德海岸线" in [item["name"] for item in featured.json()["candidates"]]


def test_route_knowledge_manager_page_is_served() -> None:
    client = TestClient(main_module.app)

    response = client.get("/route-knowledge-manager")

    assert response.status_code == 200
    assert "路线库管理" in response.text
    assert "route-import-workbench" in response.text
