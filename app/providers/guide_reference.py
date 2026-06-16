from __future__ import annotations

import html
import re
from typing import Protocol
from urllib.parse import urlparse

import httpx

from app.models import GuideReferenceItem, GuideReferenceResearch, HikingGuideRequest, RouteCandidate


class GuideReferenceProvider(Protocol):
    def collect(
        self,
        request: HikingGuideRequest,
        candidates: list[RouteCandidate],
        guide_summary: str,
    ) -> GuideReferenceResearch | None:
        raise NotImplementedError


class NoopGuideReferenceProvider:
    def collect(
        self,
        request: HikingGuideRequest,
        candidates: list[RouteCandidate],
        guide_summary: str,
    ) -> GuideReferenceResearch | None:
        return None


class DefaultGuideReferenceProvider:
    def __init__(self, timeout_s: float = 8.0, max_chars: int = 5000) -> None:
        self.timeout_s = timeout_s
        self.max_chars = max_chars

    def collect(
        self,
        request: HikingGuideRequest,
        candidates: list[RouteCandidate],
        guide_summary: str,
    ) -> GuideReferenceResearch | None:
        links = _dedupe(request.reference_links)
        note = (request.reference_notes or "").strip()
        if not links and not note:
            return None

        items: list[GuideReferenceItem] = []
        warnings: list[str] = []

        if note:
            items.append(self._item_from_text("用户粘贴攻略摘录", note, source="user-notes"))

        for link in links:
            if not _is_public_http_url(link):
                warnings.append(f"参考攻略链接已跳过，URL 不是公开 http(s) 地址：{link}")
                continue
            try:
                items.append(self._item_from_url(link))
            except Exception as exc:  # noqa: BLE001 - reference material must not block guide generation.
                warnings.append(f"参考攻略链接无法读取：{link}（{exc}）")

        if not items and warnings:
            return GuideReferenceResearch(warnings=warnings)
        if not items:
            return None

        return GuideReferenceResearch(
            items=items,
            supplemental_summary=_supplemental_summary(items, guide_summary),
            itinerary_suggestions=_dedupe(_flatten(item.route_clues for item in items))[:6],
            lodging_supply_transport_notes=_dedupe(
                [
                    *_flatten(item.lodging_clues for item in items),
                    *_flatten(item.supply_clues for item in items),
                    *_flatten(item.transport_clues for item in items),
                ]
            )[:8],
            risk_notes=_dedupe(_flatten(item.risk_notes for item in items))[:8],
            verification_items=_dedupe(
                [
                    *_flatten(item.verification_items for item in items),
                    "参考攻略为用户提供的经验材料，不覆盖主攻略结论；出发前仍需核对官方公告、天气预警和交通时刻。",
                ]
            )[:10],
            warnings=warnings,
        )

    def _item_from_url(self, url: str) -> GuideReferenceItem:
        response = httpx.get(
            url,
            headers={
                "User-Agent": "TrailerGuideReferenceBot/0.1 (+user-provided-url; summary-only)",
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
            raise RuntimeError("页面没有可摘要的正文")
        site = urlparse(str(response.url or url)).netloc or "公开网页"
        return self._item_from_text(
            title or site,
            text,
            source=f"reference-link:{site}",
            url=str(response.url or url),
            confidence=0.52,
        )

    def _item_from_text(
        self,
        title: str,
        text: str,
        source: str,
        url: str | None = None,
        confidence: float = 0.48,
    ) -> GuideReferenceItem:
        clean_text = _normalize_text(text)[: self.max_chars]
        sentences = _sentences(clean_text)
        return GuideReferenceItem(
            title=title[:80] or "参考攻略",
            summary=_summary(sentences, clean_text),
            source=source,
            url=url,
            route_clues=_pick(sentences, ("路线", "起点", "终点", "上山", "下山", "穿越", "环线", "公里", "小时")),
            lodging_clues=_pick(sentences, ("住宿", "客栈", "民宿", "帐篷", "营地", "酒店")),
            supply_clues=_pick(sentences, ("补给", "水源", "小卖部", "便利店", "吃饭", "餐")),
            transport_clues=_pick(sentences, ("交通", "高铁", "火车", "班车", "包车", "停车", "接驳", "返程")),
            risk_notes=_pick(sentences, ("危险", "风险", "迷路", "封山", "防火", "大风", "下雨", "湿滑", "夜路", "塌方")),
            verification_items=_verification_items(sentences),
            confidence=confidence,
        )


def _extract_readable_text(raw: str) -> tuple[str, str | None]:
    title_match = re.search(r"<title[^>]*>(.*?)</title>", raw, flags=re.IGNORECASE | re.DOTALL)
    title = _normalize_text(title_match.group(1)) if title_match else None
    text = re.sub(r"<(script|style|noscript|svg|canvas)[^>]*>.*?</\1>", " ", raw, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    return _normalize_text(html.unescape(text)), title


def _normalize_text(value: str) -> str:
    value = html.unescape(value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[。！？!?；;])\s*|\n+", text)
    return [part.strip() for part in parts if len(part.strip()) >= 8]


def _summary(sentences: list[str], text: str) -> str:
    selected = sentences[:3] or [text[:220]]
    summary = " ".join(selected)
    return summary[:360].strip()


def _pick(sentences: list[str], keywords: tuple[str, ...], limit: int = 4) -> list[str]:
    matches = []
    for sentence in sentences:
        if any(keyword in sentence for keyword in keywords):
            matches.append(sentence[:180])
    return _dedupe(matches)[:limit]


def _verification_items(sentences: list[str]) -> list[str]:
    items = _pick(
        sentences,
        ("住宿", "补给", "水源", "交通", "班车", "封山", "防火", "天气", "返程", "开放"),
        limit=5,
    )
    return [f"核验：{item}" for item in items]


def _supplemental_summary(items: list[GuideReferenceItem], guide_summary: str) -> str:
    titles = "、".join(item.title for item in items[:3])
    return (
        f"已读取 {len(items)} 份用户提供的参考攻略材料（{titles}）。"
        "以下内容仅作为主攻略后的经验补充，不替代路线分析、天气和官方信息核验。"
    )


def _is_public_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        value = value.strip()
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _flatten(groups) -> list[str]:
    values: list[str] = []
    for group in groups:
        values.extend(group)
    return values
