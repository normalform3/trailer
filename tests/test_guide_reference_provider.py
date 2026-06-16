import httpx

from app.models import HikingGuideRequest
from app.providers.guide_reference import DefaultGuideReferenceProvider


def test_reference_provider_skips_without_material() -> None:
    provider = DefaultGuideReferenceProvider()

    result = provider.collect(HikingGuideRequest(destination="武功山"), [], "主攻略摘要")

    assert result is None


def test_reference_provider_summarizes_user_notes() -> None:
    provider = DefaultGuideReferenceProvider()

    result = provider.collect(
        HikingGuideRequest(
            destination="武功山",
            reference_notes="路线从龙山村上山，全程约 18 公里。山顶住宿紧张，补给点不稳定，返程班车需要提前核验。",
        ),
        [],
        "主攻略摘要",
    )

    assert result is not None
    assert result.items[0].source == "user-notes"
    assert result.items[0].route_clues
    assert result.lodging_supply_transport_notes
    assert result.verification_items


def test_reference_provider_reads_public_links_and_reports_failures(monkeypatch) -> None:
    calls = []

    def fake_get(url, headers, follow_redirects, timeout):
        calls.append(url)
        if "ok.example" in url:
            return httpx.Response(
                200,
                request=httpx.Request("GET", url),
                headers={"content-type": "text/html; charset=utf-8"},
                text="<html><title>武功山攻略</title><body>路线从龙山村上山。山顶住宿需要提前预订。下雨时路面湿滑。</body></html>",
            )
        return httpx.Response(
            403,
            request=httpx.Request("GET", url),
            headers={"content-type": "text/html"},
            text="forbidden",
        )

    monkeypatch.setattr(httpx, "get", fake_get)
    provider = DefaultGuideReferenceProvider()

    result = provider.collect(
        HikingGuideRequest(
            destination="武功山",
            reference_links=[
                "https://ok.example/guide",
                "https://ok.example/guide",
                "ftp://bad.example/guide",
                "https://blocked.example/guide",
            ],
        ),
        [],
        "主攻略摘要",
    )

    assert result is not None
    assert calls == ["https://ok.example/guide", "https://blocked.example/guide"]
    assert len(result.items) == 1
    assert result.items[0].title == "武功山攻略"
    assert any("不是公开 http(s)" in warning for warning in result.warnings)
    assert any("无法读取" in warning for warning in result.warnings)
