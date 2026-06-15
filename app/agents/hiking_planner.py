from __future__ import annotations

from typing import Any, TypedDict

from app.models import (
    HikingGuideRequest,
    HikingGuideResponse,
    RouteCandidate,
    RouteGeometry,
    RouteSource,
    TransportPlan,
    TravelResearch,
)
from app.providers.llm import BailianQwenGuideProvider, GuideLLMProvider, TemplateGuideProvider
from app.providers.transport import AmapTransportProvider, StaticTransportProvider, TransportPlanningProvider
from app.providers.travel_research import DefaultTravelResearchProvider, TravelResearchProvider
from app.providers.weather import NoopWeatherProvider, WeatherProvider
from app.providers.weather import WeatherSnapshot
from app.services.route_analysis import RouteAnalysisService
from app.services.route_ingestion import RouteIngestionError, RouteIngestionService
from app.services.route_planner import RoutePlannerService

try:
    from langgraph.graph import END, StateGraph
except ModuleNotFoundError:  # pragma: no cover - exercised implicitly when absent.
    END = "__end__"
    StateGraph = None


class HikingPlannerState(TypedDict, total=False):
    request: HikingGuideRequest
    route_file_content: bytes | None
    routes: list[RouteGeometry]
    candidates: list[RouteCandidate]
    weather_snapshots: list[WeatherSnapshot | None]
    travel_research: TravelResearch | None
    transport_plan: TransportPlan | None
    warnings: list[str]
    data_sources: list[str]
    response: HikingGuideResponse


class HikingGuideAgent:
    def __init__(
        self,
        ingestion_service: RouteIngestionService | None = None,
        planner_service: RoutePlannerService | None = None,
        analysis_service: RouteAnalysisService | None = None,
        weather_provider: WeatherProvider | None = None,
        travel_research_provider: TravelResearchProvider | None = None,
        transport_provider: TransportPlanningProvider | None = None,
        llm_provider: GuideLLMProvider | None = None,
    ) -> None:
        self.ingestion_service = ingestion_service or RouteIngestionService()
        self.planner_service = planner_service or RoutePlannerService()
        self.analysis_service = analysis_service or RouteAnalysisService()
        self.weather_provider = weather_provider or NoopWeatherProvider()
        self.travel_research_provider = travel_research_provider or DefaultTravelResearchProvider()
        self.transport_provider = transport_provider or StaticTransportProvider()
        self.template_provider = TemplateGuideProvider()
        self.llm_provider = llm_provider or BailianQwenGuideProvider()
        self.graph = self._build_graph()

    def generate(
        self,
        request: HikingGuideRequest,
        route_file_content: bytes | None = None,
    ) -> HikingGuideResponse:
        initial_state: HikingPlannerState = {
            "request": request,
            "route_file_content": route_file_content,
            "routes": [],
            "candidates": [],
            "weather_snapshots": [],
            "travel_research": None,
            "warnings": [],
            "data_sources": [],
        }
        final_state = self.graph.invoke(initial_state)
        return final_state["response"]

    def _build_graph(self) -> Any:
        if StateGraph is None:
            return _SequentialPlannerGraph(self)

        graph = StateGraph(HikingPlannerState)
        graph.add_node("ingest_route", self._ingest_route)
        graph.add_node("plan_route", self._plan_route)
        graph.add_node("analyze_route", self._analyze_route)
        graph.add_node("collect_research", self._collect_research)
        graph.add_node("plan_transport", self._plan_transport)
        graph.add_node("compose_guide", self._compose_guide)

        graph.set_entry_point("ingest_route")
        graph.add_conditional_edges(
            "ingest_route",
            self._route_after_ingestion,
            {"analyze_route": "analyze_route", "plan_route": "plan_route"},
        )
        graph.add_edge("plan_route", "analyze_route")
        graph.add_edge("analyze_route", "collect_research")
        graph.add_edge("collect_research", "plan_transport")
        graph.add_edge("plan_transport", "compose_guide")
        graph.add_edge("compose_guide", END)
        return graph.compile()

    def _route_after_ingestion(self, state: HikingPlannerState) -> str:
        return "analyze_route" if state.get("routes") else "plan_route"

    def _ingest_route(self, state: HikingPlannerState) -> HikingPlannerState:
        content = state.get("route_file_content")
        if not content:
            return state

        warnings = list(state.get("warnings", []))
        data_sources = list(state.get("data_sources", []))
        try:
            routes = self.ingestion_service.parse_kml(content)
        except RouteIngestionError as exc:
            warnings.append(f"KML 解析失败，已降级为 API 规划：{exc}")
            return {**state, "warnings": warnings}

        data_sources.append("用户提供 KML")
        return {**state, "routes": routes, "warnings": warnings, "data_sources": data_sources}

    def _plan_route(self, state: HikingPlannerState) -> HikingPlannerState:
        request = state["request"]
        routes = self.planner_service.plan(request.destination, request.route_text)
        data_sources = list(state.get("data_sources", []))
        if request.route_text:
            data_sources.append("用户路线文本 + 路由 API/兜底规划")
        else:
            data_sources.append("路由 API/兜底规划")
        return {**state, "routes": routes, "data_sources": data_sources}

    def _analyze_route(self, state: HikingPlannerState) -> HikingPlannerState:
        candidates: list[RouteCandidate] = []
        weather_snapshots: list[WeatherSnapshot | None] = []
        warnings = list(state.get("warnings", []))
        data_sources = list(state.get("data_sources", []))

        for route in state.get("routes", []):
            weather = None
            try:
                weather = self.weather_provider.forecast(route.start)
                if weather and weather.source != "unknown":
                    data_sources.append(f"天气：{weather.source}")
            except Exception as exc:  # noqa: BLE001 - external provider should degrade.
                warnings.append(f"天气服务暂不可用：{exc}")
            weather_snapshots.append(weather)

            analyzed_route, analysis = self.analysis_service.analyze(route, weather)
            warnings.extend(analysis.warnings)
            candidates.append(
                RouteCandidate(
                    route=analyzed_route,
                    analysis=analysis,
                    label=self._route_label(analyzed_route.source),
                )
            )

        return {
            **state,
            "candidates": candidates,
            "weather_snapshots": weather_snapshots,
            "warnings": _dedupe(warnings),
            "data_sources": _dedupe(data_sources),
        }

    def _collect_research(self, state: HikingPlannerState) -> HikingPlannerState:
        request = state["request"]
        candidates = state.get("candidates", [])
        warnings = list(state.get("warnings", []))
        data_sources = list(state.get("data_sources", []))

        try:
            travel_research = self.travel_research_provider.collect(
                request,
                candidates,
                state.get("weather_snapshots", []),
            )
            data_sources.append("行前信息搜集")
            warnings.extend(travel_research.warnings)
        except Exception as exc:  # noqa: BLE001 - research failure should degrade.
            travel_research = None
            warnings.append(f"行前信息搜集暂不可用：{exc}")

        return {
            **state,
            "travel_research": travel_research,
            "warnings": _dedupe(warnings),
            "data_sources": _dedupe(data_sources),
        }

    def _plan_transport(self, state: HikingPlannerState) -> HikingPlannerState:
        request = state["request"]
        candidates = state.get("candidates", [])
        warnings = list(state.get("warnings", []))
        data_sources = list(state.get("data_sources", []))

        # 没有出发城市或没有路线候选，则跳过
        if not request.start_city or not candidates:
            return {**state, "transport_plan": None}

        # 取首选路线起点作为目的地坐标
        dest_coord = candidates[0].route.start
        dest_name = candidates[0].route.name

        try:
            transport_plan = self.transport_provider.plan(
                start_city=request.start_city,
                destination_coordinate=dest_coord,
                destination_name=dest_name,
            )
            data_sources.append("交通规划")
            warnings.extend(transport_plan.warnings)
        except Exception as exc:  # noqa: BLE001 - transport failure should degrade.
            transport_plan = None
            warnings.append(f"交通规划暂不可用：{exc}")

        return {
            **state,
            "transport_plan": transport_plan,
            "warnings": _dedupe(warnings),
            "data_sources": _dedupe(data_sources),
        }

    def _compose_guide(self, state: HikingPlannerState) -> HikingPlannerState:
        request = state["request"]
        candidates = state.get("candidates", [])
        warnings = list(state.get("warnings", []))
        data_sources = list(state.get("data_sources", []))
        travel_research = state.get("travel_research")
        transport_plan = state.get("transport_plan")
        try:
            draft = self.llm_provider.generate_guide(
                request,
                candidates,
                warnings,
                data_sources,
                travel_research=travel_research,
                transport_plan=transport_plan,
            )
            data_sources.append(draft.source)
        except Exception as exc:  # noqa: BLE001 - LLM outages should degrade to templates.
            warnings.append(f"百炼模型暂不可用，已使用模板生成攻略：{exc}")
            draft = self.template_provider.generate_guide(
                request,
                candidates,
                warnings,
                data_sources,
                travel_research=travel_research,
                transport_plan=transport_plan,
            )
            data_sources.append(draft.source)

        response = HikingGuideResponse(
            destination=request.destination,
            summary=draft.summary,
            route_candidates=candidates,
            travel_research=travel_research,
            transport_plan=transport_plan,
            itinerary=draft.itinerary,
            gear_list=draft.gear_list,
            safety_guide=draft.safety_guide,
            recommendations=draft.recommendations,
            data_sources=_dedupe(data_sources),
            warnings=_dedupe(warnings),
            disclaimer="本攻略为行前规划建议，不可替代专业导航、景区公告、封山/防火通知或救援信息。",
        )
        return {**state, "response": response}

    def _route_label(self, source: RouteSource) -> str:
        labels = {
            RouteSource.USER_KML: "用户提供轨迹",
            RouteSource.USER_TEXT_PLANNED: "用户描述 + 规划路线",
            RouteSource.API_PLANNED: "API 规划路线",
        }
        return labels[source]


class _SequentialPlannerGraph:
    def __init__(self, agent: HikingGuideAgent) -> None:
        self.agent = agent

    def invoke(self, state: HikingPlannerState) -> HikingPlannerState:
        state = self.agent._ingest_route(state)
        if not state.get("routes"):
            state = self.agent._plan_route(state)
        state = self.agent._analyze_route(state)
        state = self.agent._collect_research(state)
        state = self.agent._plan_transport(state)
        return self.agent._compose_guide(state)


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result
