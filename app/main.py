from __future__ import annotations

from datetime import date
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse

from app.agents import HikingGuideAgent
from app.config import get_settings
from app.models import Coordinate, HikingGuideRequest, HikingGuideResponse
from app.providers.transport import AmapTransportProvider
from app.providers.weather import AmapWeatherProvider, DailyForecast, NoopWeatherProvider, WeatherProvider
from app.services.route_analysis import RouteAnalysisService
from app.services.route_ingestion import RouteIngestionError, RouteIngestionService
from app.services.geo import line_distance_m

app = FastAPI(title="Trailer Hiking Guide Agent", version="0.1.0")


def _default_weather_provider() -> WeatherProvider:
    settings = get_settings()
    if settings.api_keys.amap_api_key:
        return AmapWeatherProvider(settings.api_keys.amap_api_key)
    return NoopWeatherProvider()


agent = HikingGuideAgent(
    weather_provider=_default_weather_provider(),
    transport_provider=AmapTransportProvider() if get_settings().api_keys.amap_api_key else None,
)
STATIC_DIR = Path(__file__).parent / "static"
_ingestion = RouteIngestionService()
_analysis = RouteAnalysisService()
_amap_weather = AmapWeatherProvider() if get_settings().api_keys.amap_api_key else None


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", response_class=FileResponse)
def frontend() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/v1/config/map")
def map_config() -> dict[str, str | None]:
    """Return the AMap Web JS API key for frontend use."""
    settings = get_settings()
    return {"amap_web_key": settings.api_keys.amap_web_key}


@app.post("/api/v1/kml-preview")
async def kml_preview(
    route_file: UploadFile = File(...),
) -> dict:
    """Parse a KML file and return coordinates with analysis for map rendering."""
    content = await route_file.read()
    try:
        routes = _ingestion.parse_kml(content)
    except RouteIngestionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    route_responses = []
    for r in routes:
        _, analysis = _analysis.analyze(r)
        elev = analysis.elevation
        route_responses.append({
            "name": r.name,
            "description": r.description,
            "source": r.source.value if r.source else None,
            "track_type": r.metadata.get("type"),
            "coordinates": [
                {"lat": c.lat, "lng": c.lon, "elevation_m": c.elevation_m}
                for c in r.coordinates
            ],
            "distance_km": analysis.distance_km,
            "estimated_duration_hours": analysis.estimated_duration_hours,
            "min_elevation_m": elev.min_m,
            "max_elevation_m": elev.max_m,
            "ascent_m": elev.ascent_m,
            "descent_m": elev.descent_m,
            "risk_level": analysis.risk_level,
            "risk_factors": analysis.risk_factors,
            "warnings": analysis.warnings,
        })

    return {"routes": route_responses}


@app.get("/api/v1/weather-forecast")
def weather_forecast(
    lat: float = Query(..., description="Latitude"),
    lng: float = Query(..., description="Longitude"),
    start_date: str | None = Query(default=None, description="Start date YYYY-MM-DD"),
    end_date: str | None = Query(default=None, description="End date YYYY-MM-DD"),
) -> dict:
    """Get weather forecast for a coordinate location.

    Returns up to 4 days of forecast from Amap.
    Optional start_date/end_date (YYYY-MM-DD) filters the results.
    """
    if not _amap_weather:
        raise HTTPException(status_code=503, detail="高德地图 API Key 未配置，天气服务不可用")

    coordinate = Coordinate(lon=lng, lat=lat)
    try:
        forecasts = _amap_weather.daily_forecast(coordinate)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"天气服务请求失败: {exc}") from exc

    # Filter by date range if provided
    if start_date or end_date:
        filtered = []
        for f in forecasts:
            if start_date and f.date < start_date:
                continue
            if end_date and f.date > end_date:
                continue
            filtered.append(f)
        forecasts = filtered

    return {
        "forecasts": [
            {
                "date": f.date,
                "day_weather": f.day_weather,
                "night_weather": f.night_weather,
                "day_temp": f.day_temp,
                "night_temp": f.night_temp,
                "day_wind": f.day_wind,
                "night_wind": f.night_wind,
                "day_power": f.day_power,
                "night_power": f.night_power,
                "is_suitable": f.is_suitable_for_hiking,
                "suitability_label": f.suitability_label,
            }
            for f in forecasts
        ],
        "location": {"lat": lat, "lng": lng},
    }


@app.post("/api/v1/hiking-guides", response_model=HikingGuideResponse)
def create_hiking_guide(request: HikingGuideRequest) -> HikingGuideResponse:
    return agent.generate(request)


@app.post("/api/v1/hiking-guides/upload", response_model=HikingGuideResponse)
async def create_hiking_guide_with_kml(
    destination: str = Form(...),
    start_city: str | None = Form(default=None),
    start_date: str | None = Form(default=None),
    end_date: str | None = Form(default=None),
    fitness_level: str | None = Form(default=None),
    preferences: str | None = Form(default=None),
    route_text: str | None = Form(default=None),
    route_file: UploadFile | None = File(default=None),
) -> HikingGuideResponse:
    content = await route_file.read() if route_file else None

    date_range = None
    if start_date and end_date:
        try:
            date_range = (date.fromisoformat(start_date), date.fromisoformat(end_date))
        except ValueError:
            raise HTTPException(status_code=422, detail="日期格式无效，请使用 YYYY-MM-DD 格式")

    prefs = [p.strip() for p in (preferences or "").split(",") if p.strip()] if preferences else []

    request = HikingGuideRequest(
        destination=destination,
        start_city=start_city,
        date_range=date_range,
        fitness_level=fitness_level,
        preferences=prefs,
        route_text=route_text,
    )
    return agent.generate(request, route_file_content=content)
