from __future__ import annotations

import json
from time import perf_counter
from math import ceil
from typing import Any, Protocol

from app.config import get_settings
from app.models import (
    GearCategory,
    GearList,
    GuideDecision,
    GuideReferenceResearch,
    GuideToolPlan,
    HikingGuideRequest,
    Itinerary,
    RiskPoint,
    RouteCandidate,
    RouteGeometry,
    SafetyGuide,
    TransportPlan,
    TravelResearch,
)


class GuideDraft:
    def __init__(
        self,
        summary: str,
        recommendations: list[str],
        source: str,
        itinerary: Itinerary | None = None,
        gear_list: GearList | None = None,
        safety_guide: SafetyGuide | None = None,
    ) -> None:
        self.summary = summary
        self.recommendations = recommendations
        self.source = source
        self.itinerary = itinerary
        self.gear_list = gear_list
        self.safety_guide = safety_guide


class GuideLLMProvider(Protocol):
    def generate_guide(
        self,
        request: HikingGuideRequest,
        candidates: list[RouteCandidate],
        warnings: list[str],
        data_sources: list[str],
        travel_research: TravelResearch | None = None,
        transport_plan: TransportPlan | None = None,
    ) -> GuideDraft:
        raise NotImplementedError

    def generate_reference_research(
        self,
        request: HikingGuideRequest,
        candidates: list[RouteCandidate],
        guide_summary: str,
        reference_research: GuideReferenceResearch,
    ) -> GuideReferenceResearch:
        raise NotImplementedError


class GuidePlanningProvider(Protocol):
    def plan_tools(
        self,
        request: HikingGuideRequest,
        routes: list[RouteGeometry],
        warnings: list[str],
        data_sources: list[str],
    ) -> GuideDecision | GuideToolPlan:
        raise NotImplementedError


class StaticGuidePlanningProvider:
    def plan_tools(
        self,
        request: HikingGuideRequest,
        routes: list[RouteGeometry],
        warnings: list[str],
        data_sources: list[str],
    ) -> GuideDecision:
        has_routes = bool(routes)
        questions: list[str] = []
        notes: list[str] = []
        if not request.start_city:
            questions.append("你从哪个城市出发？补充后可以给出更合适的机票/高铁方案。")
            notes.append("缺少出发城市，交通方案只能保留通用建议。")
        if not request.date_range:
            questions.append("计划哪天出发、哪天返回？补充后可以查询机票报价并细化天气窗口。")
            notes.append("缺少出行日期，票价查询会跳过或降级为占位提示。")
        return GuideDecision(
            tool_plan=GuideToolPlan(
                query_weather=has_routes,
                query_lodging=has_routes,
                query_food=has_routes,
                query_supply=has_routes,
                query_transport=bool(has_routes and request.start_city),
                compose_with_llm=True,
                rationale=["使用代码默认工具计划"],
            ),
            clarifying_questions=questions,
            validation_notes=notes,
        )


class TemplateGuideProvider:
    @staticmethod
    def _aggregate_candidates(candidates: list[RouteCandidate]) -> dict:
        """Aggregate metrics across all route candidates (multi-segment trails)."""
        total_distance = sum(c.analysis.distance_km for c in candidates)
        total_duration = sum(c.analysis.estimated_duration_hours for c in candidates)
        all_elev = [c.analysis.elevation for c in candidates]
        max_elev = max((e.max_m for e in all_elev if e.max_m is not None), default=None)
        min_elev = min((e.min_m for e in all_elev if e.min_m is not None), default=None)
        total_ascent = sum((e.ascent_m or 0) for e in all_elev)
        total_descent = sum((e.descent_m or 0) for e in all_elev)
        worst_risk = "high" if any(c.analysis.risk_level == "high" for c in candidates) else (
            "medium" if any(c.analysis.risk_level == "medium" for c in candidates) else "low"
        )
        all_risk_factors: list[str] = []
        for c in candidates:
            for f in c.analysis.risk_factors:
                if f not in all_risk_factors:
                    all_risk_factors.append(f)
        all_warnings: list[str] = []
        for c in candidates:
            for w in c.analysis.warnings:
                if w not in all_warnings:
                    all_warnings.append(w)
        route_names = [c.route.name for c in candidates]
        return {
            "total_distance": total_distance,
            "total_duration": total_duration,
            "max_elevation": max_elev,
            "min_elevation": min_elev,
            "total_ascent": total_ascent,
            "total_descent": total_descent,
            "worst_risk": worst_risk,
            "all_risk_factors": all_risk_factors,
            "all_warnings": all_warnings,
            "route_names": route_names,
            "count": len(candidates),
        }

    def generate_guide(
        self,
        request: HikingGuideRequest,
        candidates: list[RouteCandidate],
        warnings: list[str],
        data_sources: list[str],
        travel_research: TravelResearch | None = None,
        transport_plan: TransportPlan | None = None,
    ) -> GuideDraft:
        if not candidates:
            return GuideDraft(
                summary=f"未能为 {request.destination} 生成可用路线，请补充 KML 或更具体的路线描述。",
                recommendations=["补充 KML 路线文件或明确起终点后重新生成攻略。"],
                source="template",
            )

        agg = self._aggregate_candidates(candidates)
        multi = agg["count"] > 1
        if multi:
            name_list = "、".join(agg["route_names"][:5])
            summary = (
                f"已为 {request.destination} 规划 {agg['count']} 段拼接路线（{name_list}），"
                f"全程约 {agg['total_distance']:.1f} km，预计 {agg['total_duration']:.1f} 小时，"
                f"风险等级 {agg['worst_risk']}。"
            )
        else:
            best = candidates[0]
            summary = (
                f"已为 {request.destination} 生成路线方案。"
                f"路线为「{best.route.name}」，来源为{best.label}，"
                f"约 {best.analysis.distance_km} km，预计 {best.analysis.estimated_duration_hours} 小时，"
                f"风险等级 {best.analysis.risk_level}。"
            )
        recommendations = [
            "出发前再次核对天气预警、景区开放状态、交通末班时间和补给点营业情况。",
            "API 规划路线仅代表可行路径建议，不应包装为社区热门路线或真实用户轨迹。",
        ]
        if travel_research:
            recommendations.extend(travel_research.next_steps[:3])
            if travel_research.lodging:
                recommendations.append(f"住宿优先核对：{travel_research.lodging[0].title}。")
            if travel_research.transport:
                recommendations.append(f"交通优先核对：{travel_research.transport[0].title}。")
        if any(candidate.route.source == "user_kml" for candidate in candidates):
            recommendations.insert(0, "已优先使用用户提供的 KML 轨迹，并在此基础上补充距离、海拔和风险分析。")
        if agg["worst_risk"] == "high":
            recommendations.append("存在高风险路线，建议降低强度、缩短行程或选择更稳定天气窗口。")

        # 生成静态行程日程
        itinerary = self._template_itinerary(request, candidates, agg)
        # 生成静态装备清单
        gear_list = self._template_gear_list(request, candidates, agg)
        # 生成静态安全提醒
        safety_guide = self._template_safety_guide(request, candidates, warnings, agg)

        return GuideDraft(
            summary=summary,
            recommendations=recommendations,
            source="template",
            itinerary=itinerary,
            gear_list=gear_list,
            safety_guide=safety_guide,
        )

    def generate_reference_research(
        self,
        request: HikingGuideRequest,
        candidates: list[RouteCandidate],
        guide_summary: str,
        reference_research: GuideReferenceResearch,
    ) -> GuideReferenceResearch:
        return reference_research

    def _template_itinerary(self, request: HikingGuideRequest, candidates: list[RouteCandidate], agg: dict) -> Itinerary | None:
        if not candidates:
            return None
        distance = agg["total_distance"]
        duration = agg["total_duration"]
        route_names = agg["route_names"]
        context = self.itinerary_planning_context(request, candidates, agg)
        total_days = int(context["planned_days"])
        active_days = int(context["minimum_days"])
        is_multi = total_days > 1

        days: list[dict] = []
        leisure_before = total_days > active_days
        day_number = 1
        if leisure_before:
            days.append({
                "day_number": day_number,
                "title": "Day 1：抵达与登山口适应",
                "distance_km": None,
                "elevation_gain_m": None,
                "key_segments": [
                    f"{request.start_city or '出发地'} 前往 {request.destination} 周边或登山口",
                    "核对天气、补给、住宿和次日上山交通",
                ],
                "lodging_suggestion": "住在登山口、景区周边镇区或便于次日出发的位置",
                "notes": ["这是放宽行程日，不计入核心徒步里程"],
            })
            day_number += 1

        hiking_days_available = min(active_days, total_days - len(days))
        hiking_days_available = max(1, hiking_days_available)
        if len(candidates) > 1 and hiking_days_available <= len(candidates):
            segs_per_day = max(1, ceil(len(candidates) / hiking_days_available))
            day_idx = 0
            for active_index in range(1, hiking_days_available + 1):
                is_last_hiking_day = active_index == hiking_days_available
                start_i = day_idx
                end_i = len(candidates) if is_last_hiking_day else min(day_idx + segs_per_day, len(candidates))
                day_segs = candidates[start_i:end_i]
                day_names = [c.route.name for c in day_segs]
                day_dist = sum(c.analysis.distance_km for c in day_segs)
                day_ascent = sum((c.analysis.elevation.ascent_m or 0) for c in day_segs)
                day_dur = sum(c.analysis.estimated_duration_hours for c in day_segs)
                seg_details = [f"{c.route.name}（{c.analysis.distance_km:.1f} km）" for c in day_segs]
                days.append({
                    "day_number": day_number,
                    "title": f"Day {day_number}：{'→'.join(day_names)}",
                    "distance_km": round(day_dist, 1),
                    "elevation_gain_m": round(day_ascent, 0) if day_ascent else None,
                    "key_segments": seg_details + [f"当日合计 {day_dist:.1f} km，约 {day_dur:.1f} 小时"],
                    "lodging_suggestion": "需提前确认沿途住宿" if not is_last_hiking_day else None,
                    "notes": ["注意分配体力，留足余量"],
                })
                day_number += 1
                day_idx = end_i
        else:
            daily_dist = round(distance / hiking_days_available, 1)
            daily_ascent = round(agg["total_ascent"] / hiking_days_available, 0) if agg["total_ascent"] else None
            for active_index in range(1, hiking_days_available + 1):
                is_last_hiking_day = active_index == hiking_days_available
                segment_label = "单日徒步" if hiking_days_available == 1 else ("下山/收尾段" if is_last_hiking_day else "上山/主线段")
                days.append({
                    "day_number": day_number,
                    "title": f"Day {day_number}：{segment_label}",
                    "distance_km": daily_dist,
                    "elevation_gain_m": daily_ascent if not is_last_hiking_day else None,
                    "key_segments": [
                        f"覆盖 {'→'.join(route_names[:4])}",
                        f"预计行进 {daily_dist} km，核心徒步总耗时约 {duration:.1f} 小时",
                    ],
                    "lodging_suggestion": "需提前确认沿途住宿" if not is_last_hiking_day else None,
                    "notes": ["注意分配体力，留足余量"],
                })
                day_number += 1

        while day_number <= total_days:
            days.append({
                "day_number": day_number,
                "title": f"Day {day_number}：周边游览与机动缓冲",
                "distance_km": None,
                "elevation_gain_m": None,
                "key_segments": [
                    f"在 {request.destination} 周边安排轻量游览、摄影或休整",
                    "预留天气变化、交通接驳和返程缓冲",
                ],
                "lodging_suggestion": "可继续住景区周边或转住返程交通更便利的城镇",
                "notes": ["用户选择天数多于核心徒步所需，已加入舒缓安排"],
            })
            day_number += 1

        from app.models import DayPlan
        return Itinerary(
            is_multi_day=is_multi,
            total_days=total_days,
            days=[DayPlan(**d) for d in days],
            notes=[
                "行程为模板建议，请根据实际路线、天气和体力调整",
                *context["notes"],
            ],
        )

    def itinerary_planning_context(
        self,
        request: HikingGuideRequest,
        candidates: list[RouteCandidate],
        agg: dict | None = None,
    ) -> dict[str, Any]:
        agg = agg or self._aggregate_candidates(candidates)
        target_hours = _target_daily_hiking_hours(request.fitness_level)
        min_by_duration = max(1, ceil(float(agg["total_duration"]) / target_hours))
        min_by_distance = max(1, ceil(float(agg["total_distance"]) / 18.0))
        minimum_days = max(min_by_duration, min_by_distance)
        reasonable_max = minimum_days + 3
        requested_days = _requested_trip_days(request)
        notes: list[str] = [
            f"按路线约 {agg['total_distance']:.1f} km / {agg['total_duration']:.1f} 小时估算，核心徒步最短建议 {minimum_days} 天。",
            f"合理行程范围建议为 {minimum_days}-{reasonable_max} 天。",
        ]
        mode = "estimated"
        planned_days = minimum_days
        if requested_days is not None:
            if requested_days < minimum_days:
                planned_days = minimum_days
                mode = "too_short"
                notes.append(
                    f"你选择了 {requested_days} 天，短于最短建议 {minimum_days} 天；已按最短可行日程规划，并建议调整日期。"
                )
            elif requested_days <= reasonable_max:
                planned_days = requested_days
                mode = "relaxed" if requested_days > minimum_days else "matched"
                if requested_days > minimum_days:
                    notes.append(
                        f"你选择了 {requested_days} 天，多出的时间已用于抵达适应、周边游览或机动缓冲。"
                    )
            else:
                planned_days = reasonable_max
                mode = "too_long"
                notes.append(
                    f"你选择了 {requested_days} 天，明显长于路线本身需要；建议压缩到 {minimum_days}-{reasonable_max} 天，本次按 {reasonable_max} 天展示。"
                )

        return {
            "requested_days": requested_days,
            "minimum_days": minimum_days,
            "reasonable_min_days": minimum_days,
            "reasonable_max_days": reasonable_max,
            "planned_days": planned_days,
            "mode": mode,
            "target_daily_hiking_hours": target_hours,
            "notes": notes,
        }

    def _template_gear_list(self, request: HikingGuideRequest, candidates: list[RouteCandidate], agg: dict) -> GearList | None:
        if not candidates:
            return None

        categories: list[GearCategory] = []
        categories.append(GearCategory(category="基础装备", items=["登山杖", "登山鞋/徒步鞋", "双肩背包(30-40L)", "头灯/手电筒"]))

        clothing = ["速干衣裤", "防风外套", "雨衣/防水外套"]
        if agg["max_elevation"] is not None and agg["max_elevation"] >= 3000:
            clothing.extend(["抓绒衣/羽绒内胆", "保暖帽", "防风手套"])
        categories.append(GearCategory(category="衣物防护", items=clothing))

        food_items = ["充足饮水(2-3L)", "高热量路粮(能量棒/坚果)", "电解质饮料"]
        if agg["total_distance"] > 20:
            food_items.append("午餐便当/压缩饼干")
        categories.append(GearCategory(category="饮食补给", items=food_items))

        safety_items = ["急救包(创可贴/绷带/碘伏)", "求生哨", "防晒霜/墨镜"]
        if agg["worst_risk"] in ("medium", "high"):
            safety_items.extend(["保温毯", "备用手机电池/充电宝"])
        if request.fitness_level == "beginner":
            safety_items.append("护膝")
        categories.append(GearCategory(category="安全应急", items=safety_items))

        categories.append(GearCategory(category="电子导航", items=["手机(离线地图)", "充电宝", "运动手表/GPS"]))

        notes = []
        if agg["max_elevation"] is not None and agg["max_elevation"] >= 3000:
            notes.append("高海拔路线，注意防寒和高原反应")
        return GearList(categories=categories, notes=notes)

    def _template_safety_guide(self, request: HikingGuideRequest, candidates: list[RouteCandidate], warnings: list[str], agg: dict) -> SafetyGuide | None:
        if not candidates:
            return None

        general_warnings = [
            "出发前告知家人/朋友行程路线和预计返回时间",
            "不要独自走未开发的野路",
            "遇到恶劣天气立即下撤，不要冒险继续",
        ]
        if agg["max_elevation"] is not None and agg["max_elevation"] >= 3000:
            general_warnings.append("高海拔地区注意高原反应，出现症状立即下撤")

        risk_points: list[RiskPoint] = []
        for factor in agg["all_risk_factors"]:
            severity = "high" if agg["worst_risk"] == "high" else "medium"
            mitigation = "提高警觉，必要时下撤"
            if "高海拔" in factor:
                mitigation = "缓慢行进，注意呼吸节奏，出现不适立即下撤"
            elif "降水" in factor:
                mitigation = "携带雨具，避免涉水路段，注意路面湿滑"
            elif "大风" in factor:
                mitigation = "避免山脊暴露路段，注意保暖"
            elif "高温" in factor:
                mitigation = "避开正午时段，增加饮水频率"
            elif "低温" in factor:
                mitigation = "增加保暖层，注意防风防冻"
            risk_points.append(RiskPoint(
                location_description="路线全程",
                risk_type=factor,
                severity=severity,
                mitigation=mitigation,
            ))

        return SafetyGuide(
            general_warnings=general_warnings,
            risk_points=risk_points,
            emergency_contacts=["景区救援电话（出发前查询确认）", "当地卫生院/医院电话"],
            emergency_measures=[
                "迷路时原地等待救援，不要盲目移动",
                "受伤时先做简单包扎固定，再寻求救援",
                "拨打 110/119 求助时报告准确位置和伤情",
            ],
            seasonal_notes=["请根据实际出行季节关注对应安全事项"],
        )


class BailianQwenGuideProvider:
    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        base_http_api_url: str | None = None,
        timeout_s: float = 60.0,
    ) -> None:
        settings = get_settings()
        self.api_key = (
            api_key
            or settings.api_keys.dashscope_api_key
            or settings.api_keys.bailian_api_key
        )
        self.model = model or settings.bailian.model
        self.base_http_api_url = (base_http_api_url or settings.dashscope.base_http_api_url).rstrip("/")
        self.timeout_s = timeout_s

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def test_connection(self) -> dict[str, object]:
        if not self.api_key:
            raise RuntimeError("DASHSCOPE_API_KEY or BAILIAN_API_KEY is not configured")

        try:
            import dashscope
        except ModuleNotFoundError as exc:
            raise RuntimeError("dashscope package is not installed; run `pip install -e .`") from exc

        dashscope.base_http_api_url = self.base_http_api_url
        started = perf_counter()
        messages = [
            {
                "role": "user",
                "content": [{"text": "只回复 OK"}],
            }
        ]
        try:
            response = dashscope.MultiModalConversation.call(
                api_key=self.api_key,
                model=self.model,
                messages=messages,
                parameters={"max_tokens": 2},
            )
        except TypeError:
            response = dashscope.MultiModalConversation.call(
                api_key=self.api_key,
                model=self.model,
                messages=messages,
            )
        self._raise_for_dashscope_error(response)
        text = str(self._extract_text(response) or "").strip()
        return {
            "ok": True,
            "model": self.model,
            "elapsed_ms": round((perf_counter() - started) * 1000),
            "reply_preview": text[:20],
        }

    def generate_guide(
        self,
        request: HikingGuideRequest,
        candidates: list[RouteCandidate],
        warnings: list[str],
        data_sources: list[str],
        travel_research: TravelResearch | None = None,
        transport_plan: TransportPlan | None = None,
    ) -> GuideDraft:
        if not self.api_key:
            raise RuntimeError("DASHSCOPE_API_KEY or BAILIAN_API_KEY is not configured")

        try:
            import dashscope
        except ModuleNotFoundError as exc:
            raise RuntimeError("dashscope package is not installed; run `pip install -e .`") from exc

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
                                "你是一个严谨的中文徒步攻略规划助手。你只能基于输入的结构化数据生成攻略内容，"
                                "不得编造热门程度、社区评价、未提供的交通班次、余房或住宿价格。\n\n"
                                "输出必须是 JSON 对象，包含以下字段：\n"
                                "1. \"summary\": 综合概述（2-4句，包含路线特征、难度评估、行程天数判断）\n"
                                "2. \"recommendations\": 建议列表（6-10条，按重要性排序，涵盖出发前准备、行程注意事项、天气关注点）\n"
                                "3. \"itinerary\": 行程日程规划，包含：\n"
                                "   - \"is_multi_day\": boolean，是否多日行程（超过8小时或超过20km且有住宿需求则视为多日）\n"
                                "   - \"total_days\": 总天数\n"
                                "   - \"days\": 数组，每个包含 day_number(int), title(str), distance_km(float), elevation_gain_m(float|null), "
                                "key_segments(str[]), lodging_suggestion(str|null), notes(str[])\n"
                                "4. \"gear_list\": 装备物资清单，包含：\n"
                                "   - \"categories\": 数组，每个包含 category(str: 基础装备|衣物防护|饮食补给|安全应急|电子导航|其他), items(str[])\n"
                                "   - \"notes\": 装备补充说明(str[])\n"
                                "   生成规则：基于 distance_km、elevation、risk_level、weather、fitness_level 决定。"
                                "高海拔(>3000m)增加防寒层/防晒/电解质；长距离(>20km)增加备用鞋/路粮；"
                                "降水概率高增加防水外套/防水袋；初学者增加护膝/登山杖\n"
                                "5. \"safety_guide\": 安全提醒，包含：\n"
                                "   - \"general_warnings\": 一般安全警告(str[], 3-5条)\n"
                                "   - \"risk_points\": 路线风险点(str[]，每个含 location_description, risk_type, severity(low|medium|high), mitigation)\n"
                                "   - \"emergency_contacts\": 应急联系建议(str[])\n"
                                "   - \"emergency_measures\": 应急措施(str[], 3-5条)\n"
                                "   - \"seasonal_notes\": 季节性注意事项(str[])\n\n"
                                "重要规则：route_candidates 中的多条路线通常是同一条步道的多个分段拼接（而非备选路线）。"
                                "请将所有分段的距离和耗时相加得到全程数据，行程日程要覆盖全部分段，"
                                "summary 中要体现全程总距离和总耗时。\n"
                                "若输入 JSON 含 itinerary_planning，必须遵守其中 planned_days 生成对应天数的 days；"
                                "当 mode 为 too_short/too_long/relaxed 时，要在 summary 或 recommendations 中提示"
                                "用户选择天数、最短建议天数和合理范围。用户天数比最短建议多 1-3 天时，"
                                "不要删减天数，应加入抵达适应、周边游览、摄影休整或天气缓冲。\n\n"
                                f"输入 JSON：{self._build_prompt_payload(request, candidates, warnings, data_sources, travel_research, transport_plan)}"
                            )
                        }
                    ],
                }
            ],
        )
        self._raise_for_dashscope_error(response)
        content = self._extract_text(response)
        parsed = self._parse_json_content(content)
        summary = str(parsed.get("summary") or "").strip()
        recommendations = parsed.get("recommendations") or []
        if not summary:
            raise RuntimeError("LLM response did not include summary")
        if not isinstance(recommendations, list):
            raise RuntimeError("LLM response recommendations must be a list")

        # 解析可选字段，容错处理
        itinerary = None
        try:
            itinerary_data = parsed.get("itinerary")
            if itinerary_data and isinstance(itinerary_data, dict):
                itinerary = Itinerary(**itinerary_data)
        except Exception:  # noqa: BLE001
            pass

        gear_list = None
        try:
            gear_data = parsed.get("gear_list")
            if gear_data and isinstance(gear_data, dict):
                gear_list = GearList(**gear_data)
        except Exception:  # noqa: BLE001
            pass

        safety_guide = None
        try:
            safety_data = parsed.get("safety_guide")
            if safety_data and isinstance(safety_data, dict):
                safety_guide = SafetyGuide(**safety_data)
        except Exception:  # noqa: BLE001
            pass

        return GuideDraft(
            summary=summary,
            recommendations=[str(item).strip() for item in recommendations if str(item).strip()],
            source=f"bailian:{self.model}",
            itinerary=itinerary,
            gear_list=gear_list,
            safety_guide=safety_guide,
        )

    def plan_tools(
        self,
        request: HikingGuideRequest,
        routes: list[RouteGeometry],
        warnings: list[str],
        data_sources: list[str],
    ) -> GuideDecision:
        if not self.api_key:
            raise RuntimeError("DASHSCOPE_API_KEY or BAILIAN_API_KEY is not configured")

        try:
            import dashscope
        except ModuleNotFoundError as exc:
            raise RuntimeError("dashscope package is not installed; run `pip install -e .`") from exc

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
                                "你是徒步攻略系统中的工具决策器，负责决定调用哪些代码工具、是否需要追问、"
                                "以及对结果做非阻断验证。不要生成攻略正文，不要编造外部事实。\n\n"
                                "输出必须是 JSON 对象，包含："
                                "tool_plan(object，含 query_weather/query_lodging/query_food/query_supply/"
                                "query_transport/compose_with_llm/rationale), "
                                "clarifying_questions(string[]), validation_notes(string[]), priority_notes(string[])。\n"
                                "规则：有路线时通常需要天气、住宿、餐饮和补给；只有用户提供 start_city 才需要交通；"
                                "缺少日期或出发城市时，不阻断生成，只在 clarifying_questions 和 validation_notes 里提示。"
                                f"\n\n输入 JSON：{self._build_tool_plan_payload(request, routes, warnings, data_sources)}"
                            )
                        }
                    ],
                }
            ],
        )
        self._raise_for_dashscope_error(response)
        parsed = self._parse_json_content(self._extract_text(response))
        if "tool_plan" in parsed:
            return GuideDecision(**parsed)
        return GuideDecision(tool_plan=GuideToolPlan(**parsed))

    def generate_reference_research(
        self,
        request: HikingGuideRequest,
        candidates: list[RouteCandidate],
        guide_summary: str,
        reference_research: GuideReferenceResearch,
    ) -> GuideReferenceResearch:
        if not self.api_key:
            raise RuntimeError("DASHSCOPE_API_KEY or BAILIAN_API_KEY is not configured")

        try:
            import dashscope
        except ModuleNotFoundError as exc:
            raise RuntimeError("dashscope package is not installed; run `pip install -e .`") from exc

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
                                "你是徒步攻略系统中的参考攻略补充规划器。"
                                "你只能基于用户提供的参考攻略结构化材料做补充，不得覆盖主攻略结论，"
                                "不得把参考攻略包装为官方事实，不得复刻原文长段内容。\n\n"
                                "输出必须是 JSON 对象，只包含以下字段："
                                "supplemental_summary(string), itinerary_suggestions(string[]), "
                                "lodging_supply_transport_notes(string[]), risk_notes(string[]), "
                                "verification_items(string[])。\n"
                                "要求：所有内容都应表述为经验参考或待核验项；若参考材料与主攻略摘要或路线数据冲突，"
                                "放入 verification_items，不要改写主攻略。\n\n"
                                f"输入 JSON：{self._build_reference_payload(request, candidates, guide_summary, reference_research)}"
                            )
                        }
                    ],
                }
            ],
        )
        self._raise_for_dashscope_error(response)
        parsed = self._parse_json_content(self._extract_text(response))
        return reference_research.model_copy(
            update={
                "supplemental_summary": str(parsed.get("supplemental_summary") or reference_research.supplemental_summary or "").strip() or None,
                "itinerary_suggestions": _string_list(parsed.get("itinerary_suggestions")) or reference_research.itinerary_suggestions,
                "lodging_supply_transport_notes": (
                    _string_list(parsed.get("lodging_supply_transport_notes"))
                    or reference_research.lodging_supply_transport_notes
                ),
                "risk_notes": _string_list(parsed.get("risk_notes")) or reference_research.risk_notes,
                "verification_items": _string_list(parsed.get("verification_items")) or reference_research.verification_items,
            }
        )

    def _build_prompt_payload(
        self,
        request: HikingGuideRequest,
        candidates: list[RouteCandidate],
        warnings: list[str],
        data_sources: list[str],
        travel_research: TravelResearch | None,
        transport_plan: TransportPlan | None,
    ) -> str:
        return json.dumps(
            {
                "destination": request.destination,
                "start_city": request.start_city,
                "date_range": [d.isoformat() for d in request.date_range]
                if request.date_range
                else None,
                "fitness_level": request.fitness_level,
                "preferences": request.preferences,
                "route_text": request.route_text,
                "route_candidates": [
                    {
                        "name": candidate.route.name,
                        "label": candidate.label,
                        "source": candidate.route.source,
                        "confidence": candidate.route.confidence,
                        "distance_km": candidate.analysis.distance_km,
                        "estimated_duration_hours": candidate.analysis.estimated_duration_hours,
                        "elevation": candidate.analysis.elevation.model_dump(),
                        "risk_level": candidate.analysis.risk_level,
                        "risk_factors": candidate.analysis.risk_factors,
                        "warnings": candidate.analysis.warnings,
                    }
                    for candidate in candidates
                ],
                "itinerary_planning": TemplateGuideProvider().itinerary_planning_context(
                    request,
                    candidates,
                ) if candidates else None,
                "warnings": warnings,
                "data_sources": data_sources,
                "travel_research": travel_research.model_dump() if travel_research else None,
                "transport_plan": transport_plan.model_dump() if transport_plan else None,
            },
            ensure_ascii=False,
        )

    def _build_tool_plan_payload(
        self,
        request: HikingGuideRequest,
        routes: list[RouteGeometry],
        warnings: list[str],
        data_sources: list[str],
    ) -> str:
        return json.dumps(
            {
                "destination": request.destination,
                "start_city": request.start_city,
                "date_range": [d.isoformat() for d in request.date_range]
                if request.date_range
                else None,
                "fitness_level": request.fitness_level,
                "preferences": request.preferences,
                "route_text": request.route_text,
                "route_count": len(routes),
                "route_sources": [route.source for route in routes],
                "warnings": warnings,
                "data_sources": data_sources,
            },
            ensure_ascii=False,
        )

    def _build_reference_payload(
        self,
        request: HikingGuideRequest,
        candidates: list[RouteCandidate],
        guide_summary: str,
        reference_research: GuideReferenceResearch,
    ) -> str:
        return json.dumps(
            {
                "destination": request.destination,
                "guide_summary": guide_summary,
                "route_overview": [
                    {
                        "name": candidate.route.name,
                        "source": candidate.route.source,
                        "distance_km": candidate.analysis.distance_km,
                        "estimated_duration_hours": candidate.analysis.estimated_duration_hours,
                        "risk_level": candidate.analysis.risk_level,
                        "risk_factors": candidate.analysis.risk_factors,
                    }
                    for candidate in candidates[:3]
                ],
                "reference_research": reference_research.model_dump(),
            },
            ensure_ascii=False,
        )

    def _raise_for_dashscope_error(self, response: Any) -> None:
        status_code = self._read(response, "status_code", default=None)
        if status_code is None or int(status_code) == 200:
            return
        code = self._read(response, "code", default="unknown")
        message = self._read(response, "message", default="DashScope request failed")
        raise RuntimeError(f"DashScope request failed ({status_code}, {code}): {message}")

    def _extract_text(self, response: Any) -> object:
        try:
            output = self._read(response, "output")
            choices = self._read(output, "choices")
            message = self._read(choices[0], "message")
            content = self._read(message, "content")
            if isinstance(content, str):
                return content
            return self._read(content[0], "text")
        except (KeyError, IndexError, TypeError, AttributeError) as exc:
            raise RuntimeError("DashScope response did not include text content") from exc

    def _read(self, value: Any, key: str, default: Any = ...) -> Any:
        try:
            if isinstance(value, dict):
                return value[key]
            return getattr(value, key)
        except (KeyError, AttributeError):
            if default is ...:
                raise
            return default

    def _parse_json_content(self, content: object) -> dict[str, object]:
        """解析模型返回的 content 字段，接受多种格式并返回 dict。

        支持的输入类型：
        - 已经是 dict：直接返回
        - 字符串：可能为纯 JSON、被 ```json ``` 包裹，或以 `json` 前缀开始
        - 列表：如果是单元素字符串列表，会尝试解析该字符串
        解析失败时抛出含义明确的 RuntimeError 以便上层捕获。
        """
        # 已经是 dict，直接返回
        if isinstance(content, dict):
            parsed = content
            if not isinstance(parsed, dict):
                raise RuntimeError("LLM response must be a JSON object")
            return parsed

        # 列表情况（例如某些实现返回 parts 列表）
        if isinstance(content, list):
            if len(content) == 1 and isinstance(content[0], str):
                content_str = content[0].strip()
            else:
                raise RuntimeError("LLM response must be a JSON object")
        elif isinstance(content, str):
            content_str = content.strip()
        else:
            raise RuntimeError("LLM response content has unsupported type")

        # 尝试提取被三重反引号包裹的内容 (```json ... ```)，支持大小写
        import re

        m = re.search(r"```(?:json)?\s*(.*?)\s*```", content_str, re.DOTALL | re.IGNORECASE)
        if m:
            content_str = m.group(1)

        # 有些模型会在字符串前加上 `json` 标识，去掉它
        if content_str.lower().startswith("json"):
            content_str = content_str[4:].strip()

        try:
            parsed = json.loads(content_str)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"Failed to parse LLM JSON content: {e.msg}") from e

        if not isinstance(parsed, dict):
            raise RuntimeError("LLM response must be a JSON object")

        return parsed


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _requested_trip_days(request: HikingGuideRequest) -> int | None:
    if not request.date_range:
        return None
    start, end = request.date_range
    return max(1, (end - start).days + 1)


def _target_daily_hiking_hours(fitness_level: str | None) -> float:
    if fitness_level == "beginner":
        return 6.0
    if fitness_level == "advanced":
        return 9.0
    return 8.0
