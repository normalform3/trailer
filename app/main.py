from __future__ import annotations

import json
from queue import Queue
from threading import Thread
from datetime import date, timedelta
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, StreamingResponse

from app.agents import HikingGuideAgent
from app.config import get_settings
from app.models import (
    Coordinate,
    HikingGuideRequest,
    HikingGuideResponse,
    RouteRecommendationRequest,
    RouteRecommendationResponse,
)
from app.providers.llm import BailianQwenGuideProvider
from app.providers.transport import RoughTransportProvider
from app.providers.weather import AmapWeatherProvider, CompositeWeatherProvider, DailyForecast, NoopWeatherProvider, OpenMeteoWeatherProvider, WeatherProvider
from app.services.route_analysis import RouteAnalysisService
from app.services.route_ingestion import RouteIngestionError, RouteIngestionService
from app.services.route_recommendations import (
    RouteRecommendationService,
    RouteRecommendationUnavailable,
)

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
_route_recommendation_service = RouteRecommendationService()
DEFAULT_WEATHER_RANGE_DAYS = 7
MAX_WEATHER_RANGE_DAYS = 16


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


@app.get("/api/v1/llm/health")
def llm_health() -> dict[str, object]:
    """Run a minimal-token LLM connectivity check."""
    provider = BailianQwenGuideProvider()
    try:
        return provider.test_connection()
    except Exception as exc:  # noqa: BLE001 - health check should report provider failures.
        return {
            "ok": False,
            "model": provider.model,
            "error": _public_llm_error(exc),
        }


@app.post("/api/v1/route-recommendations", response_model=RouteRecommendationResponse)
def recommend_routes(request: RouteRecommendationRequest) -> RouteRecommendationResponse:
    """Discover evidence-backed route names before the user uploads a track."""
    try:
        return _route_recommendation_service.recommend(request)
    except RouteRecommendationUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - provider details are converted to a stable public error.
        raise HTTPException(status_code=502, detail=_public_route_recommendation_error(exc)) from exc


@app.post("/api/v1/route-recommendations/stream")
def stream_route_recommendations(request: RouteRecommendationRequest) -> StreamingResponse:
    """Stream route discovery phases while the LLM and map providers run."""

    def event_stream():
        events: Queue[dict[str, object] | None] = Queue()

        def run() -> None:
            try:
                response = _route_recommendation_service.recommend(
                    request,
                    on_event=lambda event: events.put({"event": "trace", **event}),
                )
                events.put({"event": "final", "response": response.model_dump(mode="json")})
            except RouteRecommendationUnavailable as exc:
                events.put({"event": "error", "status_code": 503, "detail": str(exc)})
            except Exception as exc:  # noqa: BLE001
                events.put({"event": "error", "status_code": 502, "detail": _public_route_recommendation_error(exc)})
            finally:
                events.put(None)

        Thread(target=run, daemon=True).start()
        while True:
            event = events.get()
            if event is None:
                break
            yield _sse(event)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


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


@app.post("/api/v1/hiking-guides/upload/stream")
async def stream_hiking_guide_with_kml(
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
) -> StreamingResponse:
    content = await route_file.read() if route_file else None
    request = _build_guide_request(
        destination=destination,
        start_city=start_city,
        start_date=start_date,
        end_date=end_date,
        fitness_level=fitness_level,
        preferences=preferences,
        route_text=route_text,
        reference_links=reference_links,
        reference_notes=reference_notes,
    )

    def event_stream():
        try:
            yield _sse({"event": "trace", "phase": "planner", "title": "启动 Planner Agent", "status": "running", "detail": "正在读取输入并准备按需调用工具。"})
            for event in agent.generate_events(request, route_file_content=content):
                yield _sse(event)
        except Exception as exc:  # noqa: BLE001
            yield _sse({"event": "error", "detail": str(exc)})

    return StreamingResponse(event_stream(), media_type="text/event-stream")


def _build_guide_request(
    destination: str,
    start_city: str | None,
    start_date: str | None,
    end_date: str | None,
    fitness_level: str | None,
    preferences: str | None,
    route_text: str | None,
    reference_links: str | None,
    reference_notes: str | None,
) -> HikingGuideRequest:
    date_range = None
    if start_date and end_date:
        try:
            date_range = (date.fromisoformat(start_date), date.fromisoformat(end_date))
        except ValueError:
            raise HTTPException(status_code=422, detail="日期格式无效，请使用 YYYY-MM-DD 格式")

    prefs = [p.strip() for p in (preferences or "").split(",") if p.strip()] if preferences else []

    return HikingGuideRequest(
        destination=destination,
        start_city=start_city,
        date_range=date_range,
        fitness_level=fitness_level,
        preferences=prefs,
        route_text=route_text,
        reference_links=_split_reference_links(reference_links),
        reference_notes=reference_notes,
    )


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _public_llm_error(exc: Exception) -> str:
    text = str(exc)
    if "DASHSCOPE_API_KEY" in text or "BAILIAN_API_KEY" in text:
        return "未配置 DASHSCOPE_API_KEY 或 BAILIAN_API_KEY"
    if "dashscope package is not installed" in text:
        return "未安装 dashscope 依赖"
    return text or exc.__class__.__name__


def _public_route_recommendation_error(exc: Exception) -> str:
    text = str(exc)
    if "DASHSCOPE_API_KEY" in text or "BAILIAN_API_KEY" in text:
        return "未配置 DASHSCOPE_API_KEY 或 BAILIAN_API_KEY"
    if "dashscope" in text.lower() or "百炼" in text or "DashScope" in text:
        return "百炼联网检索暂不可用，请检查当前模型是否支持联网搜索或稍后重试。"
    return "路线推荐服务暂不可用，请稍后重试。"


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
        range_end = date.fromisoformat(end_date) if end_date else range_start + timedelta(days=DEFAULT_WEATHER_RANGE_DAYS - 1)
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
        warnings.append(f"当前天气数据最多支持未来 {MAX_WEATHER_RANGE_DAYS} 天，结束日期已截断到 {max_end.isoformat()}")
        range_end = max_end

    return range_start, range_end, warnings


def _weather_forecasts_for_range(
    coordinate: Coordinate,
    range_start: date,
    range_end: date,
) -> tuple[list[DailyForecast], list[str]]:
    today = date.today()
    forecasts: list[DailyForecast] = []

    if _amap_weather and range_start <= today + timedelta(days=3) and range_end >= today:
        try:
            forecasts.extend(_filter_forecasts_for_range(_amap_weather.daily_forecast(coordinate), range_start, range_end))
        except Exception:
            pass

    missing_ranges = _missing_forecast_ranges(range_start, range_end, forecasts)
    for missing_start, missing_end in missing_ranges:
        try:
            forecasts.extend(_open_meteo_weather.daily_forecast(coordinate, missing_start, missing_end))
        except Exception:
            raise

    return _filter_forecasts_for_range(forecasts, range_start, range_end), []


def _filter_forecasts_for_range(
    forecasts: list[DailyForecast],
    range_start: date,
    range_end: date,
) -> list[DailyForecast]:
    seen_dates: set[str] = set()
    filtered: list[DailyForecast] = []
    for forecast in sorted(forecasts, key=lambda item: item.date):
        if forecast.date in seen_dates:
            continue
        if range_start.isoformat() <= forecast.date <= range_end.isoformat():
            filtered.append(forecast)
            seen_dates.add(forecast.date)
    return filtered


def _missing_forecast_ranges(
    range_start: date,
    range_end: date,
    forecasts: list[DailyForecast],
) -> list[tuple[date, date]]:
    covered_dates = {forecast.date for forecast in forecasts}
    missing_ranges: list[tuple[date, date]] = []
    current_start: date | None = None
    current_end: date | None = None

    current = range_start
    while current <= range_end:
        if current.isoformat() not in covered_dates:
            if current_start is None:
                current_start = current
            current_end = current
        elif current_start and current_end:
            missing_ranges.append((current_start, current_end))
            current_start = None
            current_end = None
        current += timedelta(days=1)

    if current_start and current_end:
        missing_ranges.append((current_start, current_end))
    return missing_ranges
