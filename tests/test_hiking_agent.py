from app.agents import HikingGuideAgent
from app.models import Coordinate, HikingGuideRequest, RouteGeometry, RouteSource
from app.providers.transport import StaticTransportProvider
from app.providers.travel_research import StaticTravelResearchProvider
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
