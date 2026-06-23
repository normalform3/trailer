from __future__ import annotations

import html
import json
import re
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx

from app.config import get_settings
from app.models import (
    RouteImportApplyRequest,
    RouteImportApplyResponse,
    RouteImportExtractedCandidate,
    RouteImportJobRecord,
    RouteImportRequest,
    RouteKnowledgeSource,
    RouteKnowledgeUpdate,
)
from app.services.route_knowledge import SQLiteRouteKnowledgeRepository


class RouteImportExtractor(Protocol):
    def extract(self, text: str, title: str | None = None, source_url: str | None = None) -> list[RouteImportExtractedCandidate]:
        raise NotImplementedError


class RouteKnowledgeImportService:
    def __init__(
        self,
        repository: SQLiteRouteKnowledgeRepository,
        extractor: RouteImportExtractor | None = None,
        timeout_s: float = 10.0,
        max_chars: int = 12000,
    ) -> None:
        self.repository = repository
        self.extractor = extractor or BailianRouteImportExtractor()
        self.timeout_s = timeout_s
        self.max_chars = max_chars

    def create_job(self, request: RouteImportRequest) -> RouteImportJobRecord:
        warnings: list[str] = []
        title: str | None = None
        source_url = request.source_url
        text = request.raw_text or ""

        if source_url:
            try:
                fetched_text, fetched_title, final_url = self._fetch_url(source_url)
                source_url = final_url
                title = fetched_title
                text = "\n\n".join(item for item in [text, fetched_text] if item).strip()
            except Exception as exc:  # noqa: BLE001 - import drafts should preserve failures as warnings.
                warnings.append(f"URL 无法读取：{exc}")

        text = _normalize_text(text)[: self.max_chars]
        if not text:
            warnings.append("没有可抽取的正文，已创建空草稿任务。")
            return self.repository.create_import_job(source_url, "", title, warnings, [])

        candidates: list[RouteImportExtractedCandidate] = []
        try:
            candidates = self.extractor.extract(text, title=title, source_url=source_url)
        except Exception as exc:  # noqa: BLE001 - fall back to deterministic extraction.
            warnings.append(f"LLM 抽取失败，已使用启发式候选：{exc}")

        if not candidates:
            candidates = _heuristic_extract(text)
            if candidates:
                warnings.append("未获得结构化抽取结果，已根据正文标题和编号行生成待审核候选。")
            else:
                warnings.append("正文中没有识别出明确路线候选，请补充更完整的路线资料。")

        return self.repository.create_import_job(source_url, text, title, warnings, _dedupe_candidates(candidates))

    def get_job(self, job_id: str) -> RouteImportJobRecord | None:
        return self.repository.get_import_job(job_id)

    def apply_job(self, job_id: str, request: RouteImportApplyRequest) -> RouteImportApplyResponse:
        job = self.repository.get_import_job(job_id)
        if job is None:
            raise ValueError("导入任务不存在")

        warnings = list(job.warnings)
        created_count = 0
        merged_count = 0
        ignored_count = 0

        for decision in request.decisions:
            candidate = self.repository.get_import_candidate(decision.candidate_id)
            if candidate is None or candidate.job_id != job_id:
                warnings.append(f"候选 {decision.candidate_id} 不属于当前任务，已跳过。")
                continue

            if decision.action == "ignore":
                self.repository.update_import_candidate_status(candidate.id, "ignored", action="ignore")
                ignored_count += 1
                continue

            if decision.action == "needs_review":
                self.repository.update_import_candidate_status(candidate.id, "needs_review", action="needs_review")
                continue

            if decision.action == "create":
                try:
                    self.repository.create_record(self.repository.import_candidate_to_payload(candidate))
                    self.repository.update_import_candidate_status(candidate.id, "applied", action="create")
                    created_count += 1
                except ValueError as exc:
                    warnings.append(f"候选「{candidate.name}」新增失败：{exc}")
                    self.repository.update_import_candidate_status(candidate.id, "needs_review", action="needs_review")
                continue

            target_route_id = decision.target_route_id or candidate.matched_route_id
            if not target_route_id:
                warnings.append(f"候选「{candidate.name}」缺少合并目标，已保留待审核。")
                self.repository.update_import_candidate_status(candidate.id, "needs_review", action="needs_review")
                continue
            target = self.repository.get_record(target_route_id)
            if target is None:
                warnings.append(f"候选「{candidate.name}」的合并目标不存在：{target_route_id}")
                self.repository.update_import_candidate_status(candidate.id, "needs_review", action="needs_review")
                continue

            update = _merge_candidate_into_route(target, candidate)
            self.repository.update_record(target_route_id, update)
            self.repository.update_import_candidate_status(candidate.id, "applied", action="merge", target_route_id=target_route_id)
            merged_count += 1

        final_status = "applied" if any([created_count, merged_count, ignored_count]) else job.status
        self.repository.update_import_job_status(job_id, final_status, warnings)
        refreshed = self.repository.get_import_job(job_id)
        return RouteImportApplyResponse(
            job=refreshed or job,
            created_count=created_count,
            merged_count=merged_count,
            ignored_count=ignored_count,
            warnings=warnings,
        )

    def _fetch_url(self, url: str) -> tuple[str, str | None, str]:
        response = httpx.get(
            url,
            headers={
                "User-Agent": "TrailerRouteImportBot/0.1 (+user-provided-url; draft-extraction-only)",
                "Accept": "text/html, text/plain;q=0.8",
            },
            follow_redirects=True,
            timeout=self.timeout_s,
        )
        response.raise_for_status()
        content_type = response.headers.get("content-type", "")
        if "html" not in content_type and "text" not in content_type:
            raise RuntimeError("页面不是可读取的文本内容")
        text, title = _extract_readable_text(response.text)
        if not text:
            raise RuntimeError("页面没有可抽取的正文")
        return text, title or urlparse(str(response.url or url)).netloc, str(response.url or url)


class BailianRouteImportExtractor:
    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        base_http_api_url: str | None = None,
    ) -> None:
        settings = get_settings()
        self.api_key = api_key or settings.api_keys.dashscope_api_key or settings.api_keys.bailian_api_key
        self.model = model or settings.bailian.model
        self.base_http_api_url = (base_http_api_url or settings.dashscope.base_http_api_url).rstrip("/")

    def extract(self, text: str, title: str | None = None, source_url: str | None = None) -> list[RouteImportExtractedCandidate]:
        if not self.api_key:
            raise RuntimeError("DASHSCOPE_API_KEY or BAILIAN_API_KEY is not configured")

        try:
            import dashscope
        except ModuleNotFoundError as exc:
            raise RuntimeError("dashscope package is not installed") from exc

        dashscope.base_http_api_url = self.base_http_api_url
        response = dashscope.MultiModalConversation.call(
            api_key=self.api_key,
            model=self.model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "text": (
                                "你是徒步路线知识库的资料抽取器。只根据输入正文抽取候选路线，"
                                "不要写入数据库，不要补全正文没有给出的官方事实。输出 JSON 对象，格式为："
                                "{\"routes\":[{\"name\":\"路线名\",\"region\":\"省市/区域\","
                                "\"province\":\"省份或null\",\"city\":\"城市或null\",\"summary\":\"不超过120字\","
                                "\"tags\":[\"景观或主题\"],\"distance_km\":null,\"duration_hours\":null,"
                                "\"ascent_m\":null,\"risk_notes\":[\"待核验事项\"]}]}。"
                                "只保留明确可作为徒步路线卡的候选，最多 20 条。"
                                f"\n来源标题：{title or '未知'}\n来源 URL：{source_url or '无'}\n正文：{text[:12000]}"
                            )
                        }
                    ],
                }
            ],
        )
        _raise_for_dashscope_error(response)
        parsed = _parse_json_content(_extract_dashscope_text(response))
        routes = parsed.get("routes") or []
        if not isinstance(routes, list):
            raise RuntimeError("LLM routes field must be a list")
        candidates = []
        for item in routes[:20]:
            if not isinstance(item, dict):
                continue
            try:
                candidates.append(RouteImportExtractedCandidate.model_validate(item))
            except Exception:
                continue
        if not candidates:
            raise RuntimeError("LLM response did not include valid route candidates")
        return candidates


def _merge_candidate_into_route(route, candidate) -> RouteKnowledgeUpdate:
    source_list = list(route.sources)
    if candidate.source_url and all(source.url != candidate.source_url for source in source_list):
        source_list.append(
            RouteKnowledgeSource(
                title=candidate.source_title or "导入资料",
                url=candidate.source_url,
                source_type=candidate.source_type,
                summary=candidate.summary,
            )
        )

    aliases = _dedupe([*route.aliases, candidate.name] if candidate.name != route.name else route.aliases)
    tags = _dedupe([*route.tags, *candidate.tags])
    risk_notes = _dedupe([*route.risk_notes, *candidate.risk_notes, "导入资料合并内容需人工核验。"])
    summary = route.summary or candidate.summary

    return RouteKnowledgeUpdate(
        name=route.name,
        province=route.province,
        city=route.city,
        summary=summary,
        difficulty=route.difficulty,
        distance_km=route.distance_km or candidate.distance_km,
        duration_hours=route.duration_hours or candidate.duration_hours,
        ascent_m=route.ascent_m or candidate.ascent_m,
        camping=route.camping,
        seasons=route.seasons,
        aliases=aliases,
        tags=tags,
        transport_notes=route.transport_notes,
        editorial_rank=route.editorial_rank,
        official_status=route.official_status,
        risk_level=route.risk_level,
        risk_notes=risk_notes,
        status=route.status,
        last_verified_at=route.last_verified_at,
        sources=source_list,
    )


def _heuristic_extract(text: str) -> list[RouteImportExtractedCandidate]:
    candidates: list[RouteImportExtractedCandidate] = []
    route_keywords = ("徒步", "路线", "线路", "古道", "步道", "穿越", "登山", "环线", "山", "峡", "湖", "峰", "沟")
    split_lines = re.split(r"\n+|(?<=。)\s*", text)
    for line in split_lines:
        clean = re.sub(r"^[\s\-*（(]*\d{1,2}[).、．\s-]*", "", line).strip()
        clean = re.sub(r"^\s*[一二三四五六七八九十]{1,3}[、.．]\s*", "", clean)
        if len(clean) < 4 or not any(keyword in clean for keyword in route_keywords):
            continue
        name_match = re.match(r"([^，,。；;：:（）()\s]{2,24}(?:徒步路线|徒步线路|古道|步道|穿越|环线|路线|线路|山|峡|沟|湖|峰)?)", clean)
        name = name_match.group(1).strip() if name_match else clean[:18].strip()
        name = re.sub(r"^(推荐|第.{1,3}条|中国十大|十大)", "", name).strip(" ：:")
        if not name or len(name) < 2:
            continue
        region = _guess_region(clean)
        tags = _guess_tags(clean)
        distance = _first_number(clean, r"(\d+(?:\.\d+)?)\s*(?:公里|km|KM)")
        duration = _first_number(clean, r"(\d+(?:\.\d+)?)\s*(?:小时|h|H)")
        ascent = _first_number(clean, r"(?:爬升|累计上升|上升)\s*(\d+(?:\.\d+)?)\s*(?:米|m|M)")
        candidates.append(
            RouteImportExtractedCandidate(
                name=name,
                region=region,
                summary=clean[:180],
                tags=tags,
                distance_km=distance,
                duration_hours=duration,
                ascent_m=ascent,
                risk_notes=["启发式抽取结果，需人工核验名称、地区和指标。"],
            )
        )
        if len(candidates) >= 12:
            break
    return _dedupe_candidates(candidates)


def _guess_region(text: str) -> str | None:
    match = re.search(r"([\u4e00-\u9fff]{2,8}(?:省|自治区|市|州|地区|县|区))", text)
    return match.group(1) if match else None


def _guess_tags(text: str) -> list[str]:
    tag_map = {
        "雪山": ("雪山", "冰川", "高海拔"),
        "峡谷": ("峡谷", "峡"),
        "草甸": ("草甸", "高山草甸"),
        "古道": ("古道", "茶马古道"),
        "海岸": ("海岸", "海边", "海滨"),
        "森林": ("森林", "林海"),
        "湖泊": ("湖", "海子"),
        "溪流": ("溪", "瀑布", "水线"),
        "长线": ("穿越", "重装", "多日"),
    }
    tags = [label for label, keywords in tag_map.items() if any(keyword in text for keyword in keywords)]
    return tags[:5]


def _first_number(text: str, pattern: str) -> float | None:
    match = re.search(pattern, text, flags=re.IGNORECASE)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def _extract_readable_text(raw: str) -> tuple[str, str | None]:
    title_match = re.search(r"<title[^>]*>(.*?)</title>", raw, flags=re.IGNORECASE | re.DOTALL)
    title = _normalize_text(title_match.group(1)) if title_match else None
    text = re.sub(r"<(script|style|noscript|svg|canvas)[^>]*>.*?</\1>", " ", raw, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    return _normalize_text(html.unescape(text)), title


def _normalize_text(value: str) -> str:
    value = html.unescape(value)
    value = re.sub(r"\r\n?", "\n", value)
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def _dedupe_candidates(candidates: list[RouteImportExtractedCandidate]) -> list[RouteImportExtractedCandidate]:
    seen: set[str] = set()
    result: list[RouteImportExtractedCandidate] = []
    for candidate in candidates:
        key = re.sub(r"\W+", "", f"{candidate.name}-{candidate.region or candidate.province or ''}".lower())
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(candidate)
    return result[:20]


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        text = str(item).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _raise_for_dashscope_error(response: Any) -> None:
    status_code = _read(response, "status_code", default=None)
    if status_code is None or int(status_code) == 200:
        return
    code = _read(response, "code", default="unknown")
    message = _read(response, "message", default="DashScope request failed")
    raise RuntimeError(f"DashScope request failed ({status_code}, {code}): {message}")


def _extract_dashscope_text(response: Any) -> object:
    try:
        output = _read(response, "output")
        choices = _read(output, "choices")
        message = _read(choices[0], "message")
        content = _read(message, "content")
        if isinstance(content, str):
            return content
        return _read(content[0], "text")
    except (KeyError, IndexError, TypeError, AttributeError) as exc:
        raise RuntimeError("DashScope response did not include text content") from exc


def _read(value: Any, key: str, default: Any = ...) -> Any:
    try:
        if isinstance(value, dict):
            return value[key]
        return getattr(value, key)
    except (KeyError, AttributeError):
        if default is ...:
            raise
        return default


def _parse_json_content(content: object) -> dict[str, object]:
    if isinstance(content, dict):
        return content
    if isinstance(content, str):
        content_str = content.strip()
    else:
        raise RuntimeError("LLM response content has unsupported type")
    match = re.search(r"```(?:json)?\s*(.*?)\s*```", content_str, re.DOTALL | re.IGNORECASE)
    if match:
        content_str = match.group(1)
    if content_str.lower().startswith("json"):
        content_str = content_str[4:].strip()
    parsed = json.loads(content_str)
    if not isinstance(parsed, dict):
        raise RuntimeError("LLM response must be a JSON object")
    return parsed
