from __future__ import annotations

from collections.abc import Iterator
from typing import Any, TypedDict

from app.models import (
    AgentTraceEvent,
    Coordinate,
    GuideDecision,
    HikingGuideRequest,
    HikingGuideResponse,
    GuideToolPlan,
    GuideReferenceResearch,
    RouteCandidate,
    RouteGeometry,
    RouteSource,
    TransportPlan,
    TravelResearch,
    WeatherDetail,
)
from app.agents.tools import GuideToolRegistry
from app.providers.llm import (
    BailianQwenGuideProvider,
    GuideLLMProvider,
    GuidePlanningProvider,
    StaticGuidePlanningProvider,
    TemplateGuideProvider,
)
from app.providers.guide_reference import DefaultGuideReferenceProvider, GuideReferenceProvider
from app.providers.transport import StaticTransportProvider, TransportPlanningProvider
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
    tool_plan: GuideToolPlan
    guide_decision: GuideDecision
    weather_snapshots: list[WeatherSnapshot | None]
    weather_details: list[WeatherDetail]
    travel_research: TravelResearch | None
    reference_research: GuideReferenceResearch | None
    transport_plan: TransportPlan | None
    warnings: list[str]
    data_sources: list[str]
    llm_usage: list[str]
    clarifying_questions: list[str]
    validation_notes: list[str]
    agent_trace: list[AgentTraceEvent]
    response: HikingGuideResponse


class HikingGuideAgent:
    def __init__(
        self,
        ingestion_service: RouteIngestionService | None = None,
        planner_service: RoutePlannerService | None = None,
        analysis_service: RouteAnalysisService | None = None,
        weather_provider: WeatherProvider | None = None,
        travel_research_provider: TravelResearchProvider | None = None,
        guide_reference_provider: GuideReferenceProvider | None = None,
        transport_provider: TransportPlanningProvider | None = None,
        planning_provider: GuidePlanningProvider | None = None,
        llm_provider: GuideLLMProvider | None = None,
    ) -> None:
        self.ingestion_service = ingestion_service or RouteIngestionService()
        self.planner_service = planner_service or RoutePlannerService()
        self.analysis_service = analysis_service or RouteAnalysisService()
        self.weather_provider = weather_provider or NoopWeatherProvider()
        self.travel_research_provider = travel_research_provider or DefaultTravelResearchProvider()
        self.guide_reference_provider = guide_reference_provider or DefaultGuideReferenceProvider()
        self.transport_provider = transport_provider or StaticTransportProvider()
        self.planning_provider = planning_provider or BailianQwenGuideProvider()
        self.static_planning_provider = StaticGuidePlanningProvider()
        self.template_provider = TemplateGuideProvider()
        self.llm_provider = llm_provider or BailianQwenGuideProvider()
        self.tools = GuideToolRegistry(
            weather_provider=self.weather_provider,
            travel_research_provider=self.travel_research_provider,
            transport_provider=self.transport_provider,
            template_provider=self.template_provider,
        )
        self.graph = self._build_graph()

    def generate(
        self,
        request: HikingGuideRequest,
        route_file_content: bytes | None = None,
    ) -> HikingGuideResponse:
        final_state = self.graph.invoke(self._initial_state(request, route_file_content))
        return final_state["response"]

    def generate_events(
        self,
        request: HikingGuideRequest,
        route_file_content: bytes | None = None,
    ) -> Iterator[dict[str, Any]]:
        state = self._initial_state(request, route_file_content)

        def apply_step(step_name: str, step: Any) -> None:
            nonlocal state
            before = len(state.get("agent_trace", []))
            state = step(state)
            for event in state.get("agent_trace", [])[before:]:
                yield_event = event.model_dump(mode="json")
                yield_event["event"] = "trace"
                yield_event["step"] = step_name
                yield yield_event

        for event in apply_step("ingest_route", self._ingest_route):
            yield event
        if not state.get("routes"):
            for event in apply_step("plan_route", self._plan_route):
                yield event
        for name, step in [
            ("plan_tools", self._plan_tools),
            ("collect_weather", self._collect_weather),
            ("analyze_route", self._analyze_route),
            ("collect_research", self._collect_research),
            ("plan_transport", self._plan_transport),
            ("compose_guide", self._compose_guide),
            ("collect_reference", self._collect_reference),
        ]:
            for event in apply_step(name, step):
                yield event

        yield {
            "event": "final",
            "response": state["response"].model_dump(mode="json"),
        }

    def _initial_state(
        self,
        request: HikingGuideRequest,
        route_file_content: bytes | None = None,
    ) -> HikingPlannerState:
        return {
            "request": request,
            "route_file_content": route_file_content,
            "routes": [],
            "candidates": [],
            "weather_snapshots": [],
            "weather_details": [],
            "travel_research": None,
            "reference_research": None,
            "warnings": [],
            "data_sources": [],
            "llm_usage": [],
            "clarifying_questions": [],
            "validation_notes": [],
            "agent_trace": [],
        }

    def _build_graph(self) -> Any:
        if StateGraph is None:
            return _SequentialPlannerGraph(self)

        graph = StateGraph(HikingPlannerState)
        graph.add_node("ingest_route", self._ingest_route)
        graph.add_node("plan_route", self._plan_route)
        graph.add_node("plan_tools", self._plan_tools)
        graph.add_node("collect_weather", self._collect_weather)
        graph.add_node("analyze_route", self._analyze_route)
        graph.add_node("collect_research", self._collect_research)
        graph.add_node("plan_transport", self._plan_transport)
        graph.add_node("compose_guide", self._compose_guide)
        graph.add_node("collect_reference", self._collect_reference)

        graph.set_entry_point("ingest_route")
        graph.add_conditional_edges(
            "ingest_route",
            self._route_after_ingestion,
            {"plan_tools": "plan_tools", "plan_route": "plan_route"},
        )
        graph.add_edge("plan_route", "plan_tools")
        graph.add_edge("plan_tools", "collect_weather")
        graph.add_edge("collect_weather", "analyze_route")
        graph.add_edge("analyze_route", "collect_research")
        graph.add_edge("collect_research", "plan_transport")
        graph.add_edge("plan_transport", "compose_guide")
        graph.add_edge("compose_guide", "collect_reference")
        graph.add_edge("collect_reference", END)
        return graph.compile()

    def _route_after_ingestion(self, state: HikingPlannerState) -> str:
        return "plan_tools" if state.get("routes") else "plan_route"

    def _ingest_route(self, state: HikingPlannerState) -> HikingPlannerState:
        content = state.get("route_file_content")
        if not content:
            return _with_trace(
                state,
                AgentTraceEvent(
                    phase="route",
                    title="检查路线输入",
                    status="skipped",
                    detail="未上传 KML，planner 将尝试基于目的地规划路线候选。",
                    tool_name="route_ingestion",
                ),
            )

        warnings = list(state.get("warnings", []))
        data_sources = list(state.get("data_sources", []))
        try:
            routes = self.ingestion_service.parse_kml(content)
        except RouteIngestionError as exc:
            warnings.append(f"KML 解析失败，已降级为 API 规划：{exc}")
            return _with_trace(
                {**state, "warnings": warnings},
                AgentTraceEvent(
                    phase="route",
                    title="解析 KML 失败",
                    status="fallback",
                    detail="KML 无法解析，改用路线规划工具生成候选。",
                    tool_name="route_ingestion",
                ),
            )

        data_sources.append("用户提供 KML")
        return _with_trace(
            {**state, "routes": routes, "warnings": warnings, "data_sources": data_sources},
            AgentTraceEvent(
                phase="route",
                title="读取用户轨迹",
                status="completed",
                detail=f"已从 KML 解析出 {len(routes)} 段路线，优先使用用户轨迹。",
                tool_name="route_ingestion",
            ),
        )

    def _plan_route(self, state: HikingPlannerState) -> HikingPlannerState:
        request = state["request"]
        routes = self.planner_service.plan(request.destination, request.route_text)
        data_sources = list(state.get("data_sources", []))
        if request.route_text:
            data_sources.append("用户路线文本 + 路由 API/兜底规划")
        else:
            data_sources.append("路由 API/兜底规划")
        return _with_trace(
            {**state, "routes": routes, "data_sources": data_sources},
            AgentTraceEvent(
                phase="route",
                title="规划路线候选",
                status="completed",
                detail=f"基于目的地生成 {len(routes)} 条候选路线。",
                tool_name="route_planner",
                rationale=["没有可用 KML 时，先建立可分析的路线候选，再进入攻略生成流程。"],
            ),
        )

    def _plan_tools(self, state: HikingPlannerState) -> HikingPlannerState:
        request = state["request"]
        warnings = list(state.get("warnings", []))
        data_sources = list(state.get("data_sources", []))
        llm_usage = list(state.get("llm_usage", []))
        routes = state.get("routes", [])

        try:
            raw_decision = self.planning_provider.plan_tools(request, routes, warnings, data_sources)
            decision = self._normalize_decision(raw_decision, request, routes)
            llm_usage.append("planner:llm")
            data_sources.append("LLM 工具调度")
            trace_status = "completed"
            trace_detail = _tool_plan_detail(decision.tool_plan)
            trace_rationale = decision.tool_plan.rationale or [
                "Planner 已根据路线、出发城市、日期和偏好选择后续工具。"
            ]
        except Exception as exc:  # noqa: BLE001 - planner should degrade to code defaults.
            fallback_reason = _planner_fallback_reason(exc)
            decision = self._normalize_decision(
                self.static_planning_provider.plan_tools(request, routes, warnings, data_sources),
                request,
                routes,
            )
            warnings.append(f"LLM planner 暂不可用，已使用保守默认工具计划继续生成：{fallback_reason}")
            llm_usage.append("planner:default")
            trace_status = "fallback"
            trace_detail = _tool_plan_detail(decision.tool_plan)
            trace_rationale = [f"LLM planner 暂不可用，使用保守默认工具计划继续生成。原因：{fallback_reason}"]

        return _with_trace(
            {
                **state,
                "guide_decision": decision,
                "tool_plan": decision.tool_plan,
                "clarifying_questions": _dedupe([*state.get("clarifying_questions", []), *decision.clarifying_questions]),
                "validation_notes": _dedupe([
                    *state.get("validation_notes", []),
                    *decision.validation_notes,
                    *decision.priority_notes,
                ]),
                "warnings": _dedupe(warnings),
                "data_sources": _dedupe(data_sources),
                "llm_usage": _dedupe(llm_usage),
            },
            AgentTraceEvent(
                phase="planner",
                title="Planner 选择工具",
                status=trace_status,
                detail=trace_detail,
                tool_name="llm_tool_planner",
                rationale=trace_rationale,
            ),
        )

    def _collect_weather(self, state: HikingPlannerState) -> HikingPlannerState:
        tool_plan = state.get("tool_plan") or GuideToolPlan()
        weather_snapshots: list[WeatherSnapshot | None] = []
        weather_details: list[WeatherDetail] = []
        warnings = list(state.get("warnings", []))
        data_sources = list(state.get("data_sources", []))
        weather_cache: dict[str, tuple[WeatherSnapshot | None, list[WeatherDetail]]] = {}

        for route in state.get("routes", []):
            weather = None
            if tool_plan.query_weather:
                cache_key = _weather_cache_key(route)
                try:
                    if cache_key not in weather_cache:
                        weather_cache[cache_key] = self.tools.weather_tool(_weather_coordinate(route))
                        weather_details.extend(weather_cache[cache_key][1])
                    weather = weather_cache[cache_key][0]
                    if weather and weather.source != "unknown":
                        data_sources.append(f"天气：{weather.source}")
                except Exception as exc:  # noqa: BLE001 - external provider should degrade.
                    warnings.append(f"天气服务暂不可用：{exc}")
            weather_snapshots.append(weather)

        status = "completed" if tool_plan.query_weather else "skipped"
        detail = (
            f"已按路线中点查询天气，得到 {len(_dedupe_weather_details(weather_details))} 条逐日天气详情。"
            if tool_plan.query_weather
            else "Planner 判断本次不需要调用天气工具。"
        )
        return _with_trace(
            {
                **state,
                "weather_snapshots": weather_snapshots,
                "weather_details": _dedupe_weather_details(weather_details),
                "warnings": _dedupe(warnings),
                "data_sources": _dedupe(data_sources),
            },
            AgentTraceEvent(
                phase="tool",
                title="天气工具",
                status=status,
                detail=detail,
                tool_name="weather_tool",
            ),
        )

    def _analyze_route(self, state: HikingPlannerState) -> HikingPlannerState:
        candidates: list[RouteCandidate] = []
        warnings = list(state.get("warnings", []))
        weather_snapshots = state.get("weather_snapshots", [])

        for index, route in enumerate(state.get("routes", [])):
            weather = weather_snapshots[index] if index < len(weather_snapshots) else None
            analyzed_route, analysis = self.analysis_service.analyze(route, weather)
            warnings.extend(analysis.warnings)
            candidates.append(
                RouteCandidate(
                    route=analyzed_route,
                    analysis=analysis,
                    label=self._route_label(analyzed_route.source),
                )
            )

        return _with_trace(
            {
                **state,
                "candidates": candidates,
                "warnings": _dedupe(warnings),
            },
            AgentTraceEvent(
                phase="tool",
                title="路线分析",
                status="completed",
                detail=f"已计算 {len(candidates)} 条候选路线的距离、耗时、爬升和风险等级。",
                tool_name="route_analysis",
            ),
        )

    def _collect_research(self, state: HikingPlannerState) -> HikingPlannerState:
        request = state["request"]
        candidates = state.get("candidates", [])
        tool_plan = state.get("tool_plan") or GuideToolPlan()
        warnings = list(state.get("warnings", []))
        data_sources = list(state.get("data_sources", []))

        try:
            travel_research = self.tools.poi_tool(
                request,
                candidates,
                state.get("weather_snapshots", []),
                include_lodging=tool_plan.query_lodging,
                include_food=tool_plan.query_food,
                include_supply=tool_plan.query_supply,
            )
            data_sources.append("行前信息搜集")
            warnings.extend(travel_research.warnings)
        except Exception as exc:  # noqa: BLE001 - research failure should degrade.
            travel_research = None
            warnings.append(f"行前信息搜集暂不可用：{exc}")

        trace_status = "completed" if travel_research is not None else "fallback"
        return _with_trace(
            {
                **state,
                "travel_research": travel_research,
                "warnings": _dedupe(warnings),
                "data_sources": _dedupe(data_sources),
            },
            AgentTraceEvent(
                phase="tool",
                title="周边信息工具",
                status=trace_status,
                detail=_research_detail(tool_plan, travel_research),
                tool_name="poi_tool",
            ),
        )

    def _plan_transport(self, state: HikingPlannerState) -> HikingPlannerState:
        request = state["request"]
        candidates = state.get("candidates", [])
        tool_plan = state.get("tool_plan") or GuideToolPlan()
        warnings = list(state.get("warnings", []))
        data_sources = list(state.get("data_sources", []))

        # 没有出发城市或没有路线候选，则跳过
        if not tool_plan.query_transport or not request.start_city or not candidates:
            reason = "Planner 未选择交通工具。"
            if not request.start_city:
                reason = "缺少出发城市，交通规划已跳过。"
            elif not candidates:
                reason = "没有路线候选，交通规划已跳过。"
            return _with_trace(
                {**state, "transport_plan": None},
                AgentTraceEvent(
                    phase="tool",
                    title="交通规划工具",
                    status="skipped",
                    detail=reason,
                    tool_name="transport_options_tool",
                ),
            )

        # 取首选路线起点作为目的地坐标
        dest_coord = candidates[0].route.start
        dest_name = request.destination

        try:
            departure_date = request.date_range[0] if request.date_range else None
            transport_plan = self.tools.transport_options_tool(
                start_city=request.start_city,
                destination_coordinate=dest_coord,
                destination_name=dest_name,
                departure_date=departure_date,
            )
            data_sources.append("交通规划")
            warnings.extend(transport_plan.warnings)
        except Exception as exc:  # noqa: BLE001 - transport failure should degrade.
            transport_plan = None
            warnings.append(f"交通规划暂不可用：{exc}")

        return _with_trace(
            {
                **state,
                "transport_plan": transport_plan,
                "warnings": _dedupe(warnings),
                "data_sources": _dedupe(data_sources),
            },
            AgentTraceEvent(
                phase="tool",
                title="交通规划工具",
                status="completed" if transport_plan else "fallback",
                detail=(
                    f"已从 {request.start_city} 到 {dest_name} 生成 {len(transport_plan.options)} 个交通方案。"
                    if transport_plan
                    else "交通工具调用失败，攻略会保留核验提示。"
                ),
                tool_name="transport_options_tool",
            ),
        )

    def _compose_guide(self, state: HikingPlannerState) -> HikingPlannerState:
        request = state["request"]
        candidates = state.get("candidates", [])
        warnings = list(state.get("warnings", []))
        data_sources = list(state.get("data_sources", []))
        travel_research = state.get("travel_research")
        transport_plan = state.get("transport_plan")
        tool_plan = state.get("tool_plan") or GuideToolPlan()
        llm_usage = list(state.get("llm_usage", []))
        if not tool_plan.compose_with_llm:
            draft = self.tools.guide_composer_tool().generate_guide(
                request,
                candidates,
                warnings,
                data_sources,
                travel_research=travel_research,
                transport_plan=transport_plan,
            )
            data_sources.append(draft.source)
            llm_usage.append("composer:template")
            compose_status = "skipped"
            compose_detail = "Planner 关闭最终 LLM 写作，已使用模板生成结构化攻略。"
        else:
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
                llm_usage.append("composer:template" if draft.source == "template" else "composer:llm")
                compose_status = "completed" if draft.source != "template" else "fallback"
                compose_detail = (
                    "已把路线、天气、交通和周边信息交给 LLM 生成最终攻略。"
                    if draft.source != "template"
                    else "LLM provider 返回模板结果，已继续完成攻略。"
                )
            except Exception as exc:  # noqa: BLE001 - LLM outages should degrade to templates.
                warnings.append(f"百炼模型暂不可用，已使用模板生成攻略：{exc}")
                draft = self.tools.guide_composer_tool().generate_guide(
                    request,
                    candidates,
                    warnings,
                    data_sources,
                    travel_research=travel_research,
                    transport_plan=transport_plan,
                )
                data_sources.append(draft.source)
                llm_usage.append("composer:template")
                compose_status = "fallback"
                compose_detail = "最终写作 LLM 暂不可用，已使用模板生成攻略。"

        schedule_notes = []
        if candidates:
            schedule_context = self.template_provider.itinerary_planning_context(request, candidates)
            schedule_notes = list(schedule_context["notes"])
            expected_days = int(schedule_context["planned_days"])
            if draft.itinerary is None or draft.itinerary.total_days != expected_days:
                draft.itinerary = self.template_provider._template_itinerary(request, candidates, self.template_provider._aggregate_candidates(candidates))
                schedule_notes.append(f"已按路线强度和用户日期校准行程为 {expected_days} 天。")

        traced_state = _with_trace(
            state,
            AgentTraceEvent(
                phase="composer",
                title="生成最终攻略",
                status=compose_status,
                detail=compose_detail,
                tool_name="guide_composer",
            ),
        )
        response = HikingGuideResponse(
            destination=request.destination,
            summary=draft.summary,
            route_candidates=candidates,
            travel_research=travel_research,
            reference_research=state.get("reference_research"),
            transport_plan=transport_plan,
            weather_details=state.get("weather_details", []),
            itinerary=draft.itinerary,
            gear_list=draft.gear_list,
            safety_guide=draft.safety_guide,
            recommendations=draft.recommendations,
            data_sources=_dedupe(data_sources),
            llm_usage=_dedupe(llm_usage),
            agent_trace=traced_state.get("agent_trace", []),
            clarifying_questions=_dedupe(state.get("clarifying_questions", [])),
            validation_notes=_dedupe([*state.get("validation_notes", []), *schedule_notes]),
            warnings=_dedupe(warnings),
            disclaimer="本攻略为行前规划建议，不可替代专业导航、景区公告、封山/防火通知或救援信息。",
        )
        return {**traced_state, "response": response}

    def _collect_reference(self, state: HikingPlannerState) -> HikingPlannerState:
        request = state["request"]
        response = state["response"]
        if not request.reference_links and not request.reference_notes:
            return _with_response_trace(
                state,
                AgentTraceEvent(
                    phase="reference",
                    title="参考攻略补充",
                    status="skipped",
                    detail="用户未提供参考链接或摘录，跳过补充参考模块。",
                    tool_name="guide_reference",
                ),
            )

        warnings = list(response.warnings)
        data_sources = list(response.data_sources)
        llm_usage = list(response.llm_usage)
        try:
            reference_research = self.guide_reference_provider.collect(
                request,
                state.get("candidates", []),
                response.summary,
            )
        except Exception as exc:  # noqa: BLE001 - user-provided references must stay non-blocking.
            reference_research = GuideReferenceResearch(warnings=[f"参考攻略模块暂不可用：{exc}"])

        if reference_research is None:
            return _with_response_trace(
                state,
                AgentTraceEvent(
                    phase="reference",
                    title="参考攻略补充",
                    status="skipped",
                    detail="没有可用于补充的参考攻略材料。",
                    tool_name="guide_reference",
                ),
            )

        tool_plan = state.get("tool_plan") or GuideToolPlan()
        if tool_plan.compose_with_llm:
            try:
                reference_research = self.llm_provider.generate_reference_research(
                    request,
                    state.get("candidates", []),
                    response.summary,
                    reference_research,
                )
                llm_usage.append("reference:llm")
            except Exception as exc:  # noqa: BLE001 - reference LLM composition should degrade.
                reference_research.warnings.append(f"参考攻略 LLM 整合暂不可用，已保留结构化结果：{exc}")
                llm_usage.append("reference:template")
        else:
            reference_research = self.tools.guide_composer_tool().generate_reference_research(
                request,
                state.get("candidates", []),
                response.summary,
                reference_research,
            )
            llm_usage.append("reference:template")

        data_sources.append("用户提供参考攻略")
        warnings.extend(reference_research.warnings)
        updated_response = response.model_copy(
            update={
                "reference_research": reference_research,
                "data_sources": _dedupe(data_sources),
                "llm_usage": _dedupe(llm_usage),
                "warnings": _dedupe(warnings),
            }
        )
        traced_state = _with_trace(
            {**state, "reference_research": reference_research, "response": updated_response},
            AgentTraceEvent(
                phase="reference",
                title="参考攻略补充",
                status="completed",
                detail=f"已整合 {len(reference_research.items)} 份参考材料，并保留为补充线索。",
                tool_name="guide_reference",
            ),
        )
        refreshed_response = traced_state["response"].model_copy(
            update={"agent_trace": traced_state.get("agent_trace", [])}
        )
        return {**traced_state, "response": refreshed_response}

    def _route_label(self, source: RouteSource) -> str:
        labels = {
            RouteSource.USER_KML: "用户提供轨迹",
            RouteSource.USER_TEXT_PLANNED: "用户描述 + 规划路线",
            RouteSource.API_PLANNED: "API 规划路线",
        }
        return labels[source]

    def _normalize_decision(
        self,
        decision_or_plan: GuideDecision | GuideToolPlan,
        request: HikingGuideRequest,
        routes: list[RouteGeometry],
    ) -> GuideDecision:
        if isinstance(decision_or_plan, GuideDecision):
            decision = decision_or_plan
        else:
            decision = GuideDecision(tool_plan=decision_or_plan)
        plan = decision.tool_plan
        has_routes = bool(routes)
        normalized_plan = GuideToolPlan(
            query_weather=bool(plan.query_weather and has_routes),
            query_lodging=bool(plan.query_lodging and has_routes),
            query_food=bool(plan.query_food and has_routes),
            query_supply=bool(plan.query_supply and has_routes),
            query_transport=bool(plan.query_transport and has_routes and request.start_city),
            compose_with_llm=bool(plan.compose_with_llm),
            rationale=plan.rationale,
        )
        questions = list(decision.clarifying_questions)
        notes = list(decision.validation_notes)
        if has_routes and plan.query_transport and not request.start_city:
            questions.append("你从哪个城市出发？补充后可以细化机票、高铁和接驳方案。")
            notes.append("缺少出发城市，交通工具调用已跳过。")
        if has_routes and request.start_city and not request.date_range:
            questions.append("计划哪天出发、哪天返回？补充后可以查询机票报价并校准天气窗口。")
            notes.append("缺少出行日期，机票报价会降级为占位提示。")
        return GuideDecision(
            tool_plan=normalized_plan,
            clarifying_questions=_dedupe(questions),
            validation_notes=_dedupe(notes),
            priority_notes=_dedupe(decision.priority_notes),
        )


class _SequentialPlannerGraph:
    def __init__(self, agent: HikingGuideAgent) -> None:
        self.agent = agent

    def invoke(self, state: HikingPlannerState) -> HikingPlannerState:
        state = self.agent._ingest_route(state)
        if not state.get("routes"):
            state = self.agent._plan_route(state)
        state = self.agent._plan_tools(state)
        state = self.agent._collect_weather(state)
        state = self.agent._analyze_route(state)
        state = self.agent._collect_research(state)
        state = self.agent._plan_transport(state)
        state = self.agent._compose_guide(state)
        return self.agent._collect_reference(state)


def _with_trace(state: HikingPlannerState, event: AgentTraceEvent) -> HikingPlannerState:
    return {**state, "agent_trace": [*state.get("agent_trace", []), event]}


def _with_response_trace(state: HikingPlannerState, event: AgentTraceEvent) -> HikingPlannerState:
    traced_state = _with_trace(state, event)
    response = traced_state.get("response")
    if response is None:
        return traced_state
    return {
        **traced_state,
        "response": response.model_copy(update={"agent_trace": traced_state.get("agent_trace", [])}),
    }


def _tool_plan_detail(plan: GuideToolPlan) -> str:
    selected: list[str] = []
    skipped: list[str] = []
    labels = [
        ("query_weather", "天气"),
        ("query_lodging", "住宿"),
        ("query_food", "餐饮"),
        ("query_supply", "补给"),
        ("query_transport", "交通"),
        ("compose_with_llm", "LLM 写作"),
    ]
    for attr, label in labels:
        if getattr(plan, attr):
            selected.append(label)
        else:
            skipped.append(label)
    selected_text = "、".join(selected) if selected else "无"
    skipped_text = "；跳过：" + "、".join(skipped) if skipped else ""
    return f"将调用：{selected_text}{skipped_text}。"


def _research_detail(tool_plan: GuideToolPlan, research: TravelResearch | None) -> str:
    requested = [
        label
        for enabled, label in [
            (tool_plan.query_lodging, "住宿"),
            (tool_plan.query_food, "餐饮"),
            (tool_plan.query_supply, "补给"),
        ]
        if enabled
    ]
    if research is None:
        return "周边信息工具不可用，攻略会继续生成并保留核验提示。"
    counts = [
        f"住宿 {len(research.lodging)}",
        f"餐饮 {len(research.food)}",
        f"补给 {len(research.supply)}",
    ]
    return f"Planner 请求：{'、'.join(requested) if requested else '无'}；返回：{'、'.join(counts)}。"


def _planner_fallback_reason(exc: Exception) -> str:
    text = str(exc)
    if "DASHSCOPE_API_KEY" in text or "BAILIAN_API_KEY" in text:
        return "未配置 DASHSCOPE_API_KEY 或 BAILIAN_API_KEY"
    if "dashscope package is not installed" in text:
        return "未安装 dashscope 依赖"
    return text or exc.__class__.__name__


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _weather_coordinate(route: RouteGeometry) -> Coordinate:
    return route.coordinates[len(route.coordinates) // 2]


def _weather_cache_key(route: RouteGeometry) -> str:
    if route.source == RouteSource.USER_KML:
        return "user-kml-file"
    coordinate = _weather_coordinate(route)
    return f"{round(coordinate.lat, 2)}:{round(coordinate.lon, 2)}"


def _dedupe_weather_details(details: list[WeatherDetail]) -> list[WeatherDetail]:
    seen: set[tuple[object, ...]] = set()
    result: list[WeatherDetail] = []
    for detail in details:
        key = (
            detail.date,
            detail.source,
            detail.weather_text,
            detail.detail,
            detail.max_temp_c,
            detail.min_temp_c,
            detail.precipitation_probability,
            detail.max_wind_kmh,
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(detail)
    return result
