from __future__ import annotations

import re
from collections import OrderedDict
from collections.abc import Callable
from urllib.parse import urlparse

from app.models import (
    IntentFieldState,
    RouteRecommendationCandidate,
    RouteRecommendationIntent,
    RouteRecommendationQuestion,
    RouteRecommendationRequest,
    RouteRecommendationResponse,
)
from app.providers.route_discovery import (
    AmapRoutePlaceVerifier,
    BailianRouteDiscoveryProvider,
    RawRouteCandidate,
    RouteDiscoverySearchProvider,
    RouteIntentProvider,
    RoutePlaceVerifier,
    VerifiedRoutePlace,
)
from app.services.route_knowledge import (
    NullRouteKnowledgeRepository,
    RouteKnowledgeRepository,
    SQLiteRouteKnowledgeRepository,
)


class RouteRecommendationUnavailable(RuntimeError):
    pass


class RouteRecommendationService:
    def __init__(
        self,
        intent_provider: RouteIntentProvider | None = None,
        search_provider: RouteDiscoverySearchProvider | None = None,
        place_verifier: RoutePlaceVerifier | None = None,
        knowledge_repository: RouteKnowledgeRepository | None = None,
    ) -> None:
        shared_provider = BailianRouteDiscoveryProvider()
        self.intent_provider = intent_provider or shared_provider
        self.search_provider = search_provider or shared_provider
        self.place_verifier = place_verifier or AmapRoutePlaceVerifier()
        custom_dependencies = any(item is not None for item in (intent_provider, search_provider, place_verifier))
        self.knowledge_repository = knowledge_repository or (
            NullRouteKnowledgeRepository() if custom_dependencies else SQLiteRouteKnowledgeRepository()
        )

    def recommend(
        self,
        request: RouteRecommendationRequest,
        on_event: Callable[[dict[str, object]], None] | None = None,
    ) -> RouteRecommendationResponse:
        _emit(on_event, "intent", "正在理解你的徒步需求", "running", "提取地区、体能、时间与偏好条件。")
        try:
            intent = self.intent_provider.parse_intent(request.query, request.clarification_answers)
        except RuntimeError as exc:
            if "未配置" in str(exc):
                raise RouteRecommendationUnavailable(str(exc)) from exc
            raise

        intent = _normalize_intent_states(intent)
        explicit_count = sum(1 for state in intent.field_states.values() if state == IntentFieldState.EXPLICIT)
        _emit(on_event, "intent", "需求解析完成", "completed", f"识别到 {explicit_count} 个明确条件。")
        warnings: list[str] = []
        knowledge_candidates: list[RawRouteCandidate] = []
        knowledge_attempted = not isinstance(self.knowledge_repository, NullRouteKnowledgeRepository)
        if knowledge_attempted:
            _emit(on_event, "knowledge", "正在检索路线知识库", "running", "按地区、偏好与硬条件查找已收录路线。")
            try:
                knowledge_candidates = self.knowledge_repository.search(intent, limit=8)
                _emit(
                    on_event,
                    "knowledge",
                    "路线知识库检索完成",
                    "completed",
                    f"命中 {len(knowledge_candidates)} 条可用路线。",
                    route_names=[candidate.name for candidate in knowledge_candidates],
                )
            except Exception as exc:  # noqa: BLE001 - live web remains a safe fallback.
                warnings.append(f"本地路线知识库暂不可用，已切换联网检索：{exc}")
                _emit(on_event, "knowledge", "路线知识库暂不可用", "warning", "已切换到联网检索。")

        web_candidates: list[RawRouteCandidate] = []
        search_sources: list[object] = []
        search_attempted = False
        if len(knowledge_candidates) < 3:
            search_attempted = True
            queries = _build_search_queries(intent)
            _emit(
                on_event,
                "query",
                "生成联网补充查询",
                "completed",
                f"本地候选不足，准备执行 {len(queries)} 组搜索关键词。",
                queries=queries,
            )
            _emit(on_event, "search", "百炼正在补充公开网页", "running", "只收集真实路线名称和可追溯来源。")
            try:
                web_candidates = self.search_provider.search(intent, queries)
            except Exception as exc:
                if not knowledge_candidates:
                    raise
                warnings.append("联网补充暂不可用，当前结果仅来自本地路线知识库。")
                _emit(on_event, "search", "联网补充暂不可用", "warning", "保留知识库候选继续生成结果。")
            else:
                actual_queries = list(getattr(self.search_provider, "last_search_actions", []) or [])
                search_sources = list(getattr(self.search_provider, "last_search_sources", []) or [])
                _emit(
                    on_event,
                    "search",
                    "联网补充完成",
                    "completed",
                    f"补充 {len(web_candidates)} 个原始路线名称、{len(search_sources)} 个引用来源。",
                    queries=actual_queries or queries,
                    route_names=[candidate.name for candidate in web_candidates],
                    source_count=len(search_sources),
                )
        else:
            _emit(on_event, "search", "无需联网补充", "completed", "知识库已有足够候选，本次未调用网页搜索。")

        raw_candidates = [*knowledge_candidates, *web_candidates]
        merged = _merge_candidates(raw_candidates)

        if not self.place_verifier.enabled:
            warnings.append("未配置高德 Web 服务 Key；联网候选必须由至少两个独立网页来源交叉验证。")

        ranked: list[RouteRecommendationCandidate] = []
        for index, raw in enumerate(merged, start=1):
            _emit(
                on_event,
                "verify",
                f"核验路线：{raw.name}",
                "running",
                f"正在检查地点与网页来源（{index}/{len(merged)}）。",
                route_name=raw.name,
            )
            place: VerifiedRoutePlace | None = None
            if self.place_verifier.enabled:
                try:
                    place = self.place_verifier.verify(raw.name, intent.destination_region)
                except Exception as exc:  # noqa: BLE001 - search evidence can still produce a partial result.
                    warnings.append(f"高德地点核验暂不可用：{exc}")

            independent_domains = _independent_domains(raw)
            if not raw.evidence:
                _emit(on_event, "verify", f"跳过：{raw.name}", "warning", "没有可验证的网页引用。", route_name=raw.name)
                continue
            if raw.retrieval_source != "knowledge_base" and place is None and len(independent_domains) < 2:
                _emit(on_event, "verify", f"跳过：{raw.name}", "warning", "地点未确认且不足两个独立来源。", route_name=raw.name)
                continue
            if _hard_conflict(intent, raw):
                _emit(on_event, "verify", f"过滤：{raw.name}", "warning", "与用户明确条件冲突。", route_name=raw.name)
                continue
            ranked.append(_score_candidate(intent, raw, place, len(independent_domains)))
            if raw.risk_level == "high":
                warnings.append(f"{raw.name} 属于高风险路线，出发前必须核对属地公告、开放状态与准入要求。")
            _emit(on_event, "verify", f"通过：{raw.name}", "completed", "名称与来源达到展示门槛。", route_name=raw.name)

        _emit(on_event, "rank", "正在整理推荐名称", "running", "按地区、偏好和证据质量排序。")
        ranked.sort(key=lambda item: (-item.match_score, -item.confidence, item.name))
        candidates = ranked[:3]
        if len(candidates) < 3:
            warnings.append(f"仅找到 {len(candidates)} 条达到证据门槛的候选，没有使用模型补齐。")
        if not candidates:
            warnings.append("没有找到同时满足来源与地点核验要求的路线，请补充更具体的地区或放宽条件。")

        sources: list[str] = []
        if knowledge_attempted and knowledge_candidates:
            sources.append("route-knowledge-base")
        if search_attempted:
            sources.append("bailian-web-search")
        if self.place_verifier.enabled and merged:
            sources.append("amap-place-search")
        response = RouteRecommendationResponse(
            intent=intent,
            candidates=candidates,
            clarifying_question=_clarifying_question(intent),
            warnings=_dedupe_strings(warnings),
            data_sources=sources,
        )
        _emit(
            on_event,
            "rank",
            "推荐结果已生成",
            "completed",
            f"最终保留 {len(candidates)} 条有来源的路线名称。",
            route_names=[candidate.name for candidate in candidates],
        )
        return response

    def featured(self, region: str | None = None, limit: int = 3) -> RouteRecommendationResponse:
        intent = _normalize_intent_states(
            RouteRecommendationIntent(
                destination_region=region,
                field_states={
                    "destination_region": IntentFieldState.EXPLICIT if region else IntentFieldState.UNKNOWN,
                },
            )
        )
        try:
            raw_candidates = self.knowledge_repository.search(intent, limit=max(3, min(limit, 12)))
        except Exception as exc:  # noqa: BLE001 - exposed as a stable service-level failure.
            raise RouteRecommendationUnavailable("路线知识库暂不可用，请稍后重试。") from exc
        candidates = [
            _score_candidate(intent, raw, None, len(_independent_domains(raw)))
            for raw in raw_candidates
            if raw.evidence and not _hard_conflict(intent, raw)
        ][:limit]
        warnings = [
            f"{raw.name} 属于高风险路线，请先核对属地公告与准入要求。"
            for raw in raw_candidates[:limit]
            if raw.risk_level == "high"
        ]
        if not candidates:
            warnings.append("当前路线知识库没有符合条件的精选路线。")
        return RouteRecommendationResponse(
            intent=intent,
            candidates=candidates,
            warnings=_dedupe_strings(warnings),
            data_sources=["route-knowledge-base"],
        )


def _emit(
    callback: Callable[[dict[str, object]], None] | None,
    phase: str,
    title: str,
    status: str,
    detail: str,
    **extra: object,
) -> None:
    if callback is None:
        return
    callback({"phase": phase, "title": title, "status": status, "detail": detail, **extra})


def _normalize_intent_states(intent: RouteRecommendationIntent) -> RouteRecommendationIntent:
    fields = (
        "destination_region",
        "origin_city",
        "travel_date_or_season",
        "trip_days",
        "fitness_level",
        "min_distance_km",
        "max_distance_km",
        "max_duration_hours",
        "max_ascent_m",
        "scenery_preferences",
        "transport_preference",
        "camping_preference",
        "exclusions",
    )
    states = dict(intent.field_states)
    for field in fields:
        value = getattr(intent, field)
        if field not in states:
            states[field] = (
                IntentFieldState.EXPLICIT
                if value not in (None, [], "")
                else IntentFieldState.UNKNOWN
            )
    return intent.model_copy(update={"field_states": states})


def _build_search_queries(intent: RouteRecommendationIntent) -> list[str]:
    region = intent.destination_region or "中国"
    details: list[str] = []
    if intent.fitness_level:
        details.append({"beginner": "新手", "intermediate": "中等难度", "advanced": "进阶"}[intent.fitness_level])
    if intent.trip_days:
        details.append(f"{intent.trip_days}天")
    if intent.max_duration_hours:
        details.append(f"{intent.max_duration_hours:g}小时内")
    details.extend(intent.scenery_preferences[:2])
    suffix = " ".join(details)
    queries = [
        f"{region} 徒步路线 {suffix} 攻略",
        f"{region} 徒步 线路 起点 终点 {suffix}",
    ]
    if intent.transport_preference:
        queries.append(f"{region} 徒步路线 {intent.transport_preference} 可达")
    if intent.travel_date_or_season:
        queries.append(f"{region} {intent.travel_date_or_season} 徒步路线 公告")
    return _dedupe_strings(queries)[:4]


def _merge_candidates(candidates: list[RawRouteCandidate]) -> list[RawRouteCandidate]:
    merged: OrderedDict[str, RawRouteCandidate] = OrderedDict()
    alias_index: dict[str, str] = {}
    for candidate in candidates:
        names = [_normalized_name(candidate.name), *(_normalized_name(item) for item in candidate.aliases)]
        names = [item for item in names if item]
        key = next((alias_index[name] for name in names if name in alias_index), names[0] if names else "")
        if not key:
            continue
        if key not in merged:
            merged[key] = candidate.model_copy(deep=True)
        else:
            current = merged[key]
            current.aliases = _dedupe_strings([*current.aliases, *candidate.aliases, candidate.name])
            current.evidence = _dedupe_evidence([*current.evidence, *candidate.evidence])
            current.scenery = _dedupe_strings([*current.scenery, *candidate.scenery])
            current.seasons = _dedupe_strings([*current.seasons, *candidate.seasons])
            current.transport_notes = _dedupe_strings([*current.transport_notes, *candidate.transport_notes])
            current.verification_items = _dedupe_strings([*current.verification_items, *candidate.verification_items])
            for field in ("summary", "difficulty", "distance_km", "duration_hours", "ascent_m", "camping"):
                if getattr(current, field) is None and getattr(candidate, field) is not None:
                    setattr(current, field, getattr(candidate, field))
            if candidate.retrieval_source == "knowledge_base":
                current.retrieval_source = candidate.retrieval_source
                current.popularity_label = candidate.popularity_label
                current.last_verified_at = candidate.last_verified_at
                current.official_status = candidate.official_status
                current.editorial_rank = candidate.editorial_rank
                current.risk_level = candidate.risk_level
        for name in names:
            alias_index[name] = key
    return list(merged.values())


def _score_candidate(
    intent: RouteRecommendationIntent,
    raw: RawRouteCandidate,
    place: VerifiedRoutePlace | None,
    independent_domain_count: int,
) -> RouteRecommendationCandidate:
    score = 0.0
    reasons: list[str] = []
    mismatches: list[str] = []
    unknown: list[str] = []

    if intent.destination_region:
        if place is not None or _contains_text(raw.region, intent.destination_region):
            score += 30
            reasons.append(f"位于或匹配「{intent.destination_region}」范围")
        else:
            mismatches.append("地区匹配仍需核验")
    else:
        unknown.append("目的地区域")

    difficulty_score, difficulty_reason, difficulty_mismatch = _difficulty_score(intent, raw)
    score += difficulty_score
    if difficulty_reason:
        reasons.append(difficulty_reason)
    if difficulty_mismatch:
        mismatches.append(difficulty_mismatch)
    if raw.difficulty is None:
        unknown.append("难度")

    if intent.max_duration_hours is not None:
        if raw.duration_hours is None:
            unknown.append("徒步耗时")
        elif raw.duration_hours <= intent.max_duration_hours:
            score += 15
            reasons.append(f"公开资料耗时不超过 {intent.max_duration_hours:g} 小时")
        else:
            mismatches.append("公开资料耗时超过期望")
    elif intent.trip_days is not None and raw.duration_hours is not None:
        if raw.duration_hours <= intent.trip_days * 8:
            score += 15
            reasons.append("公开资料耗时与行程天数相符")
        else:
            mismatches.append("公开资料耗时可能超过行程安排")
    else:
        unknown.append("时间匹配")

    requested_scenery = {_normalized_name(item) for item in intent.scenery_preferences if item}
    candidate_scenery = {_normalized_name(item) for item in raw.scenery if item}
    matches = {item for item in requested_scenery if any(item in value or value in item for value in candidate_scenery)}
    if requested_scenery:
        if matches:
            ratio = min(1.0, len(matches) / len(requested_scenery))
            score += 15 * ratio
            reasons.append("景观偏好有公开资料支持")
        elif not candidate_scenery:
            unknown.append("景观特征")
        else:
            mismatches.append("暂未发现期望景观的证据")

    if intent.transport_preference:
        transport_text = " ".join(raw.transport_notes)
        if transport_text and _transport_matches(intent.transport_preference, transport_text):
            score += 10
            reasons.append("交通方式与偏好匹配")
        elif transport_text:
            mismatches.append("交通方式可能不匹配")
        else:
            unknown.append("交通可达性")

    evidence_score = 10 if place is not None and raw.evidence else min(10, independent_domain_count * 4)
    score += evidence_score
    reasons.append(f"由 {independent_domain_count} 个独立网页来源支持" + ("，并经高德地点核验" if place else ""))

    if raw.retrieval_source == "knowledge_base":
        score += min(6, raw.editorial_rank * 2)
        if raw.popularity_label:
            reasons.append(f"路线库标记为「{raw.popularity_label}」")
        if raw.official_status and raw.official_status != "unverified":
            score += 4
            reasons.append("具有可追溯的官方收录信息")

    for field, label in (
        (raw.distance_km, "距离"),
        (raw.duration_hours, "耗时"),
        (raw.ascent_m, "累计爬升"),
    ):
        if field is None and label not in unknown:
            unknown.append(label)

    verification_items = _dedupe_strings(
        [
            *raw.verification_items,
            "上传与该路线对应的 KML 后，再进行距离、海拔、天气和风险分析。",
            "出发前核对官方开放公告、临时封闭和防火要求。",
        ]
    )
    if place is None:
        verification_items.insert(0, "高德未确认到同名地点，请核对路线所在地区和起终点。")

    confidence = min(
        0.92,
        0.35
        + min(independent_domain_count, 3) * 0.14
        + (0.2 if place else 0)
        + (0.15 if raw.retrieval_source == "knowledge_base" else 0),
    )
    region = place.region if place is not None else raw.region
    return RouteRecommendationCandidate(
        id=_candidate_id(raw.name, region),
        name=raw.name,
        region=region,
        coordinate=place.coordinate if place else None,
        coordinate_system=place.coordinate_system if place else None,
        match_score=min(100, round(score)),
        confidence=round(confidence, 2),
        summary=raw.summary,
        difficulty=raw.difficulty,
        distance_km=raw.distance_km,
        duration_hours=raw.duration_hours,
        ascent_m=raw.ascent_m,
        scenery=raw.scenery,
        seasons=raw.seasons,
        transport_notes=raw.transport_notes,
        match_reasons=_dedupe_strings(reasons),
        mismatches=_dedupe_strings(mismatches),
        unknown_fields=_dedupe_strings(unknown),
        evidence=raw.evidence,
        verification_items=verification_items,
        retrieval_source="knowledge_base" if raw.retrieval_source == "knowledge_base" else "live_web",
        popularity_label=raw.popularity_label,
        last_verified_at=raw.last_verified_at,
        official_status=raw.official_status,
    )


def _hard_conflict(intent: RouteRecommendationIntent, candidate: RawRouteCandidate) -> bool:
    if intent.fitness_level == "beginner" and _difficulty_rank(candidate.difficulty) >= 3:
        return True
    if intent.max_distance_km is not None and candidate.distance_km is not None:
        if candidate.distance_km > intent.max_distance_km:
            return True
    if intent.min_distance_km is not None and candidate.distance_km is not None:
        if candidate.distance_km < intent.min_distance_km:
            return True
    if intent.max_duration_hours is not None and candidate.duration_hours is not None:
        if candidate.duration_hours > intent.max_duration_hours:
            return True
    if intent.max_ascent_m is not None and candidate.ascent_m is not None:
        if candidate.ascent_m > intent.max_ascent_m:
            return True
    if intent.camping_preference is not None and candidate.camping is not None:
        if candidate.camping is not intent.camping_preference:
            return True
    text = " ".join([candidate.name, candidate.region, candidate.summary or "", *candidate.scenery])
    return any(exclusion and _normalized_name(exclusion) in _normalized_name(text) for exclusion in intent.exclusions)


def _difficulty_score(
    intent: RouteRecommendationIntent,
    candidate: RawRouteCandidate,
) -> tuple[float, str | None, str | None]:
    if not intent.fitness_level or not candidate.difficulty:
        return 0, None, None
    target = {"beginner": 1, "intermediate": 2, "advanced": 3}[intent.fitness_level]
    actual = _difficulty_rank(candidate.difficulty)
    if actual == 0:
        return 0, None, None
    if actual == target:
        return 20, "公开资料中的难度与体能等级匹配", None
    if actual < target:
        return 15, "路线难度不高于当前体能等级", None
    return 0, None, "路线难度可能高于当前体能等级"


def _difficulty_rank(value: str | None) -> int:
    text = (value or "").lower()
    if any(item in text for item in ("困难", "高难", "进阶", "advanced", "expert")):
        return 3
    if any(item in text for item in ("中等", "适中", "intermediate", "moderate")):
        return 2
    if any(item in text for item in ("简单", "轻松", "入门", "新手", "beginner", "easy")):
        return 1
    return 0


def _clarifying_question(intent: RouteRecommendationIntent) -> RouteRecommendationQuestion | None:
    states = intent.field_states
    if states.get("destination_region") == IntentFieldState.UNKNOWN:
        return RouteRecommendationQuestion(
            id="destination_region",
            text="你希望从哪个城市出发，或优先搜索哪个地区周边？",
            options=["本市周边", "高铁 2 小时内", "全国均可"],
        )
    if states.get("trip_days") == IntentFieldState.UNKNOWN and states.get("max_duration_hours") == IntentFieldState.UNKNOWN:
        return RouteRecommendationQuestion(
            id="max_duration_hours",
            text="你希望单次徒步控制在多长时间内？",
            options=["4 小时内", "6 小时内", "8 小时内"],
        )
    if states.get("fitness_level") == IntentFieldState.UNKNOWN:
        return RouteRecommendationQuestion(
            id="fitness_level",
            text="你的徒步经验更接近哪一种？",
            options=["新手", "有一定经验", "经验丰富"],
        )
    if states.get("transport_preference") == IntentFieldState.UNKNOWN:
        return RouteRecommendationQuestion(
            id="transport_preference",
            text="你更希望如何抵达徒步起点？",
            options=["公共交通优先", "可以自驾", "都可以"],
        )
    return None


def _independent_domains(candidate: RawRouteCandidate) -> set[str]:
    domains: set[str] = set()
    for evidence in candidate.evidence:
        domain = (urlparse(evidence.url).hostname or "").lower()
        if domain.startswith("www."):
            domain = domain[4:]
        if domain:
            domains.add(domain)
    return domains


def _dedupe_evidence(items):
    seen: set[str] = set()
    result = []
    for item in items:
        if item.url in seen:
            continue
        seen.add(item.url)
        result.append(item)
    return result


def _transport_matches(preference: str, notes: str) -> bool:
    preference = preference.lower()
    notes = notes.lower()
    if any(item in preference for item in ("公共", "公交", "高铁", "地铁", "bus", "train")):
        return any(item in notes for item in ("公交", "高铁", "地铁", "班车", "公共交通", "火车"))
    if any(item in preference for item in ("自驾", "开车", "drive")):
        return any(item in notes for item in ("自驾", "停车", "驾车"))
    return bool(notes)


def _contains_text(value: str, target: str) -> bool:
    left = _normalized_name(value)
    right = _normalized_name(target)
    return bool(left and right and (left in right or right in left))


def _normalized_name(value: str) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]", "", value.lower())


def _candidate_id(name: str, region: str) -> str:
    normalized = _normalized_name(name + region)
    checksum = sum((index + 1) * ord(char) for index, char in enumerate(normalized)) % 10_000_000
    return f"route-{checksum:07d}"


def _dedupe_strings(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        value = str(item).strip()
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result
