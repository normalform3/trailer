from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse

from app.agents import HikingGuideAgent
from app.config import get_settings
from app.models import Coordinate, HikingGuideRequest, HikingGuideResponse
from app.providers.transport import RoughTransportProvider
from app.providers.weather import AmapWeatherProvider, CompositeWeatherProvider, DailyForecast, NoopWeatherProvider, OpenMeteoWeatherProvider, WeatherProvider
from app.services.route_analysis import RouteAnalysisService
from app.services.route_ingestion import RouteIngestionError, RouteIngestionService
from app.services.geo import line_distance_m

app = FastAPI(title="Trailer Hiking Guide Agent", version="0.1.0")


def _default_weather_provider() -> WeatherProvider:
    settings = get_settings()
    providers: list[WeatherProvider] = []
    if settings.api_keys.amap_api_key:
        providers.append(AmapWeatherProvider(settings.api_keys.amap_api_key))
    providers.append(OpenMeteoWeatherProvider())
    if providers:
        return CompositeWeatherProvider(providers)
    return NoopWeatherProvider()


agent = HikingGuideAgent(
    weather_provider=_default_weather_provider(),
    transport_provider=RoughTransportProvider(),
)
STATIC_DIR = Path(__file__).parent / "static"
_ingestion = RouteIngestionService()
_analysis = RouteAnalysisService()
_amap_weather = AmapWeatherProvider() if get_settings().api_keys.amap_api_key else None
_open_meteo_weather = OpenMeteoWeatherProvider(timeout_s=15.0)
MAX_WEATHER_RANGE_DAYS = 46


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

    Returns daily weather analysis for the requested date range.
    Amap is used for short-term local forecasts when configured; Open-Meteo
    provides longer screening windows for trip-date selection.
    """
    try:
        range_start, range_end, warnings = _normalize_weather_date_range(start_date, end_date)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    coordinate = Coordinate(lon=lng, lat=lat)
    try:
        forecasts, provider_warnings = _weather_forecasts_for_range(coordinate, range_start, range_end)
        warnings.extend(provider_warnings)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"天气服务请求失败: {exc}") from exc

    forecasts = [f for f in forecasts if range_start.isoformat() <= f.date <= range_end.isoformat()]
    suitable_dates = [f.date for f in forecasts if f.suitability_label == "适宜"]
    marginal_dates = [f.date for f in forecasts if f.suitability_label == "一般"]
    sources = sorted({f.source for f in forecasts})

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
                "precipitation_probability": f.precipitation_probability,
                "precipitation_mm": f.precipitation_mm,
                "wind_gust_kmh": f.wind_gust_kmh,
                "uv_index_max": f.uv_index_max,
                "risk_notes": list(f.hiking_risk_notes),
                "is_suitable": f.is_suitable_for_hiking,
                "suitability_label": f.suitability_label,
                "source": f.source,
            }
            for f in forecasts
        ],
        "location": {"lat": lat, "lng": lng},
        "date_range": {"start_date": range_start.isoformat(), "end_date": range_end.isoformat()},
        "suitable_dates": suitable_dates,
        "marginal_dates": marginal_dates,
        "data_sources": sources,
        "warnings": warnings,
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
    reference_links: str | None = Form(default=None),
    reference_notes: str | None = Form(default=None),
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
        reference_links=_split_reference_links(reference_links),
        reference_notes=reference_notes,
    )
    return agent.generate(request, route_file_content=content)


def _split_reference_links(value: str | None) -> list[str]:
    if not value:
        return []
    normalized = value.replace("\n", ",").replace("，", ",")
    return [item.strip() for item in normalized.split(",") if item.strip()]


def _normalize_weather_date_range(
    start_date: str | None,
    end_date: str | None,
) -> tuple[date, date, list[str]]:
    today = date.today()
    warnings: list[str] = []
    try:
        range_start = date.fromisoformat(start_date) if start_date else today
        range_end = date.fromisoformat(end_date) if end_date else range_start + timedelta(days=29)
    except ValueError as exc:
        raise ValueError("日期格式无效，请使用 YYYY-MM-DD 格式") from exc

    if range_start > range_end:
        raise ValueError("结束日期不能早于开始日期")

    max_end = today + timedelta(days=MAX_WEATHER_RANGE_DAYS - 1)
    if range_end < today:
        raise ValueError("天气分析只支持今天及未来日期")
    if range_start < today:
        warnings.append(f"开始日期早于今天，已从 {today.isoformat()} 开始分析")
        range_start = today
    if range_start > max_end:
        raise ValueError(f"当前天气数据最多支持未来 {MAX_WEATHER_RANGE_DAYS} 天")
    if range_end > max_end:
        warnings.append(f"结束日期超出可用预报窗口，已截断到 {max_end.isoformat()}")
        range_end = max_end

    return range_start, range_end, warnings


def _weather_forecasts_for_range(
    coordinate: Coordinate,
    range_start: date,
    range_end: date,
) -> tuple[list[DailyForecast], list[str]]:
    range_days = (range_end - range_start).days + 1
    today = date.today()
    if _amap_weather and range_days <= 4 and range_end <= today + timedelta(days=3):
        try:
            return _amap_weather.daily_forecast(coordinate), []
        except Exception:
            pass
    try:
        return _open_meteo_weather.daily_forecast(coordinate, range_start, range_end), []
    except Exception:
        forecast_horizon_end = today + timedelta(days=15)
        fallback_end = min(range_end, forecast_horizon_end)
        if range_start > fallback_end:
            return [], [
                (
                    "长周期趋势服务暂不可用，且所选日期已超出常规 16 天预报窗口；"
                    "请稍后重试或改选更近的日期"
                )
            ]
        if range_days <= 16 and range_end <= forecast_horizon_end:
            raise
        forecasts = _open_meteo_weather.daily_forecast(coordinate, range_start, fallback_end)
        return forecasts, [
            f"长周期趋势服务暂不可用，已返回 {range_start.isoformat()} 至 {fallback_end.isoformat()} 的常规预报"
        ]
