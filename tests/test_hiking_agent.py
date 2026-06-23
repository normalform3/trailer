from app.agents import HikingGuideAgent
from app.models import (
    Coordinate,
    GuideDecision,
    GuideReferenceResearch,
    GuideToolPlan,
    HikingGuideRequest,
    RouteGeometry,
    RouteSource,
    TransportOption,
    TransportPlan,
    TravelResearch,
    WeatherDetail,
)
from app.providers.llm import TemplateGuideProvider
from app.providers.llm import GuideDraft
from app.providers.transport import StaticTransportProvider
from app.providers.travel_research import StaticTravelResearchProvider
from app.providers.weather import WeatherSnapshot
from app.services.route_analysis import RouteAnalysisService


class RecordingPlanner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    def plan(self, destination: str, route_text: str | None = None) -> list[RouteGeometry]:
        self.calls.append((destination, route_text))
        return [
            RouteGeometry(
                name="规划路线",
                source=RouteSource.API_PLANNED,
                confidence=0.5,
                coordinates=[
                    Coordinate(lon=114.0, lat=27.0, elevation_m=100),
                    Coordinate(lon=114.01, lat=27.01, elevation_m=200),
                ],
            )
        ]


class FixedPlanningProvider:
    def __init__(self, plan: GuideToolPlan | GuideDecision) -> None:
        self.plan = plan

    def plan_tools(self, request, routes, warnings, data_sources) -> GuideToolPlan | GuideDecision:
        return self.plan


class FailingPlanningProvider:
    def plan_tools(self, request, routes, warnings, data_sources) -> GuideToolPlan:
        raise RuntimeError("planner down")


class StrongClaimLLMProvider:
    def generate_guide(self, *args, **kwargs):
        return GuideDraft(
            summary="这是社区热门路线，官方确认开放，住宿价格为 200 元。",
            recommendations=["当前仍有余房和余票。"],
            source="bailian:test",
        )


class FailingLLMProvider:
    def generate_guide(self, *args, **kwargs):
        raise RuntimeError("composer down")


class TwoDayLLMProvider:
    def generate_guide(self, *args, **kwargs):
        from app.models import DayPlan, Itinerary

        return GuideDraft(
            summary="LLM 给了两天。",
            recommendations=["核对天气"],
            source="bailian:test",
            itinerary=Itinerary(
                is_multi_day=True,
                total_days=2,
                days=[
                    DayPlan(day_number=1, title="Day 1", distance_km=6, key_segments=[]),
                    DayPlan(day_number=2, title="Day 2", distance_km=6, key_segments=[]),
                ],
            ),
        )


class RecordingWeatherProvider:
    def __init__(self) -> None:
        self.calls: list[Coordinate] = []

    def forecast(self, coordinate: Coordinate):
        self.calls.append(coordinate)
        return None

    def details(self, coordinate: Coordinate):
        return []


class DetailedRecordingWeatherProvider:
    def __init__(self) -> None:
        self.calls: list[Coordinate] = []
        self.detail_calls: list[Coordinate] = []

    def forecast(self, coordinate: Coordinate):
        self.calls.append(coordinate)
        return WeatherSnapshot(
            min_temp_c=18,
            max_temp_c=24,
            precipitation_probability=20,
            max_wind_kmh=12,
            weather_text="晴",
            source="fixed-weather",
        )

    def details(self, coordinate: Coordinate):
        self.detail_calls.append(coordinate)
        return [
            WeatherDetail(
                title="路线区域天气",
                detail="晴，气温 18-24 C，降水概率 20%",
                date="2026-06-16",
                weather_text="晴",
                min_temp_c=18,
                max_temp_c=24,
                precipitation_probability=20,
                max_wind_kmh=12,
                source="fixed-weather",
            )
        ]


class RecordingResearchProvider:
    def __init__(self) -> None:
        self.calls: list[dict[str, bool]] = []

    def collect(
        self,
        request,
        candidates,
        weather_snapshots,
        include_lodging: bool = True,
        include_food: bool = True,
        include_supply: bool = True,
    ) -> TravelResearch:
        self.calls.append(
            {
                "include_lodging": include_lodging,
                "include_food": include_food,
                "include_supply": include_supply,
            }
        )
        return TravelResearch()


class RecordingTransportProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Coordinate, str]] = []

    def plan(
        self,
        start_city: str,
        destination_coordinate: Coordinate,
        destination_name: str,
        departure_date=None,
    ) -> TransportPlan:
        self.calls.append((start_city, destination_coordinate, destination_name))
        return TransportPlan(
            start_city=start_city,
            destination_coordinate=destination_coordinate,
            destination_name=destination_name,
            options=[TransportOption(mode="mixed", source="test")],
        )


class RecordingGuideReferenceProvider:
    def __init__(self) -> None:
        self.calls = 0

    def collect(self, request, candidates, guide_summary):
        self.calls += 1
        return GuideReferenceResearch(
            supplemental_summary=f"参考补充：{guide_summary}",
            verification_items=["核验参考攻略中的住宿和补给信息"],
        )


def test_agent_prefers_user_kml_over_planner() -> None:
    planner = RecordingPlanner()
    agent = HikingGuideAgent(
        planner_service=planner,
        analysis_service=RouteAnalysisService(),
        travel_research_provider=StaticTravelResearchProvider(),
        transport_provider=StaticTransportProvider(),
    )
    kml = """
    <kml>
      <Placemark>
        <name>用户轨迹</name>
        <LineString><coordinates>114.1,27.1,800 114.2,27.2,900</coordinates></LineString>
      </Placemark>
    </kml>
    """.encode()

    response = agent.generate(HikingGuideRequest(destination="武功山"), route_file_content=kml)

    assert planner.calls == []
    assert response.route_candidates[0].route.name == "用户轨迹"
    assert response.route_candidates[0].label == "用户提供轨迹"
    assert response.travel_research is not None
    assert response.travel_research.lodging
    assert response.travel_research.next_steps[0].startswith("本次已以用户上传 KML")
    assert "用户提供 KML" in response.data_sources


def test_agent_skips_reference_module_without_reference_material() -> None:
    reference = RecordingGuideReferenceProvider()
    agent = HikingGuideAgent(
        planner_service=RecordingPlanner(),
        analysis_service=RouteAnalysisService(),
        travel_research_provider=StaticTravelResearchProvider(),
        transport_provider=StaticTransportProvider(),
        guide_reference_provider=reference,
        planning_provider=FixedPlanningProvider(GuideToolPlan(compose_with_llm=False)),
        llm_provider=TemplateGuideProvider(),
    )

    response = agent.generate(HikingGuideRequest(destination="武功山"))

    assert reference.calls == 0
    assert response.reference_research is None


def test_agent_adds_reference_research_without_changing_main_summary() -> None:
    base_agent = HikingGuideAgent(
        planner_service=RecordingPlanner(),
        analysis_service=RouteAnalysisService(),
        travel_research_provider=StaticTravelResearchProvider(),
        transport_provider=StaticTransportProvider(),
        planning_provider=FixedPlanningProvider(GuideToolPlan(compose_with_llm=False)),
        llm_provider=TemplateGuideProvider(),
    )
    reference = RecordingGuideReferenceProvider()
    reference_agent = HikingGuideAgent(
        planner_service=RecordingPlanner(),
        analysis_service=RouteAnalysisService(),
        travel_research_provider=StaticTravelResearchProvider(),
        transport_provider=StaticTransportProvider(),
        guide_reference_provider=reference,
        planning_provider=FixedPlanningProvider(GuideToolPlan(compose_with_llm=False)),
        llm_provider=TemplateGuideProvider(),
    )

    base = base_agent.generate(HikingGuideRequest(destination="武功山"))
    with_reference = reference_agent.generate(
        HikingGuideRequest(destination="武功山", reference_notes="攻略说山顶住宿紧张，需要提前核验。")
    )

    assert reference.calls == 1
    assert with_reference.summary == base.summary
    assert with_reference.itinerary == base.itinerary
    assert with_reference.reference_research is not None
    assert "用户提供参考攻略" in with_reference.data_sources


def test_agent_uses_planner_without_kml() -> None:
    planner = RecordingPlanner()
    agent = HikingGuideAgent(
        planner_service=planner,
        analysis_service=RouteAnalysisService(),
        travel_research_provider=StaticTravelResearchProvider(),
        transport_provider=StaticTransportProvider(),
    )

    response = agent.generate(HikingGuideRequest(destination="武功山"))

    assert planner.calls == [("武功山", None)]
    assert response.route_candidates[0].label == "API 规划路线"
    assert response.travel_research is not None
    assert response.travel_research.transport
    assert "路由 API/兜底规划" in response.data_sources


def test_agent_degrades_invalid_kml_to_planner() -> None:
    planner = RecordingPlanner()
    agent = HikingGuideAgent(
        planner_service=planner,
        analysis_service=RouteAnalysisService(),
        travel_research_provider=StaticTravelResearchProvider(),
        transport_provider=StaticTransportProvider(),
    )

    response = agent.generate(HikingGuideRequest(destination="武功山"), route_file_content=b"<kml>")

    assert planner.calls == [("武功山", None)]
    assert response.route_candidates[0].route.name == "规划路线"
    assert any("KML 解析失败" in warning for warning in response.warnings)


def test_agent_tool_plan_controls_weather_research_and_transport() -> None:
    planner = RecordingPlanner()
    weather = RecordingWeatherProvider()
    research = RecordingResearchProvider()
    transport = RecordingTransportProvider()
    agent = HikingGuideAgent(
        planner_service=planner,
        analysis_service=RouteAnalysisService(),
        weather_provider=weather,
        travel_research_provider=research,
        transport_provider=transport,
        planning_provider=FixedPlanningProvider(
            GuideToolPlan(
                query_weather=True,
                query_lodging=True,
                query_food=False,
                query_supply=True,
                query_transport=True,
            )
        ),
        llm_provider=TemplateGuideProvider(),
        enable_llm_planner=True,
    )

    response = agent.generate(HikingGuideRequest(destination="武功山", start_city="上海"))

    assert len(weather.calls) == 1
    assert research.calls == [
        {"include_lodging": True, "include_food": False, "include_supply": True}
    ]
    assert len(transport.calls) == 1
    assert transport.calls[0][2] == "武功山"
    assert response.llm_usage[0] == "planner:llm"
    assert response.agent_trace
    assert "Planner 选择工具" in [event.title for event in response.agent_trace]
    assert any(event.tool_name == "transport_options_tool" for event in response.agent_trace)


def test_agent_streams_trace_events_before_final_response() -> None:
    agent = HikingGuideAgent(
        planner_service=RecordingPlanner(),
        analysis_service=RouteAnalysisService(),
        travel_research_provider=StaticTravelResearchProvider(),
        transport_provider=StaticTransportProvider(),
        planning_provider=FixedPlanningProvider(GuideToolPlan(compose_with_llm=False)),
        llm_provider=TemplateGuideProvider(),
    )

    events = list(agent.generate_events(HikingGuideRequest(destination="武功山", start_city="上海")))

    assert events[0]["event"] == "trace"
    assert events[-1]["event"] == "final"
    assert events[-1]["response"]["summary"]
    assert events[-1]["response"]["agent_trace"]
    assert "Planner 选择工具" in [event["title"] for event in events if event["event"] == "trace"]


def test_agent_queries_weather_once_for_multi_route_kml() -> None:
    weather = DetailedRecordingWeatherProvider()
    agent = HikingGuideAgent(
        analysis_service=RouteAnalysisService(),
        weather_provider=weather,
        travel_research_provider=StaticTravelResearchProvider(),
        transport_provider=StaticTransportProvider(),
        planning_provider=FixedPlanningProvider(
            GuideToolPlan(
                query_weather=True,
                query_lodging=False,
                query_food=False,
                query_supply=False,
                query_transport=False,
                compose_with_llm=False,
            )
        ),
        llm_provider=TemplateGuideProvider(),
    )
    kml = """
    <kml>
      <Placemark>
        <name>第一段</name>
        <LineString><coordinates>114.1,27.1,800 114.2,27.2,900</coordinates></LineString>
      </Placemark>
      <Placemark>
        <name>第二段</name>
        <LineString><coordinates>114.2,27.2,900 114.3,27.3,850</coordinates></LineString>
      </Placemark>
    </kml>
    """.encode()

    response = agent.generate(HikingGuideRequest(destination="武功山"), route_file_content=kml)

    assert len(response.route_candidates) == 2
    assert len(weather.calls) == 1
    assert len(weather.detail_calls) == 1
    assert len(response.weather_details) == 1
    assert response.travel_research is not None
    assert len(response.travel_research.weather) == 1


def test_agent_does_not_plan_transport_without_start_city() -> None:
    transport = RecordingTransportProvider()
    agent = HikingGuideAgent(
        planner_service=RecordingPlanner(),
        analysis_service=RouteAnalysisService(),
        travel_research_provider=RecordingResearchProvider(),
        transport_provider=transport,
        planning_provider=FixedPlanningProvider(GuideToolPlan(query_transport=True)),
        llm_provider=TemplateGuideProvider(),
    )

    response = agent.generate(HikingGuideRequest(destination="武功山"))

    assert transport.calls == []
    assert response.transport_plan is None


def test_agent_uses_rules_planner_by_default_even_when_llm_planner_fails() -> None:
    weather = RecordingWeatherProvider()
    research = RecordingResearchProvider()
    agent = HikingGuideAgent(
        planner_service=RecordingPlanner(),
        analysis_service=RouteAnalysisService(),
        weather_provider=weather,
        travel_research_provider=research,
        transport_provider=RecordingTransportProvider(),
        planning_provider=FailingPlanningProvider(),
        llm_provider=TemplateGuideProvider(),
    )

    response = agent.generate(HikingGuideRequest(destination="武功山"))

    assert len(weather.calls) == 1
    assert research.calls == [
        {"include_lodging": True, "include_food": True, "include_supply": True}
    ]
    assert "planner:rules" in response.llm_usage
    assert not any("LLM planner 暂不可用" in warning for warning in response.warnings)


def test_agent_degrades_enabled_failing_tool_planner_to_rules_plan() -> None:
    weather = RecordingWeatherProvider()
    research = RecordingResearchProvider()
    agent = HikingGuideAgent(
        planner_service=RecordingPlanner(),
        analysis_service=RouteAnalysisService(),
        weather_provider=weather,
        travel_research_provider=research,
        transport_provider=RecordingTransportProvider(),
        planning_provider=FailingPlanningProvider(),
        llm_provider=TemplateGuideProvider(),
        enable_llm_planner=True,
    )

    response = agent.generate(HikingGuideRequest(destination="武功山"))

    assert len(weather.calls) == 1
    assert research.calls == [
        {"include_lodging": True, "include_food": True, "include_supply": True}
    ]
    assert "planner:rules" in response.llm_usage
    assert any("LLM planner 暂不可用" in warning for warning in response.warnings)


def test_agent_degrades_failing_final_llm_to_template() -> None:
    agent = HikingGuideAgent(
        planner_service=RecordingPlanner(),
        analysis_service=RouteAnalysisService(),
        travel_research_provider=StaticTravelResearchProvider(),
        transport_provider=StaticTransportProvider(),
        planning_provider=FixedPlanningProvider(GuideToolPlan()),
        llm_provider=FailingLLMProvider(),
    )

    response = agent.generate(HikingGuideRequest(destination="武功山"))

    assert "composer:template" in response.llm_usage
    assert any("百炼模型暂不可用" in warning for warning in response.warnings)


def test_agent_corrects_llm_itinerary_to_user_selected_days() -> None:
    agent = HikingGuideAgent(
        planner_service=RecordingPlanner(),
        analysis_service=RouteAnalysisService(),
        travel_research_provider=StaticTravelResearchProvider(),
        transport_provider=StaticTransportProvider(),
        planning_provider=FixedPlanningProvider(GuideToolPlan()),
        llm_provider=TwoDayLLMProvider(),
    )

    response = agent.generate(
        HikingGuideRequest(
            destination="武功山",
            start_city="上海",
            date_range=(__import__("datetime").date(2026, 7, 1), __import__("datetime").date(2026, 7, 4)),
        )
    )

    assert response.itinerary is not None
    assert response.itinerary.total_days == 4
    assert any("校准行程为 4 天" in note for note in response.validation_notes)


def test_agent_keeps_template_skeleton_when_llm_omits_structured_sections() -> None:
    agent = HikingGuideAgent(
        planner_service=RecordingPlanner(),
        analysis_service=RouteAnalysisService(),
        travel_research_provider=StaticTravelResearchProvider(),
        transport_provider=StaticTransportProvider(),
        planning_provider=FixedPlanningProvider(GuideToolPlan()),
        llm_provider=StrongClaimLLMProvider(),
    )

    response = agent.generate(HikingGuideRequest(destination="武功山"))

    assert response.summary.startswith("这是社区热门路线")
    assert response.itinerary is not None
    assert response.gear_list is not None
    assert response.safety_guide is not None
    assert any(event.tool_name == "guide_reviewer" for event in response.agent_trace)
    assert any("强事实" in note for note in response.validation_notes)


def test_agent_keeps_non_blocking_clarifying_questions() -> None:
    agent = HikingGuideAgent(
        planner_service=RecordingPlanner(),
        analysis_service=RouteAnalysisService(),
        travel_research_provider=StaticTravelResearchProvider(),
        transport_provider=StaticTransportProvider(),
        planning_provider=FixedPlanningProvider(
            GuideDecision(
                tool_plan=GuideToolPlan(query_transport=False),
                clarifying_questions=["补充出发日期"],
                validation_notes=["机票报价已跳过"],
            )
        ),
        llm_provider=TemplateGuideProvider(),
        enable_llm_planner=True,
    )

    response = agent.generate(HikingGuideRequest(destination="武功山"))

    assert "补充出发日期" in response.clarifying_questions
    assert "机票报价已跳过" in response.validation_notes
    assert response.summary
