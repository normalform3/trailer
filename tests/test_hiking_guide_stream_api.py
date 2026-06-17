import json

from fastapi.testclient import TestClient

from app import main as main_module
from app.agents import HikingGuideAgent
from app.models import GuideToolPlan
from app.providers.llm import TemplateGuideProvider
from app.providers.transport import StaticTransportProvider
from app.providers.travel_research import StaticTravelResearchProvider
from app.services.route_analysis import RouteAnalysisService

from tests.test_hiking_agent import FixedPlanningProvider, RecordingPlanner


def test_stream_upload_endpoint_returns_trace_before_final(monkeypatch) -> None:
    agent = HikingGuideAgent(
        planner_service=RecordingPlanner(),
        analysis_service=RouteAnalysisService(),
        travel_research_provider=StaticTravelResearchProvider(),
        transport_provider=StaticTransportProvider(),
        planning_provider=FixedPlanningProvider(GuideToolPlan(compose_with_llm=False)),
        llm_provider=TemplateGuideProvider(),
    )
    monkeypatch.setattr(main_module, "agent", agent)

    client = TestClient(main_module.app)
    response = client.post(
        "/api/v1/hiking-guides/upload/stream",
        data={"destination": "武功山", "start_city": "上海"},
        files={
            "route_file": (
                "route.kml",
                b"<kml><Placemark><name>test</name><LineString><coordinates>114.1,27.1 114.2,27.2</coordinates></LineString></Placemark></kml>",
                "application/vnd.google-earth.kml+xml",
            )
        },
    )

    payloads = []
    for chunk in response.text.split("\n\n"):
        line = next((item for item in chunk.splitlines() if item.startswith("data:")), None)
        if line:
            payloads.append(json.loads(line.removeprefix("data:").strip()))

    assert response.status_code == 200
    assert payloads[0]["event"] == "trace"
    assert payloads[-1]["event"] == "final"
    assert payloads[-1]["response"]["agent_trace"]
